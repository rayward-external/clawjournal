"""Conservative filesystem classification for SQLite state storage.

Only filesystem kinds that can be identified without exposing mount sources or
mount paths are returned.  Unknown platforms and unrecognised filesystem kinds
remain usable: callers fail closed only for an explicit, known network or
cluster filesystem.

Some machines have no private, persistent local storage at all (HPC login
nodes typically mount home on NFS, scratch on Lustre, and purge /tmp). For
those, the user can explicitly allow the state root to stay on network storage
from one machine at a time; see ``allow_network_storage``.
"""

from __future__ import annotations

import dataclasses
import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal

StorageRisk = Literal["network", "local", "unknown"]
# Whether the state root on network storage was explicitly allowed for this
# machine. "other_machine" also covers an unreadable setting: nothing proves
# that this machine is the only one using the state.
NetworkStorageClaim = Literal["none", "this_machine", "other_machine"]
NetworkStorageResult = Literal[
    "not_needed",
    "already_allowed",
    "allowed",
    "taken_over",
    "claimed_elsewhere",
    "locks_unsupported",
]

NETWORK_STORAGE_FILENAME = "network-storage.json"
_NETWORK_STORAGE_VERSION = 1
_NETWORK_STORAGE_DIGEST_CONTEXT = b"clawjournal-network-storage-machine-v1"
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")


class UnsafeStateStorageError(RuntimeError):
    """A write was refused because application state is on unsafe storage."""


@dataclass(frozen=True)
class FilesystemInfo:
    """Sanitised storage classification safe to include in diagnostics."""

    filesystem_type: str
    storage_risk: StorageRisk
    network_storage_claim: NetworkStorageClaim = "none"

    @property
    def network_storage_allowed(self) -> bool:
        return (
            self.storage_risk == "network"
            and self.network_storage_claim == "this_machine"
        )

    @property
    def storage_migration_required(self) -> bool:
        return self.storage_risk == "network" and not self.network_storage_allowed

    def health_fields(self) -> dict[str, str | bool]:
        fields: dict[str, str | bool] = {
            "filesystem_type": self.filesystem_type,
            "storage_risk": self.storage_risk,
            "storage_migration_required": self.storage_migration_required,
        }
        if self.storage_risk == "network" and self.network_storage_claim != "none":
            fields["network_storage_claim"] = self.network_storage_claim
        return fields


_NETWORK_FILESYSTEM_ALIASES = {
    "9p": "9p",
    "afs": "afs",
    "beegfs": "beegfs",
    "blobfuse": "blobfuse",
    "blobfuse2": "blobfuse",
    "ceph": "ceph",
    "cifs": "cifs",
    "cvmfs": "cvmfs",
    "davfs": "davfs",
    "davfs2": "davfs",
    "fuse.afs": "afs",
    "fuse.beegfs": "beegfs",
    "fuse.ceph": "ceph",
    "fuse.cvmfs": "cvmfs",
    "fuse.glusterfs": "glusterfs",
    "fuse.gcsfuse": "gcsfuse",
    "fuse.juicefs": "juicefs",
    "fuse.rclone": "rclone",
    "fuse.s3fs": "s3fs",
    "fuse.sshfs": "sshfs",
    "fuse.weka": "weka",
    "gfs2": "gfs2",
    "gcsfuse": "gcsfuse",
    "glusterfs": "glusterfs",
    "gpfs": "gpfs",
    "juicefs": "juicefs",
    "lustre": "lustre",
    "nfs": "nfs",
    "nfs4": "nfs4",
    "ocfs2": "ocfs2",
    "orangefs": "orangefs",
    "panfs": "panfs",
    "pvfs2": "orangefs",
    "rclone": "rclone",
    "s3fs": "s3fs",
    "smb2": "smbfs",
    "smb3": "smbfs",
    "smbfs": "smbfs",
    "sshfs": "sshfs",
    "virtiofs": "virtiofs",
    "weka": "weka",
}

