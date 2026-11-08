import asyncio
import json
import logging
import aiohttp
import pytest
from conftest import LIMITED_TOKEN, TOKEN, build_model, free_ports
from uscmdrelay.libraries.connection_limiter import ConnectionLimiter
from uscmdrelay.listeners.http import HttpListener
from uscmdrelay.listeners.tcp import TcpListener
from uscmdrelay.services.relay import RelayService

COMMANDS = {
    'echo': {'cmd': '/bin/echo ${x}'},
    'cat': {'cmd': '/bin/cat'},
    'plain': {'cmd': '/bin/echo plain'},
    'env': {'cmd': '/bin/echo ${x}', 'format': 'env'},
    'bg': {'cmd': '/bin/true', 'background': True},
    'bad': {'cmd': '/bin/false'},
    'header': {'cmd': '/bin/echo ${headers.x-event}'},
    'flood': {'cmd': 'head -c 1000000 /dev/zero'},
    'when': {'cmd': '/bin/true', 'when': {'ref': 'refs/tags/.+'}},
}
MAX_SIZE = 4096

async def with_listeners(scenario, limits=None):
    tcp_port, http_port = free_ports(2)
    model = build_model({'default': COMMANDS}, limits={'max_message_size': MAX_SIZE, **(limits or {})}, tcp=tcp_port, http=http_port)
    relay = RelayService(model, work_dir='/tmp', logger=logging.getLogger('test'))
    logger = logging.getLogger('test')
    limiter = ConnectionLimiter(model.limits.max_connections, model.limits.max_connections_per_ip, logger=logger)

    tcp = TcpListener(model.listeners.tcp, relay, limiter, max_message_size=MAX_SIZE, logger=logger)
    http = HttpListener(model.listeners.http, relay, limiter, max_message_size=MAX_SIZE, logger=logger)

    await tcp.start()
    await http.start()

    try:
        return await scenario(tcp_port, http_port)
    finally:
        await tcp.stop_accepting()
        await http.stop_accepting()
        await relay.drain(5)
        await tcp.wait_closed()
        await http.wait_closed()

def run(scenario):
    return asyncio.run(with_listeners(scenario))

async def tcp_exchange(port, data, *, close=False):
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    writer.write(data)
    await writer.drain()

    if close:
        writer.write_eof()

    line = await asyncio.wait_for(reader.readline(), 15)
    writer.close()

    return json.loads(line)

def tcp_call(data, **kwargs):
    return run(lambda tcp, http: tcp_exchange(tcp, data, **kwargs))

async def http_exchange(port, path='/run/default/echo', *, method='POST', headers=None, data=None):
    async with aiohttp.ClientSession() as session:
        async with session.request(method, f'http://127.0.0.1:{port}{path}', headers=headers, data=data) as response:
            return response.status, await response.json(), response.headers

def http_call(path='/run/default/echo', **kwargs):
    return run(lambda tcp, http: http_exchange(http, path, **kwargs))

BEARER = {'Authorization': f'Bearer {TOKEN}'}

def test_tcp_json():
    body = json.dumps({'auth_key': TOKEN, 'command': 'default::echo', 'x': 'hi'}).encode() + b'\n'

    reply = tcp_call(body)

    assert (reply['status'], reply['message'], reply['code'], reply['exit_code']) == ('ok', 'hi', 0, 0)

def test_tcp_default_cluster():
    assert tcp_call(json.dumps({'auth_key': TOKEN, 'command': 'plain'}).encode() + b'\n')['message'] == 'plain'

def test_tcp_message_ending_at_close():
    assert tcp_call(json.dumps({'auth_key': TOKEN, 'command': 'plain'}).encode(), close=True)['code'] == 0

def test_tcp_env_ends_at_blank_line():
    assert tcp_call(f'auth_key={TOKEN}\ncommand=env\nx=hi\n\n'.encode())['message'] == 'hi'

def test_tcp_env_ends_at_close():
    assert tcp_call(f'auth_key={TOKEN}\ncommand=env\nx=hi\n'.encode(), close=True)['message'] == 'hi'

def test_tcp_failed_auth():
    assert tcp_call(b'{"auth_key":"no","command":"plain"}\n')['code'] == 1000

def test_tcp_garbage_gets_a_reply():
    assert tcp_call(b'not a message\n\n')['code'] == 2099

@pytest.mark.parametrize('raw', [b'123\n\n', b'null\n\n', b'[1]\n\n'])
def test_tcp_non_object_gets_a_reply(raw):
    assert tcp_call(raw)['code'] == 2099

def test_tcp_oversized_message():
    assert tcp_call(b'{"a":"' + b'x' * (MAX_SIZE * 2) + b'"}\n')['code'] == 2099

def test_tcp_many_lines_over_limit():
    assert tcp_call(b'a=b\n' * MAX_SIZE)['code'] == 2099

