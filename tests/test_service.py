import fcntl
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import pytest
from conftest import free_ports
from uscmdrelay.libraries.pid_file import PidFile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = {**os.environ, 'PYTHONPATH': ROOT}

class Service:
    def __init__(self, path):
        self.dir = path
        self.config = path / 'config.yml'
        self.log = path / 'service.log'
        self.pid_file = path / 'service.pid'
        self.tcp_port, self.http_port = free_ports(2)
        self.process = None

        self.write_config()

    def write_config(self, extra=''):
        self.config.write_text(f'''# a comment
listeners:
  tcp: {{port: {self.tcp_port}}}
  http: {{port: {self.http_port}}}
pid_file: {self.pid_file}
shutdown_timeout: 5
keys:
clusters:
  main:
    echo:
      cmd: /bin/echo ${{x}}
    plain:
      cmd: /bin/echo plain
    slow:
      cmd: /bin/sleep 1.5
    long:
      cmd: /bin/sleep 60
    pwd:
      cmd: /bin/pwd
{extra}''')
        self.config.chmod(0o600)

    def cli(self, *args, **kwargs):
        return subprocess.run([sys.executable, '-m', 'uscmdrelay', '--config', str(self.config), *args], env=ENV, capture_output=True, text=True, timeout=30, **kwargs)

    def add_key(self, name='k', clusters=('*',)):
        output = self.cli('key', 'add', name, '--clusters', *clusters).stdout

        return output.split('shown only once, give this to the client):\n  ')[1].split('\n')[0]

    def start(self):
        starts = self.logs().count('Service started')
        self.output = open(self.log, 'a')
        self.process = subprocess.Popen([sys.executable, '-m', 'uscmdrelay', '--config', str(self.config), 'serve'], env=ENV, stdout=self.output, stderr=subprocess.STDOUT)

        self.wait_for_log('Service started', starts + 1)

    def logs(self):
        return self.log.read_text() if self.log.exists() else ''

    def signal(self, sig):
        self.process.send_signal(sig)

    def wait(self):
        return self.process.wait(timeout=30)

    def wait_for_log(self, text, count=1):
        deadline = time.monotonic() + 10

        while time.monotonic() < deadline:
            if self.logs().count(text) >= count:
                return

            time.sleep(0.02)

        raise AssertionError(f'{text!r} not logged: {self.logs()}')

    def post(self, path, token, body=b''):
        connection = http.client.HTTPConnection('127.0.0.1', self.http_port, timeout=30)
        connection.request('POST', path, body, {'Authorization': f'Bearer {token}'})
        response = connection.getresponse()

        return response.status, json.loads(response.read())

    def tcp(self, message):
        with socket.create_connection(('127.0.0.1', self.tcp_port), timeout=30) as s:
            s.sendall(message.encode())

            return json.loads(s.makefile().readline())

@pytest.fixture
def service(tmp_path):
    instance = Service(tmp_path)

    yield instance

    if instance.process and instance.process.poll() is None:
        instance.process.kill()
        instance.process.wait()

def test_serves_both_listeners_and_stops_cleanly(service):
    token = service.add_key()
    service.start()

    assert service.post('/run/main/plain', token)[1]['message'] == 'plain'
    assert service.tcp(json.dumps({'auth_key': token, 'command': 'main::plain'}) + '\n')['message'] == 'plain'
    assert service.pid_file.exists()

    service.signal(signal.SIGTERM)

    assert service.wait() == 0
    assert not service.pid_file.exists()
    assert 'Service stopped' in service.logs()

def test_private_directory_is_used_and_removed(service):
    token = service.add_key()
    service.start()

    directory = service.post('/run/main/pwd', token)[1]['message']

    assert os.path.isdir(directory)
    assert os.stat(directory).st_mode & 0o077 == 0

    service.signal(signal.SIGTERM)
    service.wait()

    assert not os.path.exists(directory)

def test_key_add_applies_without_restart(service):
    service.start()

    token = service.add_key()
    service.wait_for_log('Config reloaded')

    assert service.post('/run/main/plain', token)[0] == 200

def test_key_remove_revokes_at_once(service):
    token = service.add_key()
    service.start()

    assert service.post('/run/main/plain', token)[0] == 200

    assert 'Running service reloaded' in service.cli('key', 'remove', 'k').stdout
    service.wait_for_log('Config reloaded')

    assert service.post('/run/main/plain', token)[0] == 401

def test_key_rotate_replaces_the_key(service):
    old = service.add_key('k', ('main',))
    service.start()

    output = service.cli('key', 'rotate', 'k').stdout
    new = output.split('client):\n  ')[1].split('\n')[0]
    service.wait_for_log('Config reloaded')

    assert service.post('/run/main/plain', old)[0] == 401
    assert service.post('/run/main/plain', new)[0] == 200
    assert 'clusters: [main]' in service.config.read_text()

