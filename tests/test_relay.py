import asyncio
import json
import logging
import pytest
from conftest import LIMITED_TOKEN, TOKEN, build_model
from uscmdrelay.models.response import ResponseCode
from uscmdrelay.models.uscmdrelay import UsCmdRelayModel
from uscmdrelay.services.auth import hash_token
from uscmdrelay.services.relay import RelayService

PRINT_ARGS = {'cmd': '/usr/bin/printf "[%s]\\n" ${x}'}

def message(command='run', auth_key=TOKEN, **fields):
    return json.dumps({'auth_key': auth_key, 'command': command, **fields}).encode()

def call(relay, raw, peer_ip='10.0.0.1', **kwargs):
    return asyncio.run(relay.handle(raw, peer_ip, **kwargs))

def assert_reply(response, expected):
    reply = response.to_dict()

    assert len(reply.pop('run_id')) == 8
    assert reply == expected

def test_runs_command(make_relay):
    response = call(make_relay({'run': {'cmd': '/bin/echo hi'}}), message())

    assert_reply(response, {'status': 'ok', 'message': 'hi', 'code': 0, 'exit_code': 0})

@pytest.mark.parametrize('raw', [
    message(auth_key='wrong'),
    message(auth_key=123),
    json.dumps({'command': 'run'}).encode(),
    b'',
])
def test_failed_auth(make_relay, raw):
    assert call(make_relay({'run': {'cmd': '/bin/true'}}), raw).code == ResponseCode.AUTH_FAILED