_LOCAL_FILESYSTEM_ALIASES = {
    "apfs": "apfs",
    "btrfs": "btrfs",
    "exfat": "exfat",
    "ext2": "ext2",
    "ext3": "ext3",
    "ext4": "ext4",
    "f2fs": "f2fs",
    "hfs": "hfs",
    "hfsplus": "hfsplus",
    "jfs": "jfs",
    "ntfs": "ntfs",
    "ntfs3": "ntfs3",
    "ramfs": "ramfs",
    "reiserfs": "reiserfs",
    "tmpfs": "tmpfs",
    "ufs": "ufs",
    "vfat": "vfat",
    "xfs": "xfs",
    "zfs": "zfs",
}

_KNOWN_UNKNOWN_FILESYSTEM_ALIASES = {
    "autofs": "autofs",
    "cgroup": "cgroup",
    "cgroup2": "cgroup2",
    "devtmpfs": "devtmpfs",
    "ecryptfs": "ecryptfs",
    "fuse": "fuse",
    "fuseblk": "fuseblk",
    "iso9660": "iso9660",
    "nsfs": "nsfs",
    "overlay": "overlay",
    "overlayfs": "overlay",
    "proc": "proc",
    "squashfs": "squashfs",
    "sysfs": "sysfs",
    "udf": "udf",
}

_MOUNTINFO_ESCAPE = re.compile(r"\\([0-7]{3})")
_SAFE_FILESYSTEM_TYPE = re.compile(r"[a-z0-9._+-]{1,32}\Z")


def _unescape_mountinfo_field(value: str) -> str:
    """Decode the octal escapes used for paths in proc mountinfo."""

    return _MOUNTINFO_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def _filesystem_info(filesystem_type: str) -> FilesystemInfo:
    normalized = filesystem_type.strip().lower()
    if _SAFE_FILESYSTEM_TYPE.fullmatch(normalized) is None:
        return FilesystemInfo("unknown", "unknown")
    if normalized in _NETWORK_FILESYSTEM_ALIASES:
        return FilesystemInfo(
            _NETWORK_FILESYSTEM_ALIASES[normalized],
            "network",
        )
    if normalized in _LOCAL_FILESYSTEM_ALIASES:
        return FilesystemInfo(
            _LOCAL_FILESYSTEM_ALIASES[normalized],
            "local",
        )
    if normalized in _KNOWN_UNKNOWN_FILESYSTEM_ALIASES:
        return FilesystemInfo(
            _KNOWN_UNKNOWN_FILESYSTEM_ALIASES[normalized],
            "unknown",
        )
    # FUSE subtypes can be user-chosen strings. Unknown kernel kinds therefore
    # cannot be returned verbatim in support-safe health payloads.
    return FilesystemInfo("unknown", "unknown")


def sanitized_filesystem_type(value: object) -> str:
    """Return only a fixed, support-safe filesystem identifier."""

    return _filesystem_info(str(value or "")).filesystem_type


def _classify_linux_mountinfo(path: Path, mountinfo: str) -> FilesystemInfo:
    """Classify *path* using the longest matching mountinfo mount point."""

    # mountinfo paths always use Linux/POSIX syntax.  Keeping the parser on
    # PurePosixPath also lets its behaviour be tested from non-Linux clients.
    target = PurePosixPath(path.as_posix())

    best_specificity = -1
    best_filesystem_types: list[str] = []
    for line in mountinfo.splitlines():
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        mount_fields = before.split()
        filesystem_fields = after.split()
        if len(mount_fields) < 5 or not filesystem_fields:
            continue
        mount_point = PurePosixPath(_unescape_mountinfo_field(mount_fields[4]))
        try:
            target.relative_to(mount_point)
        except (OSError, ValueError):
            continue
        specificity = len(mount_point.parts)
        if specificity > best_specificity:
            best_specificity = specificity
            best_filesystem_types = [filesystem_fields[0]]
        elif specificity == best_specificity:
            best_filesystem_types.append(filesystem_fields[0])

    if not best_filesystem_types:
        return FilesystemInfo("unknown", "unknown")
    candidates = [_filesystem_info(value) for value in best_filesystem_types]
    # Stacked mounts can expose multiple entries at the same mountpoint, and
    # mount IDs do not define which layer is visible. Avoid a false-local
    # admission: any network candidate makes the ambiguous stack unsafe. If
    # the sanitised candidates agree, their shared classification is safe;
    # otherwise degrade to unknown rather than guessing.
    for candidate in candidates:
        if candidate.storage_risk == "network":
            return candidate
    if all(candidate == candidates[0] for candidate in candidates[1:]):
        return candidates[0]
    return FilesystemInfo("unknown", "unknown")


