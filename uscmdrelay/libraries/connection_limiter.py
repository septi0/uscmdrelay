import logging
from uscmdrelay.libraries.throttled_log import ThrottledLog

__all__ = ['ConnectionLimiter']

class ConnectionLimiter:
    def __init__(self, max_total: int, max_per_ip: int, *, logger: logging.Logger) -> None:
        self._max_total: int = max_total
        self._max_per_ip: int = max_per_ip
        self._refused_log: ThrottledLog = ThrottledLog(logger)
        self._per_ip: dict[str, int] = {}
        self._total: int = 0

    @property
    def total(self) -> int:
        return self._total

    def acquire(self, ip: str) -> bool:
        if self._total >= self._max_total or self._per_ip.get(ip, 0) >= self._max_per_ip:
            self._refused_log.warning(f'Connection limit reached, refused {ip}')
            return False

        self._total += 1
        self._per_ip[ip] = self._per_ip.get(ip, 0) + 1

        return True

    def release(self, ip: str) -> None:
        self._total -= 1
        self._per_ip[ip] -= 1

        if not self._per_ip[ip]:
            del self._per_ip[ip]
