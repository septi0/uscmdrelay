import asyncio
import json
import logging
import pytest
from pydantic import ValidationError
from conftest import LIMITED_TOKEN, TOKEN, build_model
from test_fields_and_hooks import settle
from test_listeners import BEARER, http_call, run, tcp_exchange
from test_relay import call, message
from uscmdrelay.exceptions import ConditionNotMet
from uscmdrelay.models.command import CommandModel
from uscmdrelay.models.response import ResponseCode

TAG_PUSH = {'ref': 'refs/tags/v1.0', 'after': 'abc123'}
BRANCH_PUSH = {'ref': 'refs/heads/main'}

def tagged(**extra):
    return {'cmd': '/bin/echo ${ref}', 'when': {'ref': 'refs/tags/.+'}, **extra}

def test_runs_when_condition_is_met(make_relay):
    response = call(make_relay({'run': tagged()}), message(**TAG_PUSH))

    assert (response.status, response.message) == ('ok', 'refs/tags/v1.0')

def test_skipped_when_condition_is_not_met(make_relay, tmp_path):
    marker = tmp_path / 'ran'
    relay = make_relay({'run': {'cmd': f'/usr/bin/touch {marker}', 'when': {'ref': 'refs/tags/.+'}}})

    assert call(relay, message(**BRANCH_PUSH)).to_dict() == {'status': 'skipped', 'message': 'Condition on "ref" not met', 'code': 0}
    assert not marker.exists()

