import hashlib
import hmac
from ipaddress import ip_address
from uscmdrelay.models.key import KeyModel

__all__ = ['AuthService', 'hash_token']

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8', errors='replace')).hexdigest()

class AuthService:
    def __init__(self, keys: list[KeyModel]) -> None:
        self._keys: list[KeyModel] = keys

    def find_key(self, token: object, peer_ip: str) -> KeyModel | None:
        """ Find the key matching the token, if it may be used from this address. """
        if not isinstance(token, str):
            return None

        digest = hash_token(token)
        match = None

        for key in self._keys:
            if hmac.compare_digest(digest, key.digest):
                match = key

        if match is None or not self._is_ip_allowed(match, peer_ip):
            return None

        return match

    def is_granted(self, key: KeyModel, cluster: str) -> bool:
        return '*' in key.clusters or cluster in key.clusters

    def _is_ip_allowed(self, key: KeyModel, peer_ip: str) -> bool:
        if key.allowed_ips is None:
            return True

        try:
            ip = ip_address(peer_ip)
        except ValueError:
            return False

        # ipv4 clients of a dual stack listener are seen as ::ffff:a.b.c.d
        ip = getattr(ip, 'ipv4_mapped', None) or ip

        return any(ip in network for network in key.allowed_ips)
