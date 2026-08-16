"""Cross-process serialization for every AI scoring egress.

The durable queue lease protects ownership metadata in SQLite, but a scoring
call can legitimately outlive that lease.  This kernel-owned lock is the
second line of defence: background workers, explicit HTTP/CLI scoring, and
the Share workflow all take the same lock before reading the revision that
will be sent to a judge and hold it until the revision-CAS write completes.
The OS releases the lock automatically if a process crashes.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator


SCORING_EGRESS_LOCK_FILENAME = "scoring-egress.lock"
_LOCK_POLL_SECONDS = 0.05


def _lock_path() -> Path:
    # Import lazily so tests that isolate INDEX_DB before entering the context
    # receive an equally isolated lock file, without creating an import cycle.
    from ..workbench.index import INDEX_DB

    return Path(str(INDEX_DB)).parent / SCORING_EGRESS_LOCK_FILENAME


def _try_lock(file: BinaryIO) -> bool:
    if os.name == "nt":
        import msvcrt

        try:
            if os.fstat(file.fileno()).st_size == 0:
                file.write(b"0")
                file.flush()
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    import fcntl

    try:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock(file: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(file.fileno(), fcntl.LOCK_UN)


@contextmanager
def scoring_egress_lock(
    *,
    blocking: bool,
    timeout: float | None = None,
) -> Iterator[bool]:
    """Yield whether the installation-wide scoring egress lock was acquired.

    Background workers use ``blocking=False`` so a second daemon never claims
    work that could expire while it waits.  Explicit user actions block by
    default; callers may provide a finite timeout when their transport has a
    deadline.  Keeping the file handle open holds the OS lock.
    """

    if timeout is not None and timeout < 0:
        raise ValueError("timeout must be non-negative")
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    file = path.open("a+b")
    acquired = False
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while True:
            acquired = _try_lock(file)
            if acquired or not blocking:
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(_LOCK_POLL_SECONDS)
        yield acquired
    finally:
        if acquired:
            try:
                _unlock(file)
            except OSError:
                pass
        file.close()
