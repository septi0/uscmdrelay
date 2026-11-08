import pytest
from pydantic import ValidationError
from uscmdrelay.models.command import CommandModel
from uscmdrelay.models.uscmdrelay import UsCmdRelayModel

HASH = 'sha256:' + 'a' * 64
LISTENERS = {'tcp': {'port': 7777}}

def test_cmd_split_and_quoting():
    assert CommandModel(cmd='/bin/x "a b" --c=d').cmd == ['/bin/x', 'a b', '--c=d']

def test_command_name_case_is_kept():
    model = UsCmdRelayModel.model_validate({'listeners': LISTENERS, 'clusters': {'a': {'MyCmd': {'cmd': '/bin/x'}}}})

    assert 'MyCmd' in model.clusters['a']

def test_percent_in_command_is_literal():
    assert CommandModel(cmd='date +%F').cmd == ['date', '+%F']

def test_placeholders_and_render():
    command = CommandModel(cmd='/bin/x --a=${a.b} ${c}')

    assert command.placeholders == ['a.b', 'c']
    assert command.render({'a.b': '1', 'c': '2'}) == ['/bin/x', '--a=1', '2']

def test_escape_is_literal():
    command = CommandModel(cmd='/bin/x $${HOME} $HOME $$')

    assert command.placeholders == []
    assert command.render({}) == ['/bin/x', '${HOME}', '$HOME', '$$']

def test_substituted_value_is_not_expanded_again():
    assert CommandModel(cmd='/bin/x ${a}').render({'a': '${b}'}) == ['/bin/x', '${b}']

@pytest.mark.parametrize('cmd', [
    '${a}',
    '/opt/${a}/run',
    'sh -c "echo ${a}"',
    '/bin/bash -c "x ${a}"',
    '/usr/bin/env sh -c "x ${a}"',
    'sudo bash -c "x ${a}"',
    '/bin/x ${auth_key}',
])
def test_refused_templates(cmd):
    with pytest.raises(ValidationError):
        CommandModel(cmd=cmd)

def test_shell_without_placeholders_is_allowed():
    assert CommandModel(cmd='sh -c "echo $${HOME}"').cmd[0] == 'sh'

def test_unbalanced_quote_is_a_config_error():
    with pytest.raises(ValidationError):
        CommandModel(cmd='/bin/x "a')

def test_invalid_regex_is_a_config_error():
    with pytest.raises(ValidationError):
        CommandModel(cmd='/bin/x', fields={'a': '('})

def test_auth_key_cannot_be_a_field():
    with pytest.raises(ValidationError):
        CommandModel(cmd='/bin/x', fields={'auth_key': '.*'})

def test_secrets_expand_in_env():
    command = CommandModel.model_validate({'cmd': '/bin/x', 'env': {'A': 'pre-${S}', 'B': '$${S}'}}, context={'secrets': {'S': 'v'}})

    assert command.env == {'A': 'pre-v', 'B': '${S}'}

def test_unknown_secret_is_a_config_error():
    with pytest.raises(ValidationError):
        CommandModel.model_validate({'cmd': '/bin/x', 'env': {'A': '${S}'}}, context={'secrets': {}})

@pytest.mark.parametrize('config', [
    {},
    {'listeners': {}},
    {'listeners': LISTENERS, 'unknown': 1},
    {'listeners': {'tcp': {'port': 0}}},
    {'listeners': LISTENERS, 'clusters': {'a/b': {}}},
    {'listeners': LISTENERS, 'clusters': {'a': {'x::y': {'cmd': '/bin/x'}}}},
    {'listeners': LISTENERS, 'clusters': {'a': {'b': {'cmd': '/bin/x', 'typo': 1}}}},
    {'listeners': LISTENERS, 'keys': [{'name': 'k', 'hash': 'plain', 'clusters': ['*']}]},
    {'listeners': LISTENERS, 'keys': [{'name': 'k', 'hash': HASH, 'clusters': ['missing']}]},
    {'listeners': LISTENERS, 'keys': [{'name': 'k', 'hash': HASH, 'clusters': ['*', 'a']}]},
    {'listeners': LISTENERS, 'keys': [{'name': 'k', 'hash': HASH, 'clusters': ['*'], 'allowed_ips': ['nope']}]},
    {'listeners': LISTENERS, 'keys': [{'name': 'k', 'hash': HASH, 'clusters': ['*']}, {'name': 'k', 'hash': 'sha256:' + 'b' * 64, 'clusters': ['*']}]},
    {'listeners': LISTENERS, 'keys': [{'name': 'k', 'hash': HASH, 'clusters': ['*']}, {'name': 'j', 'hash': HASH, 'clusters': ['*']}]},
])
def test_invalid_configs(config):
    with pytest.raises(ValidationError):
        UsCmdRelayModel.model_validate(config)

def test_empty_sections_and_clusters_are_valid():
    model = UsCmdRelayModel.model_validate({'listeners': LISTENERS, 'keys': None, 'clusters': {'a': None, 'b': {}}})

    assert model.keys == []
    assert model.clusters == {'a': {}, 'b': {}}
