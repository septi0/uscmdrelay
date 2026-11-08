import asyncio
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Iterable
from uscmdrelay.exceptions import AuthenticationError, ConditionNotMet, RelayError
from uscmdrelay.libraries import payload
from uscmdrelay.libraries.cmd_exec import CmdExec, CmdExecError, CmdResult
from uscmdrelay.libraries.throttled_log import ThrottledLog
from uscmdrelay.models.command import CommandModel, ConditionModel, ExecModel, FieldModel, HookModel, SECRET_HEADERS
from uscmdrelay.models.key import KeyModel
from uscmdrelay.models.response import ResponseCode, ResponseModel
from uscmdrelay.models.uscmdrelay import UsCmdRelayModel
from uscmdrelay.services.auth import AuthService

__all__ = ['RelayService']

_INHERITED_ENV = ('PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'TZ')
_DEFAULT_FIELD = FieldModel()

@dataclass(frozen=True)
class _Run:
    id: str
    name: str
    command: ExecModel
    key_name: str
    peer_ip: str
    argv: list[str]
    stdin: bytes

    def __str__(self) -> str:
        # json keeps each value on one line
        return f'{self.name} run={self.id} key={self.key_name} ip={self.peer_ip} cmd={json.dumps(self.argv)}'

class RelayService:
    def __init__(self, model: UsCmdRelayModel, *, work_dir: str, logger: logging.Logger) -> None:
        self._logger: logging.Logger = logger
        self._failed_auth_log: ThrottledLog = ThrottledLog(logger)
        self._work_dir: str = work_dir
        self._limits = model.limits
        self._slots: asyncio.Semaphore = asyncio.Semaphore(model.limits.max_running)
        self._active: set[asyncio.Task] = set()
        self._draining: bool = False

        # one entry per command and arguments in progress, holding the run to do afterwards
        self._background: dict[tuple, _Run | None] = {}
        self._background_tasks: set[asyncio.Task] = set()

        self.update(model)

    def update(self, model: UsCmdRelayModel) -> None:
        """ Apply new keys, clusters and failure hook. Runs in progress are not affected. """
        self._auth = AuthService(model.keys)
        self._clusters = model.clusters
        self._on_failure = model.on_failure

    def authenticate(self, token: object, peer_ip: str) -> KeyModel:
        try:
            return self._auth.find_key(token, peer_ip)
        except AuthenticationError as e:
            self._failed_auth_log.warning(f'Authentication failed from {peer_ip}: {e}')
            raise RelayError('Authentication failed', ResponseCode.AUTH_FAILED)

    async def handle(self, raw: bytes, peer_ip: str, *, key: KeyModel | None = None, target: tuple[str, str] | None = None, headers: Iterable[tuple[str, str]] = ()) -> ResponseModel:
        """ Process a client message. The target comes from the listener when the message itself does not carry it. """
        try:
            return await self._handle(raw, peer_ip, key, target, headers)
        except RelayError as e:
            return ResponseModel.error(str(e), e.code)
        except Exception as e:
            self._logger.exception(e, exc_info=True)
            return ResponseModel.error('Server error', ResponseCode.SERVER_ERROR)

    def preview(self, raw: bytes, target: str, headers: Iterable[tuple[str, str]] = ()) -> list[str]:
        """ Work out the command a message would run, without running it. """
        format, data = self._parse(raw)
        command = self._find_command(*self._parse_target(target))

        return self._build_argv(command, format, data, headers)

    async def drain(self, timeout: float) -> None:
        """ Stop taking work, give running commands time to finish, then terminate the rest. """
        self._draining = True

        for key in self._background:
            if self._background[key] is not None:
                self._logger.warning(f'Dropped queued run of {self._background[key].name} on shutdown')
                self._background[key] = None

        if self._active:
            _, pending = await asyncio.wait(self._active, timeout=timeout)

            for task in pending:
                task.cancel()

            await asyncio.gather(*pending, return_exceptions=True)

        await asyncio.gather(*self._background_tasks, return_exceptions=True)

    async def _handle(self, raw: bytes, peer_ip: str, key: KeyModel | None, target: tuple[str, str] | None, headers: Iterable[tuple[str, str]]) -> ResponseModel:
        if self._draining:
            raise RelayError('Server is shutting down', ResponseCode.BUSY)

        format, data = self._parse(raw)

        if key is None:
            key = self.authenticate(data.get('auth_key'), peer_ip)

        cluster, name = target or self._parse_target(data.get('command'))

        if not self._auth.is_granted(key, cluster):
            self._logger.warning(f'Key "{key.name}" from {peer_ip} has no access to cluster {cluster!r}')
            raise RelayError('Authentication failed', ResponseCode.AUTH_FAILED)

        full_name = f'{cluster}::{name}'

        try:
            command = self._find_command(cluster, name)
            argv = self._build_argv(command, format, data, headers)
        except ConditionNotMet as e:
            self._logger.info(f'Skipped {full_name} for key "{key.name}" from {peer_ip}: {e}')
            return ResponseModel(status='skipped', message=str(e), code=ResponseCode.OK)
        except RelayError as e:
            self._logger.warning(f'Refused {full_name!r} for key "{key.name}" from {peer_ip}: {e}')
            raise

        run = _Run(secrets.token_hex(4), full_name, command, key.name, peer_ip, argv, self._build_stdin(raw, format, data, target))

        if command.background:
            return self._enqueue(run)

        return await self._run_now(run)

    def _parse(self, raw: bytes) -> tuple[str | None, dict]:
        try:
            return payload.parse(raw)
        except payload.PayloadError as e:
            raise RelayError(str(e), ResponseCode.BAD_MESSAGE)

    def _parse_target(self, command: object) -> tuple[str, str]:
        if not isinstance(command, str) or not command:
            raise RelayError('command is required', ResponseCode.BAD_MESSAGE)

        if '::' in command:
            cluster, name = command.split('::', 1)
            return cluster, name

        return 'default', command

    def _find_command(self, cluster: str, name: str) -> CommandModel:
        if cluster not in self._clusters:
            raise RelayError('Unknown cluster', ResponseCode.UNKNOWN_CLUSTER)

        if name not in self._clusters[cluster]:
            raise RelayError('Unknown command', ResponseCode.UNKNOWN_COMMAND)

        return self._clusters[cluster][name]

    def _build_argv(self, command: CommandModel, format: str | None, data: dict, headers: Iterable[tuple[str, str]]) -> list[str]:
        if format is not None and format != command.format:
            raise RelayError(f'This command expects {command.format} format', ResponseCode.WRONG_FORMAT)

        fields = {**data, 'headers': self._usable_headers(headers)}

        # before the fields, so a request that isn't for this command is skipped rather than refused
        for name, condition in command.when.items():
            if not self._is_condition_met(condition, name, fields):
                raise ConditionNotMet(f'Condition on "{name}" not met')

        return command.render(self._resolve_fields(command, fields))

    def _is_condition_met(self, condition: ConditionModel, name: str, data: dict) -> bool:
        try:
            value = payload.lookup(data, name)
        except KeyError:
            value = None

        # null counts as absent
        if condition.exists is not None:
            return condition.exists == (value is not None)

        text = self._to_text(value)

        return text is not None and len(text) <= self._limits.max_field_size and condition.pattern.fullmatch(text) is not None

    def _usable_headers(self, headers: Iterable[tuple[str, str]]) -> dict[str, str]:
        return {name.lower(): value for name, value in headers if name.lower() not in SECRET_HEADERS}

    def _resolve_fields(self, command: CommandModel, data: dict) -> dict[str, str]:
        values = {}

        for name in command.required_fields:
            rule = command.fields.get(name, _DEFAULT_FIELD)

            try:
                value = self._to_text(payload.lookup(data, name))
            except KeyError:
                if rule.default is None:
                    raise RelayError(f'Field "{name}" is missing', ResponseCode.INVALID_FIELD)

                value = rule.default

            if value is None or len(value) > self._limits.max_field_size or not rule.matches(value):
                raise RelayError(f'Field "{name}" has an invalid value', ResponseCode.INVALID_FIELD)

            values[name] = value

        return values

    def _to_text(self, value: object) -> str | None:
        if isinstance(value, bool):
            return 'true' if value else 'false'

        return str(value) if isinstance(value, (str, int, float)) else None

    def _build_stdin(self, raw: bytes, format: str | None, data: dict, target: tuple[str, str] | None) -> bytes:
        envelope = ['auth_key'] if target else ['auth_key', 'command']
        removed = [name for name in envelope if name in data]

        for name in removed:
            del data[name]

        return payload.serialize(format, data) if removed else raw

    def _enqueue(self, run: _Run) -> ResponseModel:
        key = (run.name, tuple(run.argv))

        if key not in self._background and len(self._background) >= self._limits.max_queued:
            self._logger.warning(f'Refused {run.name} from {run.peer_ip}, the background queue is full')
            raise RelayError('Too many commands queued', ResponseCode.BUSY)

        if key in self._background:
            self._background[key] = run
        else:
            self._background[key] = None

            task = asyncio.create_task(self._run_background(key, run))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        return ResponseModel(status='accepted', message='Command accepted', code=ResponseCode.OK, run_id=run.id)

    async def _run_background(self, key: tuple, run: _Run | None) -> None:
        while run is not None:
            async with self._slots:
                await self._run_background_once(run)

            run = self._background[key]
            self._background[key] = None

        del self._background[key]

    async def _run_background_once(self, run: _Run) -> None:
        if self._draining:
            self._logger.warning(f'Dropped queued run of {run.name} on shutdown')
            return

        try:
            result = await self._execute(run)

            if result.timed_out or result.returncode != 0:
                await self._run_failure_hook(run, result)
        except RelayError:
            # stopped by a shutdown, already logged
            pass
        except Exception as e:
            self._logger.exception(e, exc_info=True)

    async def _run_failure_hook(self, run: _Run, result: CmdResult) -> None:
        hook = self._find_hook(run.command)

        if hook is None:
            return

        if self._draining:
            self._logger.warning(f'Skipped on_failure of {run} on shutdown')
            return

        exit_code = 'none' if result.returncode is None else str(result.returncode)
        argv = hook.render({'run.command': run.name, 'run.exit_code': exit_code, 'run.id': run.id})

        await self._execute(_Run(run.id, f'{run.name} on_failure', hook, run.key_name, run.peer_ip, argv, result.stderr.encode('utf-8')))

    def _find_hook(self, command: ExecModel) -> HookModel | None:
        # a command setting on_failure, even to null, replaces the global one
        return command.on_failure if 'on_failure' in command.model_fields_set else self._on_failure

    async def _run_now(self, run: _Run) -> ResponseModel:
        if self._slots.locked():
            self._logger.warning(f'Refused {run.name} from {run.peer_ip}, too many commands running')
            raise RelayError('Too many commands running', ResponseCode.BUSY)

        async with self._slots:
            result = await self._execute(run)

        return self._build_response(run, result)

    async def _execute(self, run: _Run) -> CmdResult:
        started = time.monotonic()
        task = asyncio.create_task(CmdExec.exec(
            run.argv,
            input=run.stdin,
            env=self._build_env(run.command),
            cwd=run.command.cwd or self._work_dir,
            timeout=run.command.timeout,
            output_limit=self._limits.max_output_size,
        ))

        self._active.add(task)
        task.add_done_callback(self._active.discard)

        try:
            result = await task
        except CmdExecError as e:
            self._logger.error(f'{run} could not be started: {e}')
            return CmdResult(None, '', 'Command could not be started')
        except asyncio.CancelledError:
            if not self._draining:
                raise

            self._logger.warning(f'{run} terminated by shutdown')
            raise RelayError('Server is shutting down', ResponseCode.BUSY)

        self._log_result(run, result, time.monotonic() - started)

        return result

    def _build_env(self, command: ExecModel) -> dict[str, str]:
        return {**{name: os.environ[name] for name in _INHERITED_ENV if name in os.environ}, **command.env}

    def _log_result(self, run: _Run, result: CmdResult, duration: float) -> None:
        summary = f'{run} exit={result.returncode} duration={duration:.2f}s'

        if result.timed_out:
            self._logger.error(f'{summary} timed out stderr={result.stderr!r}')
        elif result.returncode != 0:
            self._logger.error(f'{summary} failed stderr={result.stderr!r}')
        else:
            self._logger.info(summary)

        if run.command.log_output:
            self._logger.info(f'{run.name} run={run.id} stdout={result.stdout!r} stderr={result.stderr!r}')
        else:
            self._logger.debug(f'{run.name} run={run.id} stdout={result.stdout!r}')

    def _build_response(self, run: _Run, result: CmdResult) -> ResponseModel:
        if result.timed_out:
            return ResponseModel.error('Command timed out', ResponseCode.COMMAND_TIMEOUT, run_id=run.id)

        if result.returncode != 0:
            message = 'Command failed' if run.command.hide_output else result.stderr or f'exited with code {result.returncode}'
            return ResponseModel.error(message, ResponseCode.COMMAND_FAILED, exit_code=result.returncode, run_id=run.id)

        message = 'Command succeeded' if run.command.hide_output else result.stdout

        return ResponseModel(status='ok', message=message, code=ResponseCode.OK, exit_code=result.returncode, run_id=run.id)
