import signal
from contextlib import contextmanager
from typing import Callable, Iterator
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from uscmdrelay.exceptions import UsCmdRelayRuntimeError
from uscmdrelay.libraries.config_file import ConfigFile
from uscmdrelay.libraries.pid_file import PidFile
from uscmdrelay.services.auth import generate_key

__all__ = ['AdminService']

class AdminService:
    def __init__(self, config_file: ConfigFile, validate: Callable[[dict], object], pid_file: PidFile) -> None:
        self._config_file: ConfigFile = config_file
        self._validate: Callable[[dict], object] = validate
        self._pid_file: PidFile = pid_file

    def add_cluster(self, name: str, *, key_name: str | None = None, allowed_ips: list[str] | None = None) -> str | None:
        """ Add an empty cluster and optionally a key for it, in one change. Returns the new key, if any. """
        with self._editing() as data:
            self._insert_cluster(data, name)

            return self._insert_key(data, key_name, [name], allowed_ips) if key_name else None

    def add_key(self, name: str, clusters: list[str], allowed_ips: list[str] | None = None) -> str:
        with self._editing() as data:
            return self._insert_key(data, name, clusters, allowed_ips)

    def rotate_key(self, name: str) -> str:
        """ Replace a key, keeping its clusters and allowed addresses. Returns the new key. """
        with self._editing() as data:
            token, key_hash = generate_key()
            self._find_key(data, name)['hash'] = key_hash

            return token

    def remove_key(self, name: str) -> None:
        with self._editing() as data:
            data['keys'].remove(self._find_key(data, name))

    def reload_service(self) -> bool:
        return self._pid_file.send_signal(signal.SIGHUP)

    @contextmanager
    def _editing(self) -> Iterator[dict]:
        with self._config_file.lock():
            data = self._config_file.read(check_owner=False)

            self._validate(data)

            yield data

            self._validate(data)
            self._config_file.write(data)

    def _find_key(self, data: dict, name: str) -> dict:
        for key in data.get('keys') or []:
            if key['name'] == name:
                return key

        raise UsCmdRelayRuntimeError(f'Key "{name}" does not exist')

    def _insert_cluster(self, data: dict, name: str) -> None:
        if data.get('clusters') is None:
            data['clusters'] = CommentedMap()

        if name in data['clusters']:
            raise UsCmdRelayRuntimeError(f'Cluster "{name}" already exists')

        data['clusters'][name] = CommentedMap()

    def _insert_key(self, data: dict, name: str, clusters: list[str], allowed_ips: list[str] | None) -> str:
        if data.get('keys') is None:
            data['keys'] = CommentedSeq()

        if any(key['name'] == name for key in data['keys']):
            raise UsCmdRelayRuntimeError(f'Key "{name}" already exists')

        token, key_hash = generate_key()
        entry = CommentedMap()

        entry['name'] = name
        entry['hash'] = key_hash
        entry['clusters'] = self._inline_list(clusters)

        if allowed_ips:
            entry['allowed_ips'] = self._inline_list(allowed_ips)

        data['keys'].append(entry)

        return token

    def _inline_list(self, items: list[str]) -> CommentedSeq:
        sequence = CommentedSeq(items)
        sequence.fa.set_flow_style()

        return sequence
