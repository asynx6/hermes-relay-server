"""Lock satu-instans: cegah `poll` dan cron `fetch-once` berjalan bersamaan
dengan cursor yang sama (pesan bisa terproses dua kali atau balapan).

Cara pakai:
    with instance_lock(lock_path):
        poller.run()

Kalau lock sudah dipegang proses lain, keluar dengan SystemExit.
"""
import errno
import os
import sys
from pathlib import Path


class InstanceLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise SystemExit(
                "Hermes agent lain sedang berjalan untuk direktori ini "
                f"(lock: {self.path}). Hentikan dulu, atau jangan jalankan "
                "cron fetch-onse bersamaan dengan poll."
            )
        try:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
        except OSError:
            pass
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def instance_lock(path: str | Path) -> InstanceLock:
    return InstanceLock(path)