def test_key_without_access_looks_like_a_wrong_key(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/true'}})

    wrong = call(relay, message(auth_key='wrong'))
    no_access = call(relay, message(auth_key=LIMITED_TOKEN))
    unknown_cluster = call(relay, message('nope::run', auth_key=LIMITED_TOKEN))

    assert wrong.to_dict() == no_access.to_dict() == unknown_cluster.to_dict()

def test_unknown_cluster_and_command(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/true'}})

    assert call(relay, message('nope::run')).code == ResponseCode.UNKNOWN_CLUSTER
    assert call(relay, message('nope')).code == ResponseCode.UNKNOWN_COMMAND

def test_allowed_ips():
    model = UsCmdRelayModel.model_validate({
        'listeners': {'tcp': {'port': 1}},
        'keys': [{'name': 'k', 'hash': f'sha256:{hash_token(TOKEN)}', 'clusters': ['*'], 'allowed_ips': ['10.0.0.5', '192.168.1.0/24']}],
        'clusters': {'default': {'run': {'cmd': '/bin/true'}}},
    })
    relay = RelayService(model, work_dir='/tmp', logger=logging.getLogger('test'))

    assert call(relay, message(), peer_ip='10.0.0.5').code == 0
    assert call(relay, message(), peer_ip='192.168.1.77').code == 0
    assert call(relay, message(), peer_ip='::ffff:10.0.0.5').code == 0
    assert call(relay, message(), peer_ip='10.0.0.6').code == ResponseCode.AUTH_FAILED

def test_value_stays_in_one_argument(make_relay):
    relay = make_relay({'run': {**PRINT_ARGS, 'fields': {'x': '.*'}}})

    assert call(relay, message(x='a b; rm -rf / $(id)')).message == '[a b; rm -rf / $(id)]'

def test_nested_fields_and_arrays(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/echo ${repo.name} ${list.1}'}})

    assert call(relay, message(repo={'name': 'app'}, list=['a', 'b'])).message == 'app b'

def test_scalars_become_text(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/echo ${a} ${b} ${c}'}})

    assert call(relay, message(a=5, b=True, c=1.5)).message == '5 true 1.5'

@pytest.mark.parametrize('value', ['myorg/app', 'refs/heads/main', 'v1.2.3', 'a:b@c+d', '_x', 'abc123'])
def test_default_pattern_accepts(make_relay, value):
    assert call(make_relay({'run': PRINT_ARGS}), message(x=value)).message == f'[{value}]'

@pytest.mark.parametrize('value', ['-rf', '--upload-pack=x', '/etc/passwd', '.env', '../up', 'a/../b', 'a b', 'a;b', '$(id)', 'a\nb', ''])
def test_default_pattern_refuses(make_relay, value):
    assert call(make_relay({'run': PRINT_ARGS}), message(x=value)).code == ResponseCode.INVALID_FIELD

@pytest.mark.parametrize('fields', [{}, {'x': None}, {'x': {'a': 1}}, {'x': [1]}])
def test_missing_or_unusable_field(make_relay, fields):
    assert call(make_relay({'run': PRINT_ARGS}), message(**fields)).code == ResponseCode.INVALID_FIELD

def test_invalid_field_message_does_not_echo_value(make_relay):
    assert 'SECRETVALUE!' not in call(make_relay({'run': PRINT_ARGS}), message(x='SECRETVALUE!')).message

def test_regex_must_match_whole_value(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/true', 'fields': {'x': 'abc'}}})

    assert call(relay, message(x='abc')).code == 0
    assert call(relay, message(x='abcd')).code == ResponseCode.INVALID_FIELD

def test_listed_field_gates_even_if_unused(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/true', 'fields': {'ref': '^refs/heads/main$'}}})

    assert call(relay, message(ref='refs/heads/main')).code == 0
    assert call(relay, message(ref='refs/heads/dev')).code == ResponseCode.INVALID_FIELD
    assert call(relay, message()).code == ResponseCode.INVALID_FIELD

def test_legacy_arguments_field(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/echo ${arguments.0}'}})

    assert call(relay, message(arguments=['first'])).message == 'first'

def test_env_format(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/echo ${repo}', 'format': 'env'}})

    assert call(relay, f'auth_key={TOKEN}\ncommand=run\nrepo=app\n\n'.encode()).message == 'app'

def test_format_must_match(make_relay):
    relay = make_relay({'j': {'cmd': '/bin/true'}, 'e': {'cmd': '/bin/true', 'format': 'env'}})

    assert call(relay, f'auth_key={TOKEN}\ncommand=j\n'.encode()).code == ResponseCode.WRONG_FORMAT
    assert call(relay, message('e')).code == ResponseCode.WRONG_FORMAT

def test_stdin_has_no_envelope(make_relay):
    response = call(make_relay({'run': {'cmd': '/bin/cat'}}), message(data='x'))

    assert json.loads(response.message) == {'data': 'x'}

def test_env_stdin_has_no_envelope(make_relay):
    response = call(make_relay({'run': {'cmd': '/bin/cat', 'format': 'env'}}), f'auth_key={TOKEN}\ncommand=run\na=1\n'.encode())

    assert response.message == 'a=1'

def test_stdin_keeps_raw_body_when_nothing_is_removed(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/cat'}})
    key = relay.authenticate(TOKEN, '10.0.0.1')
    body = b'{"command":  "kept",   "a": 1}'

    assert call(relay, body, key=key, target=('default', 'run')).message == body.decode()

def test_body_key_is_removed_from_stdin_when_url_routes(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/cat'}})

    response = call(relay, json.dumps({'auth_key': TOKEN, 'command': 'kept'}).encode(), target=('default', 'run'))

    assert json.loads(response.message) == {'command': 'kept'}

def test_minimal_environment(make_relay, monkeypatch):
    monkeypatch.setenv('SECRET_FROM_SERVICE', '1')

    response = call(make_relay({'run': {'cmd': '/usr/bin/env', 'env': {'MINE': 'x'}}}), message())

    assert 'SECRET_FROM_SERVICE' not in response.message
    assert 'MINE=x' in response.message

def test_failed_command(make_relay):
    response = call(make_relay({'run': {'cmd': '''sh -c 'echo bad >&2; exit 4' '''}}), message())

    assert_reply(response, {'status': 'error', 'message': 'bad', 'code': 3001, 'exit_code': 4})

def test_failed_command_without_stderr(make_relay):
    assert call(make_relay({'run': {'cmd': '/bin/false'}}), message()).message == 'exited with code 1'

def test_hide_output(make_relay):
    relay = make_relay({'ok': {'cmd': '/bin/echo secret', 'hide_output': True}, 'bad': {'cmd': '''sh -c 'echo secret >&2; exit 1' ''', 'hide_output': True}})

    assert call(relay, message('ok')).message == 'Command succeeded'
    assert call(relay, message('bad')).message == 'Command failed'

def test_timeout(make_relay):
    response = call(make_relay({'run': {'cmd': '/bin/sleep 30', 'timeout': 1}}), message())

    assert response.code == ResponseCode.COMMAND_TIMEOUT

def test_unstartable_command_reveals_no_path(make_relay):
    response = call(make_relay({'run': {'cmd': '/secret/path/program'}}), message())

    assert response.code == ResponseCode.COMMAND_FAILED
    assert '/secret' not in response.message

def test_empty_message_with_prior_auth(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/echo ok'}})
    key = relay.authenticate(TOKEN, '10.0.0.1')

    assert call(relay, b'', key=key, target=('default', 'run')).message == 'ok'

def test_cap_refuses_normal_commands(make_relay):
    relay = make_relay({'slow': {'cmd': '/bin/sleep 1'}, 'quick': {'cmd': '/bin/true'}}, limits={'max_running': 1})

    async def scenario():
        slow = asyncio.create_task(relay.handle(message('slow'), '10.0.0.1'))
        await asyncio.sleep(0.2)
        refused = await relay.handle(message('quick'), '10.0.0.1')
        return refused, await slow

    refused, slow = asyncio.run(scenario())

    assert refused.code == ResponseCode.BUSY
    assert slow.code == 0

def background_script(log):
    return {'cmd': f'''sh -c 'cat >> {log}; echo >> {log}; sleep 0.4' ''', 'background': True}

def test_background_replies_immediately_and_runs(make_relay, tmp_path):
    log = tmp_path / 'log'
    relay = make_relay({'run': background_script(log)})

    async def scenario():
        response = await relay.handle(message(n=1), '10.0.0.1')
        assert not log.exists()
        await asyncio.gather(*relay._background_tasks)
        return response

    response = asyncio.run(scenario())

    assert_reply(response, {'status': 'accepted', 'message': 'Command accepted', 'code': 0})
    assert json.loads(log.read_text()) == {'n': 1}

def test_background_keeps_one_pending_with_newest_payload(make_relay, tmp_path):
    log = tmp_path / 'log'
    relay = make_relay({'run': background_script(log)})

    async def scenario():
        await relay.handle(message(n=1), '10.0.0.1')
        await asyncio.sleep(0.15)

        for n in (2, 3, 4):
            await relay.handle(message(n=n), '10.0.0.1')

        await asyncio.gather(*relay._background_tasks)

        assert relay._background == {}

    asyncio.run(scenario())

    assert [json.loads(line)['n'] for line in log.read_text().splitlines()] == [1, 4]

def test_background_different_arguments_run_in_parallel(make_relay, tmp_path):
    relay = make_relay({'run': {'cmd': f'{tmp_path}/s.sh ${{x}}', 'background': True}})
    script = tmp_path / 's.sh'
    script.write_text(f'#!/bin/sh\necho start >> {tmp_path}/$1.a; sleep 0.5; echo end >> {tmp_path}/$1.b\n')
    script.chmod(0o755)

    async def scenario():
        await relay.handle(message(x='a'), '10.0.0.1')
        await relay.handle(message(x='b'), '10.0.0.1')
        await asyncio.sleep(0.3)

        started = [(tmp_path / 'a.a').exists(), (tmp_path / 'b.a').exists()]
        finished = [(tmp_path / 'a.b').exists(), (tmp_path / 'b.b').exists()]

        await asyncio.gather(*relay._background_tasks)

        return started, finished

    assert asyncio.run(scenario()) == ([True, True], [False, False])

def test_background_waits_for_a_slot(make_relay, tmp_path):
    log = tmp_path / 'log'
    relay = make_relay({'slow': {'cmd': '/bin/sleep 0.5'}, 'run': {'cmd': f'''sh -c 'date > {log}' ''', 'background': True}}, limits={'max_running': 1})

    async def scenario():
        slow = asyncio.create_task(relay.handle(message('slow'), '10.0.0.1'))
        await asyncio.sleep(0.1)

        accepted = await relay.handle(message('run'), '10.0.0.1')
        assert accepted.status == 'accepted'
        assert not log.exists()

        await slow
        await asyncio.gather(*relay._background_tasks)

    asyncio.run(scenario())

    assert log.exists()

def test_background_queue_is_limited(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/sleep 0.3 ${x}', 'background': True}}, limits={'max_queued': 2})

    async def scenario():
        first = await relay.handle(message(x='1'), '10.0.0.1')
        second = await relay.handle(message(x='2'), '10.0.0.1')
        third = await relay.handle(message(x='3'), '10.0.0.1')
        repeat = await relay.handle(message(x='1'), '10.0.0.1')

        await asyncio.gather(*relay._background_tasks)

        return [first.status, second.status, third.code, repeat.status]

    assert asyncio.run(scenario()) == ['accepted', 'accepted', ResponseCode.BUSY, 'accepted']

def test_background_failure_does_not_stop_the_follow_up(make_relay, tmp_path):
    log = tmp_path / 'log'
    relay = make_relay({'run': {'cmd': f'''sh -c 'echo x >> {log}; sleep 0.3; exit 1' ''', 'background': True}})

    async def scenario():
        await relay.handle(message(), '10.0.0.1')
        await asyncio.sleep(0.1)
        await relay.handle(message(), '10.0.0.1')
        await asyncio.gather(*relay._background_tasks)

    asyncio.run(scenario())

    assert log.read_text().count('x') == 2

def test_drain_lets_commands_finish_and_refuses_new_work(make_relay):
    relay = make_relay({'run': {'cmd': '''sh -c 'sleep 0.5; echo finished' '''}})

    async def scenario():
        running = asyncio.create_task(relay.handle(message(), '10.0.0.1'))
        await asyncio.sleep(0.1)

        drain = asyncio.create_task(relay.drain(5))
        await asyncio.sleep(0.05)

        refused = await relay.handle(message(), '10.0.0.1')
        await drain

        return refused, await running

    refused, finished = asyncio.run(scenario())

    assert refused.code == ResponseCode.BUSY
    assert finished.message == 'finished'

def test_drain_terminates_after_timeout(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/sleep 30'}})

    async def scenario():
        running = asyncio.create_task(relay.handle(message(), '10.0.0.1'))
        await asyncio.sleep(0.2)
        await relay.drain(0)
        return await running

    assert asyncio.run(scenario()).code == ResponseCode.BUSY

def test_drain_drops_pending_background(make_relay, tmp_path):
    log = tmp_path / 'log'
    relay = make_relay({'run': {'cmd': f'''sh -c 'echo x >> {log}; sleep 0.3' ''', 'background': True}})

    async def scenario():
        await relay.handle(message(), '10.0.0.1')
        await asyncio.sleep(0.1)
        await relay.handle(message(), '10.0.0.1')
        await relay.drain(5)

        assert relay._background == {}

    asyncio.run(scenario())

    assert log.read_text().count('x') == 1

def test_update_applies_new_keys_and_clusters(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/true'}})

    assert call(relay, message()).code == 0

    relay.update(build_model({'default': {}}))

    assert call(relay, message()).code == ResponseCode.UNKNOWN_COMMAND

def test_key_is_not_logged(make_relay, caplog):
    caplog.set_level(logging.DEBUG)

    relay = make_relay({'run': {'cmd': '/bin/cat'}})
    call(relay, message())
    call(relay, message(auth_key='wrong-secret-token'))

    assert TOKEN not in caplog.text
    assert 'wrong-secret-token' not in caplog.text

@pytest.mark.parametrize('raw,reason', [
    (json.dumps({'command': 'run'}).encode(), 'no key provided'),
    (message(auth_key='wrong'), 'unknown key'),
])
def test_failed_auth_reason_is_logged(make_relay, caplog, raw, reason):
    caplog.set_level(logging.WARNING)

    assert call(make_relay({'run': {'cmd': '/bin/true'}}), raw).message == 'Authentication failed'
    assert f'Authentication failed from 10.0.0.1: {reason}' in caplog.text

def test_blocked_address_is_logged_but_looks_like_a_wrong_key(caplog):
    caplog.set_level(logging.WARNING)

    model = UsCmdRelayModel.model_validate({
        'listeners': {'tcp': {'port': 1}},
        'keys': [{'name': 'k', 'hash': f'sha256:{hash_token(TOKEN)}', 'clusters': ['*'], 'allowed_ips': ['10.0.0.5']}],
        'clusters': {'default': {'run': {'cmd': '/bin/true'}}},
    })
    relay = RelayService(model, work_dir='/tmp', logger=logging.getLogger('test'))

    blocked = call(relay, message(), peer_ip='10.0.0.6')

    assert blocked.to_dict() == call(relay, message(auth_key='wrong'), peer_ip='10.0.0.7').to_dict()
    assert 'key "k" is not allowed from this address' in caplog.text

def test_log_output_writes_stdout_and_stderr(make_relay, caplog):
    caplog.set_level(logging.INFO)

    call(make_relay({'run': {'cmd': '''sh -c 'echo visible; echo problem >&2' ''', 'log_output': True}}), message())

    assert "stdout='visible'" in caplog.text
    assert "stderr='problem'" in caplog.text

def test_output_is_not_logged_by_default(make_relay, caplog):
    caplog.set_level(logging.INFO)

    call(make_relay({'run': {'cmd': '/bin/echo hidden'}}), message())

    assert 'hidden' not in caplog.text.replace('/bin/echo", "hidden', '')
