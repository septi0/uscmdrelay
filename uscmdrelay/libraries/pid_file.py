import fcntl
import os
import signal

__all__ = ['PidFile']

class PidFile:
    def __init__(self, path: str) -> None:
        self._path: str = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        """ Lock and write the pid file, held for the whole service lifetime. Returns False if another service holds it. """
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)

        # the lock is released by the kernel when the service dies, so a stale pid file never blocks a new service
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False

        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode('utf-8'))

        self._fd = fd

        return True

    def release(self) -> None:
        if self._fd is None:
            return

        try:
            os.unlink(self._path)
        finally:
            os.close(self._fd)
            self._fd = None

    def send_signal(self, sig: signal.Signals) -> bool:
        """ Signal the running service. Returns False if there is none, or it cannot be reached. """
        # a symlink could make root trust a file the service user does not own
        try:
            fd = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return False

        try:
            # the running service holds an exclusive lock, anything else is not a live pid
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                return False
            except BlockingIOError:
                pass

            self._kill_as(os.fstat(fd).st_uid, int(os.read(fd, 32)), sig)
            return True
        except (OSError, ValueError):
            return False
        finally:
            os.close(fd)

    def _kill_as(self, uid: int, pid: int, sig: signal.Signals) -> None:
        if os.getuid() != 0 or uid == 0:
            os.kill(pid, sig)
            return

        # whoever can write the pid file picks the target, so root signals with only that user's rights
        os.setresuid(uid, uid, 0)

        try:
            os.kill(pid, sig)
        finally:
            os.setresuid(0, 0, 0)
