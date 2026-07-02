"""Install-scoped filesystem primitives.

`~/.clawjournal/hash_salt` seeds the salted sha256 used for `entity_hash`
throughout the findings pipeline; `~/.clawjournal/api_token` gates the
loopback daemon API. Both files are created exactly once per install via
a write-then-link handshake so a CLI/daemon race on a fresh install
cannot produce divergent values or expose a partially-written file (see
docs/security-refactor.md §Storage sensitivity and §Daemon API surface).
"""

from __future__ import annotations

import errno
import os
import secrets
import tempfile
from pathlib import Path

HASH_SALT_FILENAME = "hash_salt"
API_TOKEN_FILENAME = "api_token"

HASH_SALT_BYTES = 32
API_TOKEN_BYTES = 32


def atomic_write_text(path: Path, text: str, *, parents: bool = False) -> None:
    """Write *text* to *path* atomically (tmp in the same dir + fsync + os.replace).

    One shared helper so the skill installer, bundle export, and other writers don't
    each carry a private copy that can drift on a durability fix. ``parents=True``
    creates the parent directory first.
    """
    if parents:
        path.parent.mkdir(parents=True, exist_ok=True)
    # No leading dot: keep the temp name the same length as the target so a long but
    # legal output filename can't tip mkstemp over the OS NAME_MAX limit.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_ensure(path: Path, byte_count: int, *, token_hex: bool) -> bytes:
    """Create `path` with random bytes if missing; otherwise read existing.

    Uses the write-then-link pattern: write a full payload to a uniquely
    named tmp file in the same directory, fsync, then atomically link
    it into place. `os.link` fails with EEXIST if another caller won
    the race, in which case we unlink our tmp file and read the
    winner's bytes. The target is therefore either absent or complete
    — no observer ever sees a zero-byte or partial file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    # Fast path: target already exists and is non-empty.
    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        existing = b""
    if existing:
        return existing

    payload = (
        secrets.token_hex(byte_count).encode("ascii") if token_hex
        else secrets.token_bytes(byte_count)
    )

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)

    try:
        os.link(tmp_name, str(path))
        return payload
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise
        return path.read_bytes()
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def ensure_hash_salt(install_dir: Path) -> bytes:
    """Return the per-install hash salt, creating it atomically if absent."""
    return _atomic_ensure(install_dir / HASH_SALT_FILENAME, HASH_SALT_BYTES, token_hex=False)


def ensure_api_token(install_dir: Path) -> str:
    """Return the per-install API bearer token as hex, creating it atomically if absent."""
    return _atomic_ensure(install_dir / API_TOKEN_FILENAME, API_TOKEN_BYTES, token_hex=True).decode("ascii")


def ensure_install_files(install_dir: Path) -> tuple[bytes, str]:
    """Bootstrap both secrets-scoped files. Called from `open_index()`."""
    return ensure_hash_salt(install_dir), ensure_api_token(install_dir)
