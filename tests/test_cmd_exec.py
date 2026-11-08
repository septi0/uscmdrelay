import asyncio
import gc
import time
import pytest
from uscmdrelay.libraries.cmd_exec import CmdExec, CmdExecError

def run(cmd, **kwargs):
    return asyncio.run(CmdExec.exec(cmd, **{'timeout': 10, 'output_limit': 1024 * 1024, **kwargs}))

def is_gone(pid: int) -> bool:
    deadline = time.monotonic() + 5

    while time.monotonic() < deadline:
        try:
            with open(f'/proc/{pid}/stat') as f:
                if f.read().rsplit(')', 1)[1].split()[0] == 'Z':
                    return True
        except FileNotFoundError:
            return True

        time.sleep(0.05)

    return False

def test_captures_output_and_code():
    result = run(['sh', '-c', 'echo out; echo err >&2; exit 3'])

    assert (result.returncode, result.stdout, result.stderr, result.timed_out) == (3, 'out', 'err', False)

def test_passes_stdin():
    assert run(['cat'], input=b'hello').stdout == 'hello'

def test_command_ignoring_large_stdin_finishes(caplog):
    assert run(['true'], input=b'x' * (4 * 1024 * 1024)).returncode == 0

    gc.collect()

    assert 'never retrieved' not in caplog.text

def test_output_is_capped_and_command_still_finishes():
    result = run(['sh', '-c', 'head -c 5000000 /dev/zero; echo done >&2'], output_limit=100)

    assert result.returncode == 0
    assert len(result.stdout) == 100
    assert result.stderr == 'done'

def test_binary_output_is_decoded():
    assert run(['printf', r'\377ok']).stdout == '�ok'

def test_uses_only_given_env():
    assert run(['/usr/bin/env'], env={'ONLY': '1'}).stdout == 'ONLY=1'

@pytest.mark.parametrize('cmd', [['/nonexistent/program'], ['/bin/echo', 'a\0b']])
def test_unstartable_command(cmd):
    with pytest.raises(CmdExecError):
        run(cmd)

def test_timeout_stops_the_whole_process_group(tmp_path):
    pid_file = tmp_path / 'pid'
    started = time.monotonic()

    result = run(['sh', '-c', f'sleep 30 & echo $! > {pid_file}; echo partial; wait'], timeout=1)

    assert result.timed_out
    assert result.stdout == 'partial'
    assert time.monotonic() - started < 5
    assert is_gone(int(pid_file.read_text()))

def test_cancel_stops_the_process_group(tmp_path):
    pid_file = tmp_path / 'pid'

    async def scenario():
        task = asyncio.create_task(CmdExec.exec(['sh', '-c', f'sleep 30 & echo $! > {pid_file}; wait'], timeout=30, output_limit=100))

        await asyncio.sleep(0.5)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert is_gone(int(pid_file.read_text()))

def test_terminate_kills_what_ignores_sigterm():
    async def scenario():
        process = await asyncio.create_subprocess_exec('sh', '-c', 'trap "" TERM; sleep 30', start_new_session=True)

        await asyncio.sleep(0.3)
        await CmdExec._terminate(process, timeout=0.5)

        assert process.returncode is not None

    asyncio.run(scenario())
