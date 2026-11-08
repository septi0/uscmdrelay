import asyncio
import logging
import os
import resource
import signal
import tempfile
from ipaddress import ip_address
from logging.handlers import TimedRotatingFileHandler
from typing import Iterable
from pydantic import ValidationError
from uscmdrelay.exceptions import UsCmdRelayRuntimeError
from uscmdrelay.libraries.config_file import ConfigFile
from uscmdrelay.libraries.connection_limiter import ConnectionLimiter
from uscmdrelay.libraries.pid_file import PidFile
from uscmdrelay.listeners.http import HttpListener
from uscmdrelay.listeners.tcp import TcpListener
from uscmdrelay.models.listener import ListenerModel
from uscmdrelay.models.uscmdrelay import UsCmdRelayModel
from uscmdrelay.services.admin import AdminService
from uscmdrelay.services.relay import RelayService

__all__ = ['UsCmdRelayManager']

class _PrivateLogHandler(TimedRotatingFileHandler):
    def _open(self):
        stream = super()._open()
        os.fchmod(stream.fileno(), 0o640)

        return stream

class UsCmdRelayManager:
    def __init__(self, *, log_file: str | None = None, log_level: str | None = None, config_file: str | None = None, admin: bool = False) -> None:
        self._logger: logging.Logger = self._logger_factory(log_file, log_level)
        self._config_file: ConfigFile = ConfigFile(self._find_config_file(config_file))

        # the admin commands may be run by root on a config owned by the service user
        self._check_owner: bool = not admin
        self._model: UsCmdRelayModel = self._load_model()
        self._pid_file: PidFile = PidFile(self._model.pid_file or self._default_pid_filepath())
        self._relay: RelayService | None = None

    @property
    def admin(self) -> AdminService:
        return AdminService(self._config_file, self._validate, self._pid_file)

    def run_forever(self) -> bool:
        """ Run the service until it is told to stop. Returns False if it failed. """
        return asyncio.run(self._serve())

    def preview(self, target: str, raw: bytes, headers: Iterable[tuple[str, str]]) -> list[str]:
        """ Work out the command a message would run, without running it. """
        return RelayService(self._model, work_dir='', logger=self._logger.getChild('relay')).preview(raw, target, headers)

    def _find_config_file(self, config_file: str | None) -> str:
        if config_file:
            if not os.path.isfile(config_file):
                raise UsCmdRelayRuntimeError(f'Config file "{config_file}" does not exist')

            return config_file

        for directory in ('/etc/uscmdrelay', '/etc/opt/uscmdrelay', os.path.expanduser('~/.config/uscmdrelay')):
            path = os.path.join(directory, 'config.yml')

            if os.path.isfile(path):
                return path

        raise UsCmdRelayRuntimeError('Config file does not exist, create one from config.sample.yml or pass --config')

    def _load_model(self) -> UsCmdRelayModel:
        return self._validate(self._config_file.read(check_owner=self._check_owner))

    def _validate(self, data: dict) -> UsCmdRelayModel:
        try:
            return UsCmdRelayModel.model_validate(data, context={'secrets': self._config_file.read_secrets(check_owner=self._check_owner)})
        except ValidationError as e:
            problems = [f"  - {'.'.join(str(x) for x in error['loc']) or 'general'}: {error['msg']}" for error in e.errors(include_url=False)]

            raise UsCmdRelayRuntimeError('Configuration file contains {} error(s):\n{}'.format(e.error_count(), '\n'.join(problems)))

    def _default_pid_filepath(self) -> str:
        return '/var/run/uscmdrelay.pid' if os.getuid() == 0 else os.path.expanduser('~/.uscmdrelay.pid')

    def _logger_factory(self, log_file: str | None, log_level: str | None) -> logging.Logger:
        level = getattr(logging, log_level or 'INFO')
        logger = logging.getLogger()
        logger.setLevel(level)

        if log_file:
            if os.path.dirname(log_file):
                os.makedirs(os.path.dirname(log_file), exist_ok=True)

            handler = _PrivateLogHandler(log_file, when='midnight', backupCount=4)
        else:
            handler = logging.StreamHandler()

        handler.setLevel(level)
        handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

        logger.addHandler(handler)

        return logger

    async def _serve(self) -> bool:
        if not self._pid_file.acquire():
            self._logger.error('Service is already running')
            return False

        try:
            self._ensure_file_limit()

            # relative values given to a command must not reach anything
            with tempfile.TemporaryDirectory(prefix='uscmdrelay-', ignore_cleanup_errors=True) as work_dir:
                return await self._run_listeners(work_dir)
        finally:
            self._pid_file.release()

    def _ensure_file_limit(self) -> None:
        # raised only when needed and only as far as needed, since commands inherit it
        limits = self._model.limits
        needed = limits.max_connections + limits.max_running * 3 + 64
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)

        if soft == resource.RLIM_INFINITY or soft >= needed:
            return

        if hard != resource.RLIM_INFINITY and hard < needed:
            self._logger.warning(f'The open files limit ({hard}) is too low for max_connections, connections may fail')
            needed = hard

        resource.setrlimit(resource.RLIMIT_NOFILE, (needed, hard))

    async def _run_listeners(self, work_dir: str) -> bool:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGQUIT):
            loop.add_signal_handler(sig, stop.set)

        loop.add_signal_handler(signal.SIGHUP, self._reload)

        self._relay = RelayService(self._model, work_dir=work_dir, logger=self._logger.getChild('relay'))

        listeners = self._create_listeners()

        try:
            for listener in listeners:
                await listener.start()
        except OSError as e:
            self._logger.error(f'Could not start listening: {e}')
            return False

        self._warn_if_exposed()
        self._logger.info('Service started')

        await stop.wait()

        self._logger.info('Shutting down, waiting for running commands')

        for listener in listeners:
            await listener.stop_accepting()

        await self._relay.drain(self._model.shutdown_timeout)

        for listener in listeners:
            await listener.wait_closed()

        self._logger.info('Service stopped')

        return True

    def _create_listeners(self) -> list[TcpListener | HttpListener]:
        limits = self._model.limits
        limiter = ConnectionLimiter(limits.max_connections, limits.max_connections_per_ip, logger=self._logger.getChild('connections'))
        listeners = []

        if self._model.listeners.tcp:
            listeners.append(TcpListener(self._model.listeners.tcp, self._relay, limiter, max_message_size=limits.max_message_size, logger=self._logger.getChild('tcp')))

        if self._model.listeners.http:
            listeners.append(HttpListener(self._model.listeners.http, self._relay, limiter, max_message_size=limits.max_message_size, logger=self._logger.getChild('http')))

        return listeners

    def _reload(self) -> None:
        try:
            model = self._load_model()
        except (UsCmdRelayRuntimeError, OSError) as e:
            self._logger.error(f'Reload refused, keeping the current config: {e}')
            return

        applied = {'keys', 'clusters', 'on_failure'}

        if model.model_dump(exclude=applied) != self._model.model_dump(exclude=applied):
            self._logger.warning('Listener, limit and pid file changes need a restart to apply')

        self._relay.update(model)
        self._logger.info('Config reloaded')

    def _warn_if_exposed(self) -> None:
        for listener in filter(None, (self._model.listeners.tcp, self._model.listeners.http)):
            if not self._is_local(listener):
                self._logger.warning(f'Listening on non-local address {listener.host}:{listener.port}: keys and data travel unencrypted, use a trusted network')

    def _is_local(self, listener: ListenerModel) -> bool:
        try:
            return ip_address(listener.host).is_loopback
        except ValueError:
            return listener.host == 'localhost'
