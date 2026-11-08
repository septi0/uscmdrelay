import asyncio
import json
import logging
import pytest
from pydantic import ValidationError
from conftest import build_model
from test_relay import call, message
from uscmdrelay.exceptions import RelayError
from uscmdrelay.models.command import CommandModel, HookModel
from uscmdrelay.models.response import ResponseCode

ECHO = {'cmd': '/bin/echo ${x}'}

def script(path, body):
    path.write_text(f'#!/bin/sh\n{body}\n')
    path.chmod(0o755)

    return str(path)

async def settle(relay):
    while relay._background_tasks:
        await asyncio.gather(*relay._background_tasks)

@pytest.mark.parametrize('value', ['file:///etc/passwd', 'file:/etc/passwd', 'http://x/y', 'a:/b'])
def test_default_pattern_refuses_urls(make_relay, value):
    assert call(make_relay({'run': ECHO}), message(x=value)).code == ResponseCode.INVALID_FIELD

@pytest.mark.parametrize('value', ['myorg/app:1.2', 'registry:5000/app', 'a:b'])
def test_default_pattern_accepts_image_names(make_relay, value):
    assert call(make_relay({'run': ECHO}), message(x=value)).message == value

def test_commands_run_in_the_private_directory(make_relay, tmp_path):
    assert call(make_relay({'run': {'cmd': '/bin/pwd'}}), message()).message == str(tmp_path)

def test_relative_value_cannot_reach_system_files(make_relay):
    response = call(make_relay({'run': {'cmd': '/bin/cat ${file}'}}), message(file='etc/hostname'))

    assert response.code == ResponseCode.COMMAND_FAILED

def test_command_can_choose_its_directory(make_relay):
    assert call(make_relay({'run': {'cmd': '/bin/pwd', 'cwd': '/'}}), message()).message == '/'

def test_missing_directory_fails_cleanly(make_relay):
    response = call(make_relay({'run': {'cmd': '/bin/pwd', 'cwd': '/nonexistent/dir'}}), message())

    assert response.code == ResponseCode.COMMAND_FAILED
    assert '/nonexistent' not in response.message

def test_cwd_must_be_absolute():
    with pytest.raises(ValidationError):
        CommandModel(cmd='/bin/x', cwd='relative')

def test_field_size_limit_applies_to_custom_patterns(make_relay):
    relay = make_relay({'run': {**ECHO, 'fields': {'x': '.*'}}}, limits={'max_field_size': 10})

    assert call(relay, message(x='a' * 10)).code == 0
    assert call(relay, message(x='a' * 11)).code == ResponseCode.INVALID_FIELD

@pytest.mark.parametrize('cmd', ['env ${x}', '/usr/bin/sudo ${x}', 'nohup ${x}', 'nice --adjustment=5 ${x}'])
def test_wrapper_cannot_run_a_value(cmd):
    with pytest.raises(ValidationError):
        CommandModel(cmd=cmd)

def test_wrapper_can_pass_values_to_its_program():
    assert CommandModel(cmd='sudo /opt/x.sh ${y}').placeholders == ['y']