def test_hand_edit_applies_on_sighup(service):
    token = service.add_key()
    service.start()

    assert service.post('/run/main/added', token)[0] == 404

    service.config.write_text(service.config.read_text() + '    added:\n      cmd: /bin/echo new\n')
    service.signal(signal.SIGHUP)
    service.wait_for_log('Config reloaded')

    assert service.post('/run/main/added', token)[1]['message'] == 'new'

def test_broken_reload_keeps_the_old_config(service):
    token = service.add_key()
    service.start()

    service.config.write_text('clusters: [')
    service.signal(signal.SIGHUP)
    service.wait_for_log('Reload refused')

    assert service.post('/run/main/plain', token)[0] == 200

def test_reload_warns_about_changes_needing_restart(service):
    token = service.add_key()
    service.start()

    service.config.write_text(service.config.read_text().replace('shutdown_timeout: 5', 'shutdown_timeout: 6'))
    service.signal(signal.SIGHUP)
    service.wait_for_log('need a restart')

    assert service.post('/run/main/plain', token)[0] == 200

def test_sigterm_lets_running_commands_finish(service):
    token = service.add_key()
    service.start()
    result = {}

    thread = threading.Thread(target=lambda: result.update(reply=service.post('/run/main/slow', token)))
    thread.start()
    time.sleep(0.5)

    service.signal(signal.SIGTERM)
    thread.join()

    assert result['reply'][0] == 200
    assert service.wait() == 0

def test_sigterm_terminates_commands_after_the_timeout(service):
    service.write_config()
    service.config.write_text(service.config.read_text().replace('shutdown_timeout: 5', 'shutdown_timeout: 0'))

    token = service.add_key()
    service.start()
    result = {}

    thread = threading.Thread(target=lambda: result.update(reply=service.post('/run/main/long', token)))
    thread.start()
    time.sleep(0.5)

    service.signal(signal.SIGTERM)
    thread.join()

    assert result['reply'][0] == 503
    assert service.wait() == 0

def test_second_instance_is_refused(service):
    service.start()

    second = service.cli('serve')

    assert second.returncode == 1
    assert 'already running' in second.stderr

def test_stale_pid_file_does_not_block_a_restart(service):
    token = service.add_key()
    service.start()

    service.process.kill()
    service.process.wait()

    assert service.pid_file.exists()

    service.start()

    assert service.post('/run/main/plain', token)[0] == 200

def test_port_in_use_fails_cleanly(service):
    with socket.socket() as blocker:
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(('127.0.0.1', service.http_port))
        blocker.listen()

        result = service.cli('serve')

    assert result.returncode == 1
    assert 'Could not start listening' in result.stderr
    assert not service.pid_file.exists()

def test_configtest_exit_codes(service):
    assert service.cli('configtest').returncode == 0

    service.config.write_text('listeners: {}\n')
    result = service.cli('configtest')

    assert result.returncode == 2
    assert 'At least one listener' in result.stderr
    assert 'Traceback' not in result.stderr

def test_missing_config_exit_code(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'uscmdrelay', '--config', str(tmp_path / 'nope.yml'), 'configtest'], env=ENV, capture_output=True, text=True)

    assert result.returncode == 2

def test_non_utf8_config_is_a_clean_error(service):
    service.config.write_bytes(b'listeners: {}\n# \xff\xfe\n')
    result = service.cli('configtest')

    assert result.returncode == 2
    assert 'Traceback' not in result.stderr

def test_writable_directory_is_refused(service):
    service.dir.chmod(0o777)

    try:
        result = service.cli('configtest')
    finally:
        service.dir.chmod(0o700)

    assert result.returncode == 2
    assert 'must not be writable' in result.stderr

def test_allowed_ip_requires_key(service):
    before = service.config.read_text()
    result = service.cli('cluster', 'add', 'x', '--allowed-ip', '10.0.0.1')

    assert result.returncode == 2
    assert service.config.read_text() == before

def test_cluster_add_with_key(service):
    result = service.cli('cluster', 'add', 'ops', '--key', 'bot', '--allowed-ip', '10.0.0.1')

    assert result.returncode == 0
    assert '# a comment' in service.config.read_text()
    assert 'allowed_ips: [10.0.0.1]' in service.config.read_text()

