import logging
import socket
import pytest
from uscmdrelay.models.uscmdrelay import UsCmdRelayModel
from uscmdrelay.services.auth import hash_token
from uscmdrelay.services.relay import RelayService

TOKEN = 'test-token'
LIMITED_TOKEN = 'limited-token'

def free_ports(count: int) -> list[int]:
    # held open together, so the ports returned are always different
    sockets = [socket.socket() for _ in range(count)]

    try:
        for s in sockets:
            s.bind(('127.0.0.1', 0))

        return [s.getsockname()[1] for s in sockets]
    finally:
        for s in sockets:
            s.close()

def build_model(clusters: dict, *, limits: dict | None = None, tcp: int = 1, http: int = 2, on_failure: str | None = None) -> UsCmdRelayModel:
    return UsCmdRelayModel.model_validate({
        'listeners': {'tcp': {'port': tcp}, 'http': {'port': http}},
        'limits': limits or {},
        'on_failure': on_failure,
        'keys': [
            {'name': 'admin', 'hash': f'sha256:{hash_token(TOKEN)}', 'clusters': ['*']},
            {'name': 'limited', 'hash': f'sha256:{hash_token(LIMITED_TOKEN)}', 'clusters': ['deploy']},
        ],
        'clusters': {'deploy': {}, **clusters},
    })

@pytest.fixture
def make_relay(tmp_path):
    def factory(commands: dict, *, limits: dict | None = None, on_failure: str | None = None) -> RelayService:
        return RelayService(build_model({'default': commands}, limits=limits, on_failure=on_failure), work_dir=str(tmp_path), logger=logging.getLogger('test'))

    return factory
