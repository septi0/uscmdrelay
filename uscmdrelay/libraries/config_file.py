import fcntl
import os
import shlex
import tempfile
from contextlib import contextmanager
from typing import Iterator
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from uscmdrelay.exceptions import UsCmdRelayRuntimeError
from uscmdrelay.libraries import payload

__all__ = ['ConfigFile']

class ConfigFile:
    def __init__(self, path: str) -> None:
        self._path: str = os.path.realpath(path)
        self._directory: str = os.path.dirname(self._path)
        self._secrets_path: str = os.path.join(self._directory, 'secrets.env')

        # round trip mode keeps comments and ordering when the file is written back
        self._yaml: YAML = YAML()
        self._yaml.preserve_quotes = True
        self._yaml.width = 4096
        self._yaml.indent(mapping=2, sequence=4, offset=2)

    def read(self, *, check_owner: bool = True) -> dict:
        self._check_file(self._directory, 0o022, 'must not be writable by group or others', 'go-w', check_owner)
        self._check_file(self._path, 0o022, 'must not be writable by group or others', 'go-w', check_owner)

        try:
            with open(self._path, 'r', encoding='utf-8') as f:
                data = self._yaml.load(f)
        except (YAMLError, UnicodeDecodeError) as e:
            raise UsCmdRelayRuntimeError(f'Failed to parse config file: {e}')

        return {} if data is None else data

    def read_secrets(self, *, check_owner: bool = True) -> dict[str, str]:
        if not os.path.isfile(self._secrets_path):
            return {}

        self._check_file(self._secrets_path, 0o077, 'must have permissions 600', '600', check_owner)

        try:
            with open(self._secrets_path, 'r', encoding='utf-8') as f:
                return payload.parse_env(f.read())
        except (payload.PayloadError, UnicodeDecodeError) as e:
            raise UsCmdRelayRuntimeError(f'Failed to parse "{self._secrets_path}": {e}')

    @contextmanager
    def lock(self) -> Iterator[None]:
        """ Hold exclusive access for a read and write cycle. The directory is locked, since writing replaces the file. """
        fd = os.open(self._directory, os.O_RDONLY)

        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def write(self, data: dict) -> None:
        """ Replace the config atomically, keeping its owner and permissions. """
        stat = os.stat(self._path)
        fd, temp_path = tempfile.mkstemp(dir=self._directory, prefix='.config-')

        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                os.fchown(f.fileno(), stat.st_uid, stat.st_gid)
                os.fchmod(f.fileno(), stat.st_mode & 0o777)

                self._yaml.dump(data, f)

                f.flush()
                os.fsync(f.fileno())

            os.replace(temp_path, self._path)
        except BaseException:
            os.unlink(temp_path)
            raise

    def _check_file(self, path: str, forbidden_mode: int, requirement: str, chmod_mode: str, check_owner: bool) -> None:
        stat = os.stat(path)

        if stat.st_mode & forbidden_mode:
            raise UsCmdRelayRuntimeError(f'"{path}" {requirement}, fix with: chmod {chmod_mode} {shlex.quote(path)}')

        if check_owner and stat.st_uid not in (os.getuid(), 0):
            raise UsCmdRelayRuntimeError(f'"{path}" must be owned by root or the user running the service')