def test_parallel_edits_are_all_saved(service):
    processes = [subprocess.Popen([sys.executable, '-m', 'uscmdrelay', '--config', str(service.config), 'key', 'add', f'k{i}', '--clusters', 'main'], env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for i in range(10)]

    assert [process.wait(timeout=30) for process in processes] == [0] * 10
    assert service.config.read_text().count('- name:') == 10

def test_dry_run_command(service, tmp_path):
    data = tmp_path / 'data.json'
    data.write_text('{"x": "hello"}')

    result = service.cli('test', 'main::echo', '--data', str(data))

    assert (result.returncode, result.stdout.strip()) == (0, 'Would run: /bin/echo hello')

def test_dry_run_reads_stdin_and_reports_refusals(service):
    result = service.cli('test', 'main::echo', '--data', '-', input='{"x": "-bad"}')

    assert result.returncode == 1
    assert 'Would be refused: Field "x"' in result.stderr
    assert 'Would run' not in result.stdout

def test_dry_run_reports_a_skip(service):
    service.write_config("    tagged:\n      cmd: /bin/echo ok\n      when:\n        ref: 'refs/tags/.+'\n")

    result = service.cli('test', 'main::tagged', '--data', '-', input='{"ref": "refs/heads/main"}')

    assert (result.returncode, result.stdout.strip()) == (0, 'Would skip: Condition on "ref" not met')

def test_dry_run_with_header(service):
    service.write_config('    head:\n      cmd: /bin/echo ${headers.x-event}\n')

    assert service.cli('test', 'main::head', '--header', 'X-Event: push').stdout.strip() == 'Would run: /bin/echo push'

def test_sample_config_is_valid_as_shipped(tmp_path):
    sample = tmp_path / 'config.yml'
    shutil.copy(os.path.join(ROOT, 'config.sample.yml'), sample)
    sample.chmod(0o600)

    result = subprocess.run([sys.executable, '-m', 'uscmdrelay', '--config', str(sample), 'configtest'], env=ENV, capture_output=True, text=True)

    assert (result.returncode, result.stderr) == (0, '')

def test_client_leaving_before_its_reply_is_not_an_error(service):
    token = service.add_key()
    service.start()

    with socket.create_connection(('127.0.0.1', service.http_port)) as s:
        s.sendall(f'POST /run/main/slow HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {token}\r\nContent-Length: 0\r\n\r\n'.encode())
        time.sleep(0.3)

    service.wait_for_log('exit=0')
    time.sleep(0.3)

    assert 'Traceback' not in service.logs()

def test_same_port_twice_is_a_config_error(service):
    service.config.write_text('listeners:\n  tcp: {port: 7777}\n  http: {port: 7777}\n')

    assert service.cli('configtest').returncode == 2

def test_same_port_on_different_addresses_is_allowed(service):
    service.config.write_text('listeners:\n  tcp: {host: 127.0.0.1, port: 7777}\n  http: {host: 127.0.0.2, port: 7777}\n')

    assert service.cli('configtest').returncode == 0

def test_log_file_is_not_readable_by_others(service, tmp_path):
    log = tmp_path / 'logs' / 'relay.log'

    assert service.cli('--log', str(log), 'configtest').returncode == 0
    assert os.stat(log).st_mode & 0o777 == 0o640

def test_symlinked_pid_file_is_not_signalled(tmp_path):
    victim = subprocess.Popen(['sleep', '30'])
    target = tmp_path / 'other.pid'
    target.write_text(str(victim.pid))
    link = tmp_path / 'service.pid'
    link.symlink_to(target)

    with open(target) as f:
        fcntl.flock(f, fcntl.LOCK_EX)

        try:
            assert not PidFile(str(link)).send_signal(signal.SIGTERM)
            time.sleep(0.2)
            assert victim.poll() is None
        finally:
            victim.kill()
            victim.wait()

def test_permission_error_says_how_to_fix_it(service):
    service.config.chmod(0o660)
    result = service.cli('configtest')

    assert result.returncode == 2
    assert f'chmod go-w {service.config}' in result.stderr

def test_malformed_dry_run_header_is_a_usage_error(service):
    result = service.cli('test', 'main::plain', '--header', 'NoColon')

    assert result.returncode == 2
    assert 'Name: value' in result.stderr

def test_failed_logins_do_not_flood_the_log(service):
    service.add_key()
    service.start()

    for _ in range(200):
        assert service.post('/run/main/plain', 'wrong')[0] == 401

    assert service.logs().count('Authentication failed') == 1

@pytest.mark.skipif(os.getuid() != 0, reason='needs root')
def test_root_signals_with_only_the_pid_file_owners_rights(tmp_path):
    nobody = 65534
    path = tmp_path / 'pid'
    victim = subprocess.Popen(['sleep', '30'])
    own = subprocess.Popen(['sleep', '30'], user=nobody)

    def holder_with(pid):
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
        os.fchown(fd, nobody, nobody)
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, str(pid).encode())

        return fd

    try:
        fd = holder_with(victim.pid)

        assert not PidFile(str(path)).send_signal(signal.SIGTERM)
        assert victim.poll() is None
        assert os.getuid() == 0 and os.geteuid() == 0

        os.close(fd)
        fd = holder_with(own.pid)

        assert PidFile(str(path)).send_signal(signal.SIGTERM)
        assert own.wait(timeout=5) == -signal.SIGTERM

        os.close(fd)
    finally:
        victim.kill()
        own.kill()
