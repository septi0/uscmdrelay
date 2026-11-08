import os
import signal
import stat
import pytest
from ruamel.yaml import YAML
from uscmdrelay.exceptions import UsCmdRelayRuntimeError
from uscmdrelay.libraries.pid_file import PidFile
from uscmdrelay.manager import UsCmdRelayManager
from uscmdrelay.services.auth import hash_token

CONFIG = '''# top comment
listeners:
  tcp: {port: 7777}  # inline comment
keys: []
clusters:
  # a cluster comment
  deploy:
    run:
      cmd: /bin/echo ${x}
'''

@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / 'config.yml'
    path.write_text(CONFIG + f'pid_file: {tmp_path}/pid\n')
    path.chmod(0o640)

    return path

@pytest.fixture
def manager(config_path):
    return UsCmdRelayManager(config_file=str(config_path), admin=True)

def load(path):
    return YAML(typ='safe').load(path.read_text())

def test_add_cluster_keeps_comments_and_existing_content(manager, config_path):
    assert manager.admin.add_cluster('ops') is None

    text = config_path.read_text()

    assert '# top comment' in text
    assert '# inline comment' in text
    assert '# a cluster comment' in text
    assert load(config_path)['clusters']['ops'] == {}
    assert load(config_path)['clusters']['deploy']['run']['cmd'] == '/bin/echo ${x}'

def test_add_cluster_with_key_is_one_change(manager, config_path):
    token = manager.admin.add_cluster('ops', key_name='bot', allowed_ips=['10.0.0.5'])
    key = load(config_path)['keys'][0]

    assert key == {'name': 'bot', 'hash': f'sha256:{hash_token(token)}', 'clusters': ['ops'], 'allowed_ips': ['10.0.0.5']}

def test_add_key_stores_only_the_hash(manager, config_path):
    token = manager.admin.add_key('bot', ['deploy'])

    assert len(token) >= 40
    assert token not in config_path.read_text()
    assert load(config_path)['keys'][0]['hash'] == f'sha256:{hash_token(token)}'

def test_add_key_for_all_clusters(manager, config_path):
    manager.admin.add_key('bot', ['*'])

    assert load(config_path)['keys'][0]['clusters'] == ['*']

def test_result_is_loadable_by_the_service(manager, config_path):
    manager.admin.add_cluster('ops', key_name='bot')

    UsCmdRelayManager(config_file=str(config_path))

def test_remove_key(manager, config_path):
    manager.admin.add_key('one', ['deploy'])
    manager.admin.add_key('two', ['deploy'])
    manager.admin.remove_key('one')

    assert [key['name'] for key in load(config_path)['keys']] == ['two']

def test_keeps_permissions(manager, config_path):
    manager.admin.add_cluster('ops')

    assert stat.S_IMODE(os.stat(config_path).st_mode) == 0o640

@pytest.mark.parametrize('action', [
    lambda admin: admin.add_cluster('deploy'),
    lambda admin: admin.add_cluster('bad/name'),
    lambda admin: admin.add_key('bot', ['missing']),
    lambda admin: admin.add_key('bad name', ['deploy']),
    lambda admin: admin.add_key('bot', ['deploy'], ['not-an-ip']),
    lambda admin: admin.remove_key('missing'),
])
def test_refused_changes_leave_the_file_untouched(manager, config_path, action):
    before = config_path.read_text()

    with pytest.raises(UsCmdRelayRuntimeError):
        action(manager.admin)

    assert config_path.read_text() == before
    assert sorted(p.name for p in config_path.parent.iterdir()) == ['config.yml']

def test_duplicate_key_refused(manager):
    manager.admin.add_key('bot', ['deploy'])

    with pytest.raises(UsCmdRelayRuntimeError):
        manager.admin.add_key('bot', ['deploy'])

def test_writable_config_is_refused(config_path):
    config_path.chmod(0o664)

    with pytest.raises(UsCmdRelayRuntimeError):
        UsCmdRelayManager(config_file=str(config_path))

def test_broken_yaml_is_reported(config_path):
    config_path.write_text('listeners: [')

    with pytest.raises(UsCmdRelayRuntimeError):
        UsCmdRelayManager(config_file=str(config_path))

def test_duplicate_yaml_keys_are_reported(config_path):
    config_path.write_text('listeners: {tcp: {port: 1}}\nlisteners: {tcp: {port: 2}}\n')

    with pytest.raises(UsCmdRelayRuntimeError):
        UsCmdRelayManager(config_file=str(config_path))

def test_python_tags_are_not_constructed(config_path, tmp_path):
    marker = tmp_path / 'pwned'
    config_path.write_text(f'listeners: !!python/object/apply:os.system ["touch {marker}"]\n')

    with pytest.raises(UsCmdRelayRuntimeError):
        UsCmdRelayManager(config_file=str(config_path))

    assert not marker.exists()

def test_missing_config_is_reported(tmp_path):
    with pytest.raises(UsCmdRelayRuntimeError):
        UsCmdRelayManager(config_file=str(tmp_path / 'missing.yml'))

def write_secrets(config_path, content, mode=0o600):
    path = config_path.parent / 'secrets.env'
    path.write_text(content)
    path.chmod(mode)

def add_env(config_path, value):
    config_path.write_text(config_path.read_text().replace('cmd: /bin/echo ${x}', f'cmd: /bin/echo ${{x}}\n      env: {{TOKEN: "{value}"}}'))

def test_secrets_are_used_and_stay_out_of_the_config(config_path):
    write_secrets(config_path, 'REGISTRY=abc\n')
    add_env(config_path, '${REGISTRY}')

    manager = UsCmdRelayManager(config_file=str(config_path))

    assert manager._model.clusters['deploy']['run'].env == {'TOKEN': 'abc'}
    assert 'abc' not in config_path.read_text()

def test_secret_placeholder_survives_a_cli_write(config_path):
    write_secrets(config_path, 'REGISTRY=abc\n')
    add_env(config_path, '${REGISTRY}')

    UsCmdRelayManager(config_file=str(config_path), admin=True).admin.add_cluster('ops')

    assert '${REGISTRY}' in config_path.read_text()
    assert 'abc' not in config_path.read_text()

def test_readable_secrets_are_refused(config_path):
    write_secrets(config_path, 'REGISTRY=abc\n', mode=0o640)

    with pytest.raises(UsCmdRelayRuntimeError):
        UsCmdRelayManager(config_file=str(config_path))

def test_unknown_secret_is_reported(config_path):
    add_env(config_path, '${MISSING}')

    with pytest.raises(UsCmdRelayRuntimeError):
        UsCmdRelayManager(config_file=str(config_path))

def test_pid_file_lock(tmp_path):
    path = str(tmp_path / 'pid')
    first = PidFile(path)
    second = PidFile(path)

    assert first.acquire()
    assert not second.acquire()

    first.release()

    assert not os.path.exists(path)
    assert second.acquire()

    second.release()

def test_leftover_pid_file_does_not_block_or_get_signalled(tmp_path):
    path = tmp_path / 'pid'
    path.write_text('1')

    pid_file = PidFile(str(path))

    assert not pid_file.send_signal(1)
    assert pid_file.acquire()

    pid_file.release()

def test_signal_reaches_the_lock_holder(tmp_path):
    received = []
    previous = signal.signal(signal.SIGUSR1, lambda *_: received.append(True))
    holder = PidFile(str(tmp_path / 'pid'))

    try:
        assert holder.acquire()
        assert PidFile(str(tmp_path / 'pid')).send_signal(signal.SIGUSR1)
        assert received
    finally:
        holder.release()
        signal.signal(signal.SIGUSR1, previous)
