from __future__ import annotations

import os
import stat
from pathlib import Path


class RuntimeInstanceLock:
    """Fail closed when two API workers would own terminal side effects.

    SQLite state and event subscriptions are safe across processes, but PTYs,
    tmux recovery, probes and the local control socket have exactly one owner.
    Keep this advisory lock for the full application lifespan instead of
    letting a second Uvicorn worker partially initialize those resources.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._descriptor: int | None = None

    @property
    def acquired(self) -> bool:
        return self._descriptor is not None

    def acquire(self) -> None:
        if self._descriptor is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError("runtime instance lock is not a regular file")
            if os.name != "nt" and info.st_uid != os.geteuid():
                raise RuntimeError("runtime instance lock is not owned by the service uid")
            os.fchmod(descriptor, 0o600)
            try:
                if os.name == "nt":
                    import msvcrt

                    if info.st_size == 0:
                        os.write(descriptor, b"\0")
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as error:
                raise RuntimeError(
                    "another AgentServer runtime owns this DATA_DIR; "
                    "run exactly one API worker"
                ) from error
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(descriptor)
            self._descriptor = descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def release(self) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "RuntimeInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_error: object) -> None:
        self.release()