def test_regex_must_match_the_whole_value(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/true', 'when': {'ref': 'refs/tags/'}}})

    assert call(relay, message(ref='refs/tags/v1')).status == 'skipped'

def test_skipped_rather_than_refused_for_a_missing_template_field(make_relay):
    assert call(make_relay({'run': tagged()}), message()).status == 'skipped'

def test_fields_still_validated_when_condition_is_met(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/echo ${after}', 'when': {'ref': 'refs/tags/.+'}}})

    assert call(relay, message(ref='refs/tags/v1', after='-bad')).code == ResponseCode.INVALID_FIELD

def test_all_conditions_must_be_true(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/true', 'when': {'a': 'x', 'b': 'y'}}})

    assert call(relay, message(a='x', b='y')).status == 'ok'
    assert call(relay, message(a='x', b='z')).status == 'skipped'

@pytest.mark.parametrize('fields,exists,status', [
    ({'a': 1}, True, 'ok'),
    ({'a': {'nested': 1}}, True, 'ok'),
    ({'a': [1]}, True, 'ok'),
    ({'a': ''}, True, 'ok'),
    ({'a': False}, True, 'ok'),
    ({}, True, 'skipped'),
    ({'a': None}, True, 'skipped'),
    ({}, False, 'ok'),
    ({'a': None}, False, 'ok'),
    ({'a': 1}, False, 'skipped'),
])
def test_exists(make_relay, fields, exists, status):
    relay = make_relay({'run': {'cmd': '/bin/true', 'when': {'a': {'exists': exists}}}})

    assert call(relay, message(**fields)).status == status

def test_nested_field_and_array_index(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/true', 'when': {'repo.tags.0': 'v1'}}})

    assert call(relay, message(repo={'tags': ['v1']})).status == 'ok'
    assert call(relay, message(repo={'tags': []})).status == 'skipped'

@pytest.mark.parametrize('value', [{'a': 1}, [1], None])
def test_non_text_values_do_not_match_a_regex(make_relay, value):
    relay = make_relay({'run': {'cmd': '/bin/true', 'when': {'a': '.*'}}})

    assert call(relay, message(a=value)).status == 'skipped'

def test_numbers_and_booleans_match_as_text(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/true', 'when': {'a': 'true', 'b': '5'}}})

    assert call(relay, message(a=True, b=5)).status == 'ok'

def test_value_over_the_field_limit_does_not_match(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/true', 'when': {'a': '.*'}}}, limits={'max_field_size': 5})

    assert call(relay, message(a='x' * 5)).status == 'ok'
    assert call(relay, message(a='x' * 6)).status == 'skipped'

def test_condition_values_skip_the_safe_pattern(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/true', 'when': {'a': '-.*'}}})

    assert call(relay, message(a='-looks-like-an-option')).status == 'ok'

def test_header_condition(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/true', 'when': {'headers.x-event': 'push'}}})

    assert call(relay, message(), headers=[('X-Event', 'push')]).status == 'ok'
    assert call(relay, message(), headers=[('X-Event', 'create')]).status == 'skipped'

def test_body_cannot_supply_headers(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/true', 'when': {'headers.x-event': 'push'}}})

    assert call(relay, message(headers={'x-event': 'push'})).status == 'skipped'

def test_env_format(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/true', 'format': 'env', 'when': {'kind': 'tag'}}})

    assert call(relay, f'auth_key={TOKEN}\ncommand=run\nkind=tag\n'.encode()).status == 'ok'
    assert call(relay, f'auth_key={TOKEN}\ncommand=run\nkind=branch\n'.encode()).status == 'skipped'

def test_authentication_comes_first(make_relay):
    relay = make_relay({'run': tagged()})

    assert call(relay, message(auth_key='wrong')).code == ResponseCode.AUTH_FAILED
    assert call(relay, message(auth_key=LIMITED_TOKEN)).code == ResponseCode.AUTH_FAILED

def test_format_is_checked_before_conditions(make_relay):
    relay = make_relay({'run': tagged()})

    assert call(relay, f'auth_key={TOKEN}\ncommand=run\n'.encode()).code == ResponseCode.WRONG_FORMAT

def test_skipped_background_command_is_not_queued(make_relay):
    relay = make_relay({'run': {'cmd': '/bin/true', 'background': True, 'when': {'a': 'x'}}})

    assert call(relay, message()).status == 'skipped'
    assert relay._background == {}

def test_skip_does_not_trigger_on_failure(make_relay, tmp_path):
    marker = tmp_path / 'hook'
    relay = make_relay({'run': {'cmd': '/bin/false', 'background': True, 'when': {'a': 'x'}}}, on_failure=f'/usr/bin/touch {marker}')

    async def scenario():
        await relay.handle(message(), '10.0.0.1')
        await settle(relay)

    asyncio.run(scenario())

    assert not marker.exists()

def test_skip_is_logged_without_the_value(make_relay, caplog):
    caplog.set_level(logging.INFO)

    call(make_relay({'run': tagged()}), message(ref='SECRETVALUE'))

    assert 'Skipped default::run for key "admin"' in caplog.text
    assert 'SECRETVALUE' not in caplog.text

def test_preview_reports_a_skip(make_relay):
    with pytest.raises(ConditionNotMet):
        make_relay({'run': tagged()}).preview(json.dumps(BRANCH_PUSH).encode(), 'run')

def test_preview_runs_when_met(make_relay):
    assert make_relay({'run': tagged()}).preview(json.dumps(TAG_PUSH).encode(), 'run') == ['/bin/echo', 'refs/tags/v1.0']

@pytest.mark.parametrize('when', [
    {'a': {}},
    {'a': {'pattern': 'x', 'exists': True}},
    {'a': {'typo': 'x'}},
    {'a': '('},
    {'a b': 'x'},
    {'auth_key': 'x'},
    {'auth_key.x': {'exists': True}},
    {'headers.authorization': {'exists': True}},
    {'headers.cookie': 'x'},
    {'headers.X-Event': 'x'},
])
def test_invalid_conditions_are_config_errors(when):
    with pytest.raises(ValidationError):
        CommandModel(cmd='/bin/true', when=when)

def test_condition_forms():
    command = CommandModel(cmd='/bin/true', when={'a': 'x', 'b': {'pattern': 'y'}, 'c': {'exists': False}})

    assert command.when['a'].pattern.pattern == 'x'
    assert command.when['b'].pattern.pattern == 'y'
    assert command.when['c'].exists is False

def test_reload_applies_new_conditions(make_relay):
    relay = make_relay({'run': tagged()})

    assert call(relay, message(ref='refs/heads/main')).status == 'skipped'

    relay.update(build_model({'default': {'run': {'cmd': '/bin/echo ${ref}'}}}))

    assert call(relay, message(ref='refs/heads/main')).status == 'ok'

def test_skip_over_http_is_a_200():
    status, body, _ = http_call('/run/default/when', headers=BEARER, data=json.dumps(BRANCH_PUSH).encode())

    assert (status, body['status'], body['code']) == (200, 'skipped', 0)

def test_skip_over_tcp():
    body = json.dumps({'auth_key': TOKEN, 'command': 'when', **BRANCH_PUSH}).encode() + b'\n'

    assert run(lambda tcp, http: tcp_exchange(tcp, body))['status'] == 'skipped'