def test_default_used_when_missing(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/echo ${branch}', 'fields': {'branch': {'pattern': '[a-z]+', 'default': 'main'}}}})

    assert call(relay, message()).message == 'main'
    assert call(relay, message(branch='dev')).message == 'dev'
    assert call(relay, message(branch='DEV')).code == ResponseCode.INVALID_FIELD

def test_default_without_pattern_uses_safe_pattern(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/echo ${x}', 'fields': {'x': {'default': 'ok'}}}})

    assert call(relay, message()).message == 'ok'
    assert call(relay, message(x='-bad')).code == ResponseCode.INVALID_FIELD

@pytest.mark.parametrize('field', [{'pattern': '[a-z]+', 'default': 'BAD'}, {'default': '-bad'}, {'typo': 'x'}])
def test_invalid_field_definitions(field):
    with pytest.raises(ValidationError):
        CommandModel(cmd='/bin/echo ${x}', fields={'x': field})

def test_header_field(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/echo ${headers.x-event}'}})

    assert call(relay, message(), headers=[('X-Event', 'push')]).message == 'push'

@pytest.mark.parametrize('name', ['Authorization', 'authorization', 'Proxy-Authorization', 'Cookie'])
def test_secret_headers_are_never_available(make_relay, name):
    assert make_relay({})._usable_headers([(name, 'secret'), ('X-Other', 'kept')]) == {'x-other': 'kept'}

def test_body_cannot_supply_headers(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/echo ${headers.x-event}'}})

    assert call(relay, message(headers={'x-event': 'forged'})).code == ResponseCode.INVALID_FIELD

def test_header_value_is_validated(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/echo ${headers.x-event}'}})

    assert call(relay, message(), headers=[('X-Event', '-rf')]).code == ResponseCode.INVALID_FIELD

def test_headers_stay_out_of_stdin(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/cat'}})

    assert 'hidden' not in call(relay, message(), headers=[('X-Event', 'hidden')]).message

def test_uppercase_header_placeholder_is_a_config_error():
    with pytest.raises(ValidationError):
        CommandModel(cmd='/bin/echo ${headers.X-Event}')

@pytest.mark.parametrize('name', ['authorization', 'proxy-authorization', 'cookie'])
def test_secret_header_placeholder_is_a_config_error(name):
    with pytest.raises(ValidationError):
        CommandModel(cmd=f'/bin/echo ${{headers.{name}}}')

def test_default_over_field_limit_is_a_config_error():
    with pytest.raises(ValidationError):
        build_model({'a': {'run': {'cmd': '/bin/echo ${x}', 'fields': {'x': {'default': 'a' * 11}}}}}, limits={'max_field_size': 10})

def test_refused_request_is_logged_without_its_value(make_relay, caplog):
    caplog.set_level(logging.WARNING)

    call(make_relay({'run': ECHO}), message(x='-SECRETVALUE'))

    assert 'Field "x" has an invalid value' in caplog.text
    assert 'key "admin"' in caplog.text
    assert 'SECRETVALUE' not in caplog.text

def test_preview_shows_the_command_without_running(make_relay, tmp_path):
    marker = tmp_path / 'ran'
    relay = make_relay({'run': {'cmd': f'/usr/bin/touch {marker} ${{x}}'}})

    assert relay.preview(b'{"x": "v"}', 'run') == ['/usr/bin/touch', str(marker), 'v']
    assert not marker.exists()

@pytest.mark.parametrize('target,raw,code', [
    ('nope::x', b'{}', ResponseCode.UNKNOWN_CLUSTER),
    ('nope', b'{}', ResponseCode.UNKNOWN_COMMAND),
    ('run', b'{"x": "-bad"}', ResponseCode.INVALID_FIELD),
    ('run', b'x=y', ResponseCode.WRONG_FORMAT),
    ('run', b'{broken', ResponseCode.BAD_MESSAGE),
])
def test_preview_refusals(make_relay, target, raw, code):
    with pytest.raises(RelayError) as error:
        make_relay({'run': ECHO}).preview(raw, target)

    assert error.value.code == code

def test_run_id_differs_per_run(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/true'}})

    assert call(relay, message()).run_id != call(relay, message()).run_id

def test_hook_string_and_mapping_forms():
    assert HookModel.model_validate('/bin/x ${run.id}').cmd == ['/bin/x', '${run.id}']
    assert HookModel.model_validate({'cmd': '/bin/x', 'timeout': 5}).timeout == 5

@pytest.mark.parametrize('hook', ['/bin/x ${other}', 'sh -c "x ${run.id}"', '${run.id}'])
def test_invalid_hooks(hook):
    with pytest.raises(ValidationError):
        HookModel.model_validate(hook)

def run_background(relay, *messages):
    async def scenario():
        for item in messages:
            await relay.handle(item, '10.0.0.1')

        await settle(relay)

    asyncio.run(scenario())

def hook_recorder(tmp_path):
    log = tmp_path / 'hook.log'

    return log, script(tmp_path / 'hook.sh', f'echo "$1 $2 $3" >> {log}; cat >> {log}') + ' ${run.command} ${run.exit_code} ${run.id}'

def failing(tmp_path):
    return script(tmp_path / 'fail.sh', 'echo broken >&2; exit 7')

def test_global_hook_gets_details_and_stderr(make_relay, tmp_path):
    log, hook = hook_recorder(tmp_path)
    relay = make_relay({'run': {'cmd': failing(tmp_path), 'background': True}}, on_failure=hook)

    run_background(relay, message())

    lines = log.read_text().splitlines()
    name, code, run_id = lines[0].split()

    assert (name, code, len(run_id)) == ('default::run', '7', 8)
    assert lines[1] == 'broken'

def test_command_hook_replaces_global(make_relay, tmp_path):
    log, hook = hook_recorder(tmp_path)
    other = tmp_path / 'other'
    relay = make_relay({'run': {'cmd': failing(tmp_path), 'background': True, 'on_failure': f'/usr/bin/touch {other}'}}, on_failure=hook)

    run_background(relay, message())

    assert other.exists()
    assert not log.exists()

def test_command_can_opt_out(make_relay, tmp_path):
    log, hook = hook_recorder(tmp_path)
    relay = make_relay({'run': {'cmd': failing(tmp_path), 'background': True, 'on_failure': None}}, on_failure=hook)

    run_background(relay, message())

    assert not log.exists()

def test_hook_not_run_on_success(make_relay, tmp_path):
    log, hook = hook_recorder(tmp_path)

    run_background(make_relay({'run': {'cmd': '/bin/true', 'background': True}}, on_failure=hook), message())

    assert not log.exists()

def test_hook_not_run_for_normal_commands(make_relay, tmp_path):
    log, hook = hook_recorder(tmp_path)

    assert call(make_relay({'run': {'cmd': '/bin/false'}}, on_failure=hook), message()).code == ResponseCode.COMMAND_FAILED
    assert not log.exists()

def test_hook_runs_on_timeout(make_relay, tmp_path):
    log, hook = hook_recorder(tmp_path)

    run_background(make_relay({'run': {'cmd': '/bin/sleep 30', 'timeout': 1, 'background': True}}, on_failure=hook), message())

    assert log.read_text().split()[1] == '-15'

def test_hook_runs_when_command_cannot_start(make_relay, tmp_path):
    log, hook = hook_recorder(tmp_path)

    run_background(make_relay({'run': {'cmd': '/nonexistent/x', 'background': True}}, on_failure=hook), message())

    assert log.read_text().split()[1] == 'none'

def test_failing_hook_does_not_stop_follow_up(make_relay, tmp_path):
    runs = tmp_path / 'runs'
    target = script(tmp_path / 'r.sh', f'echo x >> {runs}; sleep 0.3; exit 1')
    relay = make_relay({'run': {'cmd': target, 'background': True}}, on_failure='/nonexistent/hook')

    async def scenario():
        await relay.handle(message(), '10.0.0.1')
        await asyncio.sleep(0.1)
        await relay.handle(message(), '10.0.0.1')
        await settle(relay)

        assert relay._background == {}

    asyncio.run(scenario())

    assert runs.read_text().count('x') == 2

def test_hook_not_run_for_commands_terminated_by_shutdown(make_relay, tmp_path):
    log, hook = hook_recorder(tmp_path)
    relay = make_relay({'run': {'cmd': '/bin/sleep 30', 'background': True}}, on_failure=hook)

    async def scenario():
        await relay.handle(message(), '10.0.0.1')
        await asyncio.sleep(0.2)
        await relay.drain(0)

    asyncio.run(scenario())

    assert not log.exists()

def test_hook_cannot_see_the_key(make_relay, tmp_path):
    log, hook = hook_recorder(tmp_path)
    relay = make_relay({'run': {'cmd': failing(tmp_path), 'background': True}}, on_failure=hook)

    run_background(relay, message())

    assert 'test-token' not in log.read_text()

def test_stdin_of_a_background_run_has_no_key(make_relay, tmp_path):
    out = tmp_path / 'out'
    target = script(tmp_path / 'c.sh', f'cat > {out}')

    run_background(make_relay({'run': {'cmd': target, 'background': True}}), message(a=1))

    assert json.loads(out.read_text()) == {'a': 1}
