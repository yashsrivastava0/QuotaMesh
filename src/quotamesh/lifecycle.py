"""OS-held local process lock. A crash releases the lock without stale PID guesses."""

import os
from contextlib import contextmanager


@contextmanager
def process_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(directory / "gateway.lock", os.O_RDWR | os.O_CREAT, 0o600)
    stream = os.fdopen(descriptor, "r+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            if not os.fstat(descriptor).st_size:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError(
                    "Another QuotaMesh gateway is using this data directory."
                ) from None
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError(
                    "Another QuotaMesh gateway is using this data directory."
                ) from None
        locked = True
        yield
    finally:
        if locked:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()
