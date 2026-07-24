"""Run a ClawJournal installer while holding its cross-process install lock.

This bootstrap helper intentionally uses only the Python standard library so
both platform installers can serialize before creating or modifying the venv.
Internal ``selfupdate.reinstall()`` calls already own the same lock and mark
their child environment to avoid recursive acquisition.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


LOCK_FILENAME = "reinstall.lock"
LOCK_HELD_ENV = "CLAWJOURNAL_INSTALL_LOCK_HELD"


def _lock_path() -> Path:
    return Path.home() / ".clawjournal" / LOCK_FILENAME


def _acquire_lock(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    waiting = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            while True:
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if not waiting:
                        print(
                            "[i] Another ClawJournal install is running; waiting...",
                            file=sys.stderr,
                        )
                        waiting = True
                    time.sleep(0.1)
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(
                    "[i] Another ClawJournal install is running; waiting...",
                    file=sys.stderr,
                )
                fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _release_lock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        print("install_lock.py: expected an installer command", file=sys.stderr)
        return 2

    fd = _acquire_lock(_lock_path())
    try:
        env = dict(os.environ)
        env[LOCK_HELD_ENV] = "1"
        try:
            return subprocess.run(command, env=env, check=False).returncode
        except OSError as exc:
            print(f"[x] Could not start the installer: {exc}", file=sys.stderr)
            return 1
    finally:
        _release_lock(fd)


if __name__ == "__main__":
    raise SystemExit(main())