def _read_linux_mountinfo() -> str | None:
    """Read the kernel mount table without exposing it outside this module."""

    try:
        return Path("/proc/self/mountinfo").read_text(
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None


def classify_filesystem(path: Path) -> FilesystemInfo:
    """Return a sanitised, conservative classification for *path*.

    Linux exposes the mounted filesystem kind in ``/proc/self/mountinfo``.
    Other platforms deliberately degrade to ``unknown`` until an equally
    reliable, source-free implementation is available.
    """

    if not sys.platform.startswith("linux"):
        return FilesystemInfo("unknown", "unknown")

    mountinfo = _read_linux_mountinfo()
    if mountinfo is None:
        return FilesystemInfo("unknown", "unknown")

    # A direct path on a hard, disconnected NFS mount can block in resolve()
    # before the daemon has a chance to show migration guidance. Match the
    # lexical absolute path first and stop immediately when mountinfo already
    # proves it is network-backed. Local/unknown paths still resolve so a
    # symlink into shared storage cannot bypass the guard.
    lexical_target = Path(os.path.abspath(Path(path)))
    lexical_info = _classify_linux_mountinfo(lexical_target, mountinfo)
    if lexical_info.storage_migration_required:
        return lexical_info

    try:
        target = Path(path).resolve(strict=False)
    except (OSError, RuntimeError):
        return lexical_info

    # Resolving can trigger autofs or race a remount, so classify against a
    # fresh kernel snapshot instead of the one read before resolve().
    refreshed_mountinfo = _read_linux_mountinfo()
    if refreshed_mountinfo is None:
        return FilesystemInfo("unknown", "unknown")
    return _classify_linux_mountinfo(target, refreshed_mountinfo)


def _network_storage_machine_digest(nonce: str) -> str:
    """Return a salted, non-reversible identifier for this machine."""

    hostname = socket.gethostname().strip().lower()
    return hashlib.sha256(
        b"\0".join((
            _NETWORK_STORAGE_DIGEST_CONTEXT,
            nonce.encode("ascii"),
            hostname.encode("utf-8", "surrogateescape"),
        ))
    ).hexdigest()


def _network_storage_claim(state_dir: Path) -> NetworkStorageClaim:
    """Read whether network storage was allowed for this machine."""

    try:
        payload = json.loads(
            (state_dir / NETWORK_STORAGE_FILENAME).read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return "none"
    except (OSError, ValueError):
        return "other_machine"
    if not isinstance(payload, dict):
        return "other_machine"
    nonce = payload.get("machine_nonce")
    digest = payload.get("machine_digest")
    if (
        payload.get("version") != _NETWORK_STORAGE_VERSION
        or not isinstance(nonce, str)
        or _HEX_64.fullmatch(nonce) is None
        or not isinstance(digest, str)
        or _HEX_64.fullmatch(digest) is None
    ):
        return "other_machine"
    if hmac.compare_digest(digest, _network_storage_machine_digest(nonce)):
        return "this_machine"
    return "other_machine"


def classify_state_storage(database: Path) -> FilesystemInfo:
    """Classify the index location, honoring an explicit network allowance.

    The allowance lives beside the index in the state root. Use the lexical
    parent: resolving a known network path can block on a disconnected hard
    mount.
    """

    info = classify_filesystem(database)
    if info.storage_risk != "network":
        return info
    state_dir = Path(os.path.abspath(Path(database))).parent
    return dataclasses.replace(
        info,
        network_storage_claim=_network_storage_claim(state_dir),
    )


def _file_locks_supported(directory: Path) -> bool:
    """Probe the advisory locks SQLite and the index lease rely on.

    Some cluster mounts (for example Lustre without ``flock``) reject them.
    """

    if os.name == "nt":
        return True
    import fcntl

    probe = directory / f".{NETWORK_STORAGE_FILENAME}.{secrets.token_hex(8)}.probe"
    descriptor = os.open(probe, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, b"0")
        try:
            # SQLite's unix VFS uses fcntl byte-range locks; the index
            # connection lease uses flock.
            fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.lockf(descriptor, fcntl.LOCK_UN)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            return False
    finally:
        os.close(descriptor)
        probe.unlink(missing_ok=True)
    return True


def _fsync_directory(directory: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    except OSError as exc:
        # Some network filesystems do not support fsync on directories.
        if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise
    finally:
        os.close(descriptor)


def allow_network_storage(
    state_dir: Path,
    *,
    take_over: bool = False,
) -> tuple[NetworkStorageResult, FilesystemInfo]:
    """Allow the state root to stay on network storage for this machine only.

    SQLite's rollback journal (the index never uses WAL) is reliable on
    network storage while every process using it runs on one machine, so
    cross-machine lock and cache coherence never matter. The allowance records a
    salted identifier for this machine; every other machine stays blocked
    until the user explicitly moves the allowance with ``take_over``.
    """

    state_dir = Path(os.path.abspath(Path(state_dir)))
    database = state_dir / "index.db"
    info = classify_state_storage(database)
    if info.storage_risk != "network":
        return "not_needed", info
    if info.network_storage_claim == "this_machine":
        return "already_allowed", info
    replacing = info.network_storage_claim == "other_machine"
    if replacing and not take_over:
        return "claimed_elsewhere", info

    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not _file_locks_supported(state_dir):
        return "locks_unsupported", info

    nonce = secrets.token_hex(32)
    payload = json.dumps(
        {
            "version": _NETWORK_STORAGE_VERSION,
            "allowed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "machine_nonce": nonce,
            "machine_digest": _network_storage_machine_digest(nonce),
        },
        indent=2,
        sort_keys=True,
    ) + "\n"
    target = state_dir / NETWORK_STORAGE_FILENAME
    if replacing:
        temporary = state_dir / f".{NETWORK_STORAGE_FILENAME}.{secrets.token_hex(8)}.tmp"
        try:
            _write_new_file(temporary, payload)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        try:
            # O_EXCL keeps a concurrent allowance from another machine from
            # being silently overwritten. A partial file reads as another
            # machine's claim, so a reader can only fail closed.
            _write_new_file(target, payload)
        except FileExistsError:
            claimed = classify_state_storage(database)
            if claimed.network_storage_claim == "this_machine":
                return "already_allowed", claimed
            return "claimed_elsewhere", claimed
    _fsync_directory(state_dir)
    return ("taken_over" if replacing else "allowed"), classify_state_storage(database)


def _write_new_file(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())


def storage_migration_message(info: FilesystemInfo) -> str:
    """Return the actionable, path-free message for unsafe SQLite storage."""

    filesystem_type = sanitized_filesystem_type(info.filesystem_type)
    if info.network_storage_claim == "other_machine":
        return (
            "ClawJournal's state directory is on network storage "
            f"({filesystem_type}) that is set up for use from another machine. "
            "Stop ClawJournal on that machine, then run "
            "clawjournal storage allow-network --take-over on this one and "
            "restart. Never use the same state from two machines at once."
        )
    return (
        "ClawJournal's state directory is on a network or shared filesystem "
        f"({filesystem_type}). Stop all ClawJournal processes, copy the "
        "entire state directory to private persistent local storage, set "
        "CLAWJOURNAL_HOME to that local directory, and restart before scanning "
        "or rebuilding the index. If this machine has no persistent local "
        "storage (common on HPC clusters), instead run clawjournal storage "
        "allow-network to keep the state in place for this machine only, then "
        "restart."
    )
