"""Strictly bounded runtime context for local support-report drafts.

The HTTP endpoint that consumes this module must remain usable while the
workbench index is unavailable.  Collection is therefore split in two:

* :func:`capture_support_environment` runs once while the daemon starts and
  records only coarse, allowlisted process metadata.
* :func:`collect_support_context` projects that immutable snapshot together
  with the already-cached index-health mapping.  It performs no filesystem,
  SQLite, subprocess, Git, or network work.

Neither function accepts request data.  Unknown and malformed values degrade
to fixed placeholders instead of being stringified into the report.
"""

from __future__ import annotations

import platform
import re
import sys
from dataclasses import dataclass
from typing import Mapping

from . import __version__


SUPPORT_CONTEXT_SCHEMA_VERSION = 1

_SAFE_PACKAGE_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}\Z")
_SAFE_REVISION_RE = re.compile(r"[0-9a-fA-F]{7,64}\Z")
_NUMERIC_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){0,3}\Z")
_OS_RELEASE_PREFIX_RE = re.compile(r"[0-9]+(?:\.[0-9]+){0,3}")

_OS_FAMILY_ALIASES = {
    "darwin": "macOS",
    "freebsd": "FreeBSD",
    "linux": "Linux",
    "windows": "Windows",
}
_ARCHITECTURE_ALIASES = {
    "aarch64": "arm64",
    "amd64": "x86_64",
    "arm64": "arm64",
    "i386": "x86",
    "i686": "x86",
    "ppc64le": "ppc64le",
    "riscv64": "riscv64",
    "s390x": "s390x",
    "x86": "x86",
    "x86_64": "x86_64",
}
_INDEX_HEALTH_STATUSES = frozenset({
    "ready",
    "checking",
    "recovery_required",
    "rebuilding",
    "unavailable",
})
_STORAGE_RISKS = frozenset({"network", "local", "unknown"})
_FILESYSTEM_TYPES = frozenset({
    "unknown",
    "9p",
    "afs",
    "beegfs",
    "blobfuse",
    "ceph",
    "cifs",
    "cvmfs",
    "davfs",
    "gfs2",
    "gcsfuse",
    "glusterfs",
    "gpfs",
    "juicefs",
    "lustre",
    "nfs",
    "nfs4",
    "ocfs2",
    "orangefs",
    "panfs",
    "rclone",
    "s3fs",
    "smbfs",
    "sshfs",
    "virtiofs",
    "weka",
    "apfs",
    "btrfs",
    "exfat",
    "ext2",
    "ext3",
    "ext4",
    "f2fs",
    "hfs",
    "hfsplus",
    "jfs",
    "ntfs",
    "ntfs3",
    "ramfs",
    "reiserfs",
    "tmpfs",
    "ufs",
    "vfat",
    "xfs",
    "zfs",
    "autofs",
    "cgroup",
    "cgroup2",
    "devtmpfs",
    "ecryptfs",
    "fuse",
    "fuseblk",
    "iso9660",
    "nsfs",
    "overlay",
    "proc",
    "squashfs",
    "sysfs",
    "udf",
})
_UNAVAILABLE_SECTION_ORDER = (
    "package_version",
    "package_revision",
    "runtime_python",
    "runtime_sqlite",
    "runtime_os",
    "runtime_architecture",
    "expected_schema",
    "cached_index_health",
)


@dataclass(frozen=True)
class SupportEnvironment:
    """Immutable, privacy-bounded metadata captured at daemon startup."""

    package_version: str
    package_revision: str | None
    python_version: str
    sqlite_version: str
    os_family: str
    os_release: str
    architecture: str
    expected_user_version: int | None
    unavailable_sections: tuple[str, ...] = ()


