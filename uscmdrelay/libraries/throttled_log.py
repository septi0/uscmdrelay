import logging
import time

__all__ = ['ThrottledLog']

class ThrottledLog:
    """ Logs a repeated warning at most once per interval, with a count. """

    def __init__(self, logger: logging.Logger, interval: float = 60) -> None:
        self._logger: logging.Logger = logger
        self._interval: float = interval
        self._count: int = 0
        self._next_report: float = 0

    def warning(self, message: str) -> None:
        self._count += 1

        if time.monotonic() >= self._next_report:
            self._logger.warning(f'{message} ({self._count} since the last report)')
            self._count = 0
            self._next_report = time.monotonic() + self._interval