def test_http_bearer():
    status, body, _ = http_call(headers=BEARER, data=b'{"x": "hi"}')

    assert status == 200
    assert (body['status'], body['message'], body['code'], body['exit_code']) == ('ok', 'hi', 0, 0)

def test_http_bearer_is_case_insensitive():
    assert http_call(headers={'Authorization': f'bearer {TOKEN}'}, data=b'{"x": "hi"}')[0] == 200

def test_http_falls_back_to_body_key():
    status, body, _ = http_call(data=json.dumps({'auth_key': TOKEN, 'x': 'hi'}).encode())

    assert (status, body['message']) == (200, 'hi')

def test_http_body_fallback_with_env_format():
    status, body, _ = http_call('/run/default/env', data=f'auth_key={TOKEN}\nx=hi\n'.encode())

    assert (status, body['message']) == (200, 'hi')

def test_http_invalid_bearer_does_not_fall_back():
    status, body, _ = http_call(headers={'Authorization': 'Bearer wrong'}, data=json.dumps({'auth_key': TOKEN, 'x': 'hi'}).encode())

    assert (status, body['code']) == (401, 1000)

def test_http_empty_bearer_does_not_fall_back():
    assert http_call(headers={'Authorization': 'Bearer '}, data=json.dumps({'auth_key': TOKEN, 'x': 'hi'}).encode())[0] == 401

def test_http_other_scheme_uses_body():
    assert http_call(headers={'Authorization': 'Basic abc'}, data=json.dumps({'auth_key': TOKEN, 'x': 'hi'}).encode())[0] == 200

def test_http_key_in_url_is_ignored():
    assert http_call(f'/run/default/plain?auth_key={TOKEN}&token={TOKEN}')[0] == 401

def test_http_bearer_authenticates_before_body_is_parsed():
    status, body, _ = http_call(headers={'Authorization': 'Bearer wrong'}, data=b'{malformed')

    assert (status, body['code']) == (401, 1000)

def test_http_unauthenticated_cannot_probe_commands():
    known = http_call('/run/default/echo')
    unknown = http_call('/run/nope/nothing')

    assert known[0] == unknown[0] == 401
    assert known[1] == unknown[1]

def test_http_body_key_never_reaches_stdin():
    _, body, _ = http_call('/run/default/cat', data=json.dumps({'auth_key': TOKEN, 'a': 1}).encode())

    assert json.loads(body['message']) == {'a': 1}

def test_http_empty_body():
    status, body, _ = http_call('/run/default/plain', headers=BEARER)

    assert (status, body['message']) == (200, 'plain')

@pytest.mark.parametrize('path,headers,data,status,code', [
    ('/run/nope/x', BEARER, None, 404, 2000),
    ('/run/default/nope', BEARER, None, 404, 2001),
    ('/run/default/echo', BEARER, b'{"x": "-bad"}', 422, 2002),
    ('/run/default/echo', BEARER, None, 422, 2002),
    ('/run/default/echo', BEARER, b'x=hi', 400, 2003),
    ('/run/default/echo', BEARER, b'{malformed', 400, 2099),
    ('/run/default/echo', BEARER, b'{"x": "' + b'a' * MAX_SIZE + b'"}', 413, 2099),
    ('/run/default/bad', BEARER, None, 500, 3001),
    ('/run/default/plain', {'Authorization': f'Bearer {LIMITED_TOKEN}'}, None, 401, 1000),
])
def test_http_status_mapping(path, headers, data, status, code):
    response = http_call(path, headers=headers, data=data)

    assert (response[0], response[1]['code']) == (status, code)

def test_http_background_returns_202():
    status, body, _ = http_call('/run/default/bg', headers=BEARER)

    assert (status, body['status']) == (202, 'accepted')

@pytest.mark.parametrize('method,path,status', [('GET', '/run/default/plain', 405), ('POST', '/other', 404), ('GET', '/', 404)])
def test_http_other_routes_answer_json(method, path, status):
    response = http_call(path, method=method)

    assert response[0] == status
    assert response[1]['status'] == 'error'

def test_http_hides_server_version():
    assert http_call('/run/default/plain', headers=BEARER)[2]['Server'] == 'uscmdrelay'

@pytest.mark.parametrize('listener', ['tcp', 'http'])
def test_silent_client_is_disconnected(monkeypatch, listener):
    monkeypatch.setattr(f'uscmdrelay.listeners.{listener}.READ_TIMEOUT', 0.5)

    async def scenario(tcp, http):
        reader, writer = await asyncio.open_connection('127.0.0.1', tcp if listener == 'tcp' else http)
        await asyncio.wait_for(reader.read(), 5)
        writer.close()

        return True

    assert run(scenario)

def test_concurrent_requests_are_all_answered():
    async def scenario(tcp, http):
        body = json.dumps({'auth_key': TOKEN, 'command': 'plain'}).encode() + b'\n'
        return await asyncio.gather(*[tcp_exchange(tcp, body) for _ in range(10)])

    assert all(response['code'] == 0 for response in run(scenario))