def _safe_package_version(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    value = value.strip()
    return value if _SAFE_PACKAGE_VERSION_RE.fullmatch(value) else "unknown"


def _safe_revision(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    if _SAFE_REVISION_RE.fullmatch(value) is None:
        return None
    # A short source revision distinguishes editable builds without exposing
    # any machine- or participant-specific identifier.
    return value[:12]


def _safe_numeric_version(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    value = value.strip()
    return value if _NUMERIC_VERSION_RE.fullmatch(value) else "unknown"


def _safe_os_family(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    return _OS_FAMILY_ALIASES.get(value.strip().lower(), "unknown")


def _safe_os_release(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    match = _OS_RELEASE_PREFIX_RE.match(value.strip())
    return match.group(0) if match else "unknown"


def _safe_architecture(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    return _ARCHITECTURE_ALIASES.get(value.strip().lower(), "unknown")


def _safe_expected_schema(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= 1_000_000 else None


def _safe_filesystem_type(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    value = value.strip().lower()
    return value if value in _FILESYSTEM_TYPES else "unknown"


def _ordered_unavailable(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    requested = set(values)
    return tuple(
        section for section in _UNAVAILABLE_SECTION_ORDER if section in requested
    )


def unavailable_support_environment() -> SupportEnvironment:
    """Return a fixed startup snapshot for an unexpected collection failure."""

    return SupportEnvironment(
        package_version="unknown",
        package_revision=None,
        python_version="unknown",
        sqlite_version="unknown",
        os_family="unknown",
        os_release="unknown",
        architecture="unknown",
        expected_user_version=None,
        unavailable_sections=_UNAVAILABLE_SECTION_ORDER[:-1],
    )


def capture_support_environment(
    *,
    revision: object,
    expected_user_version: object,
    sqlite_version: object,
) -> SupportEnvironment:
    """Capture coarse process metadata once, without reading user state."""

    unavailable: list[str] = []

    package_version = _safe_package_version(__version__)
    if package_version == "unknown":
        unavailable.append("package_version")

    package_revision = _safe_revision(revision)
    if package_revision is None:
        unavailable.append("package_revision")

    python_version = _safe_numeric_version(
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    if python_version == "unknown":
        unavailable.append("runtime_python")

    safe_sqlite_version = _safe_numeric_version(sqlite_version)
    if safe_sqlite_version == "unknown":
        unavailable.append("runtime_sqlite")

    try:
        raw_os_family = platform.system()
        raw_os_release = platform.release()
    except Exception:
        raw_os_family = None
        raw_os_release = None
        unavailable.append("runtime_os")
    os_family = _safe_os_family(raw_os_family)
    os_release = _safe_os_release(raw_os_release)
    if (
        "runtime_os" not in unavailable
        and (os_family == "unknown" or os_release == "unknown")
    ):
        unavailable.append("runtime_os")

    try:
        raw_architecture = platform.machine()
    except Exception:
        raw_architecture = None
        unavailable.append("runtime_architecture")
    architecture = _safe_architecture(raw_architecture)
    if architecture == "unknown" and "runtime_architecture" not in unavailable:
        unavailable.append("runtime_architecture")

    expected_schema = _safe_expected_schema(expected_user_version)
    if expected_schema is None:
        unavailable.append("expected_schema")

    return SupportEnvironment(
        package_version=package_version,
        package_revision=package_revision,
        python_version=python_version,
        sqlite_version=safe_sqlite_version,
        os_family=os_family,
        os_release=os_release,
        architecture=architecture,
        expected_user_version=expected_schema,
        unavailable_sections=_ordered_unavailable(unavailable),
    )


def _project_environment(value: object) -> SupportEnvironment:
    """Revalidate even the process-local snapshot before serialization."""

    if not isinstance(value, SupportEnvironment):
        return unavailable_support_environment()
    unavailable = [
        section
        for section in value.unavailable_sections
        if isinstance(section, str) and section in _UNAVAILABLE_SECTION_ORDER
    ]

    package_version = _safe_package_version(value.package_version)
    if package_version == "unknown":
        unavailable.append("package_version")
    package_revision = _safe_revision(value.package_revision)
    if package_revision is None:
        unavailable.append("package_revision")
    python_version = _safe_numeric_version(value.python_version)
    if python_version == "unknown":
        unavailable.append("runtime_python")
    sqlite_version = _safe_numeric_version(value.sqlite_version)
    if sqlite_version == "unknown":
        unavailable.append("runtime_sqlite")
    os_family = _safe_os_family(value.os_family)
    os_release = _safe_os_release(value.os_release)
    if os_family == "unknown" or os_release == "unknown":
        unavailable.append("runtime_os")
    architecture = _safe_architecture(value.architecture)
    if architecture == "unknown":
        unavailable.append("runtime_architecture")
    expected_user_version = _safe_expected_schema(value.expected_user_version)
    if expected_user_version is None:
        unavailable.append("expected_schema")

    return SupportEnvironment(
        package_version=package_version,
        package_revision=package_revision,
        python_version=python_version,
        sqlite_version=sqlite_version,
        os_family=os_family,
        os_release=os_release,
        architecture=architecture,
        expected_user_version=expected_user_version,
        unavailable_sections=_ordered_unavailable(unavailable),
    )


def _unknown_health() -> dict[str, object]:
    return {
        "filesystem_type": "unknown",
        "storage_risk": "unknown",
        "storage_migration_required": False,
        "status": "unknown",
        "condition": None,
    }


def _project_cached_health(value: object) -> tuple[dict[str, object], bool]:
    """Project only fixed fields from the in-memory health snapshot."""

    if not isinstance(value, Mapping):
        return _unknown_health(), False
    try:
        raw_filesystem_type = value.get("filesystem_type")
        raw_storage_risk = value.get("storage_risk")
        raw_migration_required = value.get("storage_migration_required")
        raw_status = value.get("status")
        raw_code = value.get("code")
        interrupted_recovery = value.get("interrupted_recovery")
    except Exception:
        return _unknown_health(), False

    filesystem_type = _safe_filesystem_type(raw_filesystem_type)
    storage_risk = (
        raw_storage_risk
        if isinstance(raw_storage_risk, str)
        and raw_storage_risk in _STORAGE_RISKS
        else "unknown"
    )
    status = (
        raw_status
        if isinstance(raw_status, str) and raw_status in _INDEX_HEALTH_STATUSES
        else "unknown"
    )
    migration_required = (
        raw_migration_required is True or storage_risk == "network"
    )

    condition: str | None = None
    if (
        isinstance(raw_code, str)
        and raw_code == "storage_migration_required"
    ) or migration_required:
        condition = "storage_migration_required"
    elif interrupted_recovery is True:
        condition = "interrupted_recovery"
    elif status == "recovery_required":
        condition = "recovery_required"
    elif status == "unavailable":
        condition = "unavailable"

    projected = {
        "filesystem_type": filesystem_type,
        "storage_risk": storage_risk,
        "storage_migration_required": migration_required,
        "status": status,
        "condition": condition,
    }
    return projected, status != "unknown"


def collect_support_context(
    environment: object,
    cached_index_health: object,
) -> dict[str, object]:
    """Build the exact support-context response from process-local snapshots."""

    safe_environment = _project_environment(environment)
    unavailable = list(safe_environment.unavailable_sections)

    health, health_available = _project_cached_health(cached_index_health)
    if not health_available:
        unavailable.append("cached_index_health")
    unavailable_sections = _ordered_unavailable(unavailable)

    return {
        "support_context_schema_version": SUPPORT_CONTEXT_SCHEMA_VERSION,
        "kind": "workbench",
        "package": {
            "version": safe_environment.package_version,
            "revision": safe_environment.package_revision,
        },
        "runtime": {
            "python_version": safe_environment.python_version,
            "sqlite_version": safe_environment.sqlite_version,
            "os_family": safe_environment.os_family,
            "os_release": safe_environment.os_release,
            "architecture": safe_environment.architecture,
        },
        "schema": {
            "expected_user_version": safe_environment.expected_user_version,
        },
        "storage": {
            "filesystem_type": health["filesystem_type"],
            "storage_risk": health["storage_risk"],
            "storage_migration_required": health[
                "storage_migration_required"
            ],
        },
        "index": {
            "status": health["status"],
            "condition": health["condition"],
        },
        "collection": {
            "status": "partial" if unavailable_sections else "complete",
            "unavailable_sections": list(unavailable_sections),
        },
    }


def unavailable_support_context() -> dict[str, object]:
    """Return a fixed partial response after an unexpected handler failure."""

    return collect_support_context(None, None)
