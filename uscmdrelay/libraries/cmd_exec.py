import asyncio
import logging
import os
import signal
from dataclasses import dataclass

__all__ = ['CmdExec', 'CmdExecError', 'CmdResult']

class CmdExecError(Exception):
    pass

@dataclass(frozen=True)
class CmdResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False

class CmdExec:
    _chunk_size: int = 64 * 1024
    _output_grace: float = 2
    _exit_poll_interval: float = 0.05
    _lingering: set[asyncio.Future] = set()

    @classmethod
    async def exec(cls, cmd: list[str], *, input: bytes = b'', env: dict[str, str] | None = None, cwd: str | None = None, timeout: float, output_limit: int) -> CmdResult:
        logging.debug(f'Executing command: {cmd}')

        # its own process group, so its children can be stopped with it
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
                start_new_session=True,
            )
        except (OSError, ValueError) as e:
            raise CmdExecError(str(e))

        stdout = bytearray()
        stderr = bytearray()

        io_tasks = [
            asyncio.ensure_future(cls._feed(process.stdin, input)),
            asyncio.ensure_future(cls._drain(process.stdout, stdout, output_limit)),
            asyncio.ensure_future(cls._drain(process.stderr, stderr, output_limit)),
        ]

        # process.wait() can also wait for the pipes to close, which a child left running may never do
        exit_tasks = [asyncio.ensure_future(process.wait()), asyncio.ensure_future(cls._poll_exit(process))]

        try:
            exited, _ = await asyncio.wait(exit_tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            timed_out = not exited

            if timed_out:
                await cls._terminate(process)

            await asyncio.wait(io_tasks, timeout=cls._output_grace)
        except asyncio.CancelledError:
            await cls._terminate(process)
            raise
        finally:
            for task in exit_tasks:
                task.cancel()

            cls._keep_draining(io_tasks)

        return CmdResult(process.returncode, cls._decode(stdout), cls._decode(stderr), timed_out)

    @classmethod
    async def _poll_exit(cls, process: asyncio.subprocess.Process) -> None:
        while process.returncode is None:
            await asyncio.sleep(cls._exit_poll_interval)

    @classmethod
    def _keep_draining(cls, tasks: list[asyncio.Future]) -> None:
        # a child left running must not block on a full pipe
        for task in tasks:
            if not task.done():
                cls._lingering.add(task)
                task.add_done_callback(cls._lingering.discard)

    @classmethod
    async def _feed(cls, stdin: asyncio.StreamWriter, data: bytes) -> None:
        # commands are free to exit without reading their input
        try:
            if data:
                stdin.write(data)
                await stdin.drain()

            stdin.close()
            await stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            # python 3.10 logs the failure as never retrieved unless it is awaited here too
            try:
                await stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    @classmethod
    async def _drain(cls, stream: asyncio.StreamReader, buffer: bytearray, limit: int) -> None:
        # output over the limit is still read, so the command never blocks on a full pipe
        while chunk := await stream.read(cls._chunk_size):
            buffer += chunk[:max(limit - len(buffer), 0)]

    @classmethod
    async def _terminate(cls, process: asyncio.subprocess.Process, timeout: float = 10) -> None:
        cls._signal_group(process, signal.SIGTERM)

        if await cls._wait(process, timeout):
            return

        cls._signal_group(process, signal.SIGKILL)

        await cls._wait(process, 5)

    @classmethod
    async def _wait(cls, process: asyncio.subprocess.Process, timeout: float) -> bool:
        try:
            await asyncio.wait_for(process.wait(), timeout)
        except asyncio.TimeoutError:
            return False

        return True

    @classmethod
    def _signal_group(cls, process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

    @classmethod
    def _decode(cls, output: bytearray) -> str:
        return output.decode('utf-8', errors='replace').strip()
