"""Conservative filesystem classification for SQLite state storage.

Only filesystem kinds that can be identified without exposing mount sources or
mount paths are returned.  Unknown platforms and unrecognised filesystem kinds
remain usable: callers fail closed only for an explicit, known network or
cluster filesystem.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

StorageRisk = Literal["network", "local", "unknown"]


class UnsafeStateStorageError(RuntimeError):
    """A write was refused because application state is on unsafe storage."""


@dataclass(frozen=True)
class FilesystemInfo:
    """Sanitised storage classification safe to include in diagnostics."""

    filesystem_type: str
    storage_risk: StorageRisk

    @property
    def storage_migration_required(self) -> bool:
        return self.storage_risk == "network"

    def health_fields(self) -> dict[str, str | bool]:
        return {
            "filesystem_type": self.filesystem_type,
            "storage_risk": self.storage_risk,
            "storage_migration_required": self.storage_migration_required,
        }


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


def storage_migration_message(info: FilesystemInfo) -> str:
    """Return the actionable, path-free message for unsafe SQLite storage."""

    filesystem_type = sanitized_filesystem_type(info.filesystem_type)
    return (
        "ClawJournal's state directory is on a network or shared filesystem "
        f"({filesystem_type}). Stop all ClawJournal processes, copy the "
        "entire state directory to private persistent local storage, set "
        "CLAWJOURNAL_HOME to that local directory, and restart before scanning "
        "or rebuilding the index."
    )
