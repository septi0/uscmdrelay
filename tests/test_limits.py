import asyncio
import json
import logging
import os
import socket
import time
import pytest
from conftest import TOKEN
from test_listeners import BEARER, http_call, http_exchange, tcp_exchange, with_listeners
from uscmdrelay.libraries.cmd_exec import CmdExec
from uscmdrelay.libraries.connection_limiter import ConnectionLimiter

PLAIN = json.dumps({'auth_key': TOKEN, 'command': 'plain'}).encode() + b'\n'

async def is_refused(port) -> bool:
    reader, writer = await asyncio.open_connection('127.0.0.1', port)

    try:
        return await asyncio.wait_for(reader.read(), 2) == b''
    except ConnectionError:
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        writer.close()

async def hold(port, count):
    connections = [await asyncio.open_connection('127.0.0.1', port) for _ in range(count)]
    await asyncio.sleep(0.1)

    return connections

def limiter(total, per_ip):
    return ConnectionLimiter(total, per_ip, logger=logging.getLogger('test'))

def test_limiter_counts_per_address():
    limits = limiter(10, 2)

    assert limits.acquire('a') and limits.acquire('a')
    assert not limits.acquire('a')
    assert limits.acquire('b')

    limits.release('a')

    assert limits.acquire('a')

def test_limiter_counts_in_total():
    limits = limiter(2, 5)

    assert limits.acquire('a') and limits.acquire('b')
    assert not limits.acquire('c')

    limits.release('a')
    limits.release('b')

    assert limits.total == 0

def test_refused_connections_do_not_flood_the_log(caplog):
    limits = limiter(1, 1)
    limits.acquire('a')

    with caplog.at_level(logging.WARNING):
        for _ in range(1000):
            limits.acquire('b')

    assert len(caplog.records) == 1

@pytest.mark.parametrize('listener', ['tcp', 'http'])
def test_per_address_cap(listener):
    async def scenario(tcp, http):
        port = tcp if listener == 'tcp' else http
        connections = await hold(port, 2)
        refused = await is_refused(port)

        connections[0][1].close()
        await asyncio.sleep(0.1)

        accepted_again = not await is_refused(port)

        connections[1][1].close()

        return refused, accepted_again

    assert asyncio.run(with_listeners(scenario, limits={'max_connections_per_ip': 2})) == (True, True)

def test_cap_is_shared_by_both_listeners():
    async def scenario(tcp, http):
        tcp_connections = await hold(tcp, 1)
        http_connections = await hold(http, 1)
        refused = await is_refused(http)

        for _, writer in [*tcp_connections, *http_connections]:
            writer.close()

        return refused

    assert asyncio.run(with_listeners(scenario, limits={'max_connections': 2}))

def test_slots_are_released_after_requests():
    async def scenario(tcp, http):
        for _ in range(8):
            assert (await tcp_exchange(tcp, PLAIN))['code'] == 0

        status, _, _ = await http_exchange(http, '/run/default/plain', headers=BEARER)

        return status

    assert asyncio.run(with_listeners(scenario, limits={'max_connections_per_ip': 2})) == 200

def test_malformed_http_reveals_no_version():
    async def scenario(tcp, http):
        reader, writer = await asyncio.open_connection('127.0.0.1', http)
        writer.write(b'GARBAGE\r\n\r\n')
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), 5)
        writer.close()

        return data

    data = asyncio.run(with_listeners(scenario))

    assert b'aiohttp' not in data.lower()
    assert b'python/' not in data.lower()

def test_http_header_reaches_field():
    status, body, _ = http_call('/run/default/header', headers={**BEARER, 'X-Event': 'push'})

    assert (status, body['message']) == (200, 'push')

def request_bytes(listener):
    if listener == 'tcp':
        return json.dumps({'auth_key': TOKEN, 'command': 'flood'}).encode() + b'\n'

    return f'POST /run/default/flood HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\nContent-Length: 0\r\n\r\n'.encode()

@pytest.mark.parametrize('listener', ['tcp', 'http'])
def test_client_that_stops_reading_is_disconnected(monkeypatch, listener):
    monkeypatch.setattr(f'uscmdrelay.listeners.{listener}.WRITE_TIMEOUT', 0.5)

    async def scenario(tcp, http):
        loop = asyncio.get_running_loop()
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.setblocking(False)

        await loop.sock_connect(sock, ('127.0.0.1', tcp if listener == 'tcp' else http))
        await loop.sock_sendall(sock, request_bytes(listener))
        await asyncio.sleep(1.5)

        received = 0

        try:
            while data := await asyncio.wait_for(loop.sock_recv(sock, 65536), 5):
                received += len(data)
        except ConnectionError:
            pass
        finally:
            sock.close()

        return received

    assert asyncio.run(with_listeners(scenario)) < 6_000_000

def run_exec(cmd, **kwargs):
    return asyncio.run(CmdExec.exec(cmd, **{'timeout': 10, 'output_limit': 1024 * 1024, **kwargs}))

def test_real_exit_code_when_a_child_keeps_the_output_open(tmp_path):
    pid_file = tmp_path / 'pid'
    started = time.monotonic()

    result = run_exec(['sh', '-c', f'sleep 30 & echo $! > {pid_file}; echo started; exit 3'], timeout=20)

    assert (result.returncode, result.stdout, result.timed_out) == (3, 'started', False)
    assert time.monotonic() - started < 5

    pid = int(pid_file.read_text())

    os.kill(pid, 0)
    os.kill(pid, 9)

def test_cwd_is_used():
    assert run_exec(['pwd'], cwd='/').stdout == '/'

def test_normal_command_returns_without_delay():
    started = time.monotonic()

    assert run_exec(['true']).returncode == 0
    assert time.monotonic() - started < 1
