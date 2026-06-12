"""Managed TruffleHog binary install.

``clawjournal trufflehog install`` downloads a pinned release archive
from trufflesecurity/trufflehog's official GitHub releases, verifies
it against the sha256 checksums vendored below (taken from the
release's ``checksums.txt`` at pin time — verification never trusts
what the network just served), extracts the single binary, and
installs it atomically under ``~/.clawjournal/bin/``.

``trufflehog.resolve_binary()`` prefers this managed copy over PATH.

AGPL posture unchanged: the binary is fetched from upstream's own
release artifacts at the user's explicit request and is only ever
invoked as a subprocess (see ``trufflehog.py``); nothing is linked
in-process and nothing is redistributed by us.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from .trufflehog import _VERSION_RE, _scrubbed_subprocess_env, managed_binary_path

PINNED_VERSION = "3.95.5"

# sha256 of the official release archives, copied from
# trufflehog_3.95.5_checksums.txt on the v3.95.5 release page.
# When bumping PINNED_VERSION, refresh every row from the new
# release's checksums.txt — a stale row fails closed.
_ARCHIVE_SHA256: dict[str, str] = {
    "darwin_amd64": "8091a92ad3ef6c46244f5b6b9683c72296381d77f63e8a979e913d8d58df595d",
    "darwin_arm64": "0a08b46f63d48ccb894689b68b5e7b91ac5efa09b9684a3457d388456887c213",
    "linux_amd64": "8d151a19465973bec226be5992a2a11b053f4ab92c77861f642089892ae9aa58",
    "linux_arm64": "bb876c4e5a84fa4fdbda4fc24143ed2d12eac32cfd3f7e41c79cbd7d33607b4a",
    "windows_amd64": "4421ac2786b2a356d62d2f4c59798bba5069c7a9f4dc7af9558061568b642c4d",
}

_DOWNLOAD_URL_TEMPLATE = (
    "https://github.com/trufflesecurity/trufflehog/releases/download/"
    "v{version}/trufflehog_{version}_{platform}.tar.gz"
)

_MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024  # archives are ~30–60 MB
_MAX_BINARY_BYTES = 500 * 1024 * 1024  # decompressed binary cap
_DOWNLOAD_TIMEOUT_SECONDS = 120


def platform_key() -> str | None:
    """``{os}_{arch}`` key into ``_ARCHIVE_SHA256``, or None if this
    platform has no pinned archive."""
    if sys.platform == "darwin":
        os_part = "darwin"
    elif sys.platform.startswith("linux"):
        os_part = "linux"
    elif os.name == "nt":
        os_part = "windows"
    else:
        return None

    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        return None

    key = f"{os_part}_{arch}"
    return key if key in _ARCHIVE_SHA256 else None


def download_url(key: str) -> str:
    return _DOWNLOAD_URL_TEMPLATE.format(version=PINNED_VERSION, platform=key)


def _download_archive(url: str, dest_fd: int) -> str:
    """Stream ``url`` into ``dest_fd``; return the payload's sha256 hex.

    Raises ``urllib.error.URLError``/``OSError`` on network trouble and
    ``ValueError`` if the payload exceeds the size cap.
    """
    digest = hashlib.sha256()
    total = 0
    request = urllib.request.Request(url, headers={"User-Agent": "clawjournal"})
    with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_DOWNLOAD_BYTES:
                raise ValueError(
                    f"download exceeded {_MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB cap"
                )
            digest.update(chunk)
            os.write(dest_fd, chunk)
    return digest.hexdigest()


def _extract_binary(archive_path: Path, binary_name: str, dest_path: Path) -> None:
    """Pull exactly the ``binary_name`` member out of the release tarball
    into ``dest_path`` (parent must exist). Member names are matched
    exactly — anything resembling a path (``/``, ``..``) is never written,
    so a malicious archive cannot traverse out of the target directory.

    Raises ``ValueError`` if the member is absent or oversized, and
    ``tarfile.TarError`` on a corrupt archive.
    """
    with tarfile.open(archive_path, mode="r:gz") as tar:
        member = None
        for candidate in tar:
            if candidate.name == binary_name and candidate.isfile():
                member = candidate
                break
        if member is None:
            raise ValueError(f"archive has no '{binary_name}' member")
        if member.size > _MAX_BINARY_BYTES:
            raise ValueError("binary member exceeds size cap")
        source = tar.extractfile(member)
        if source is None:
            raise ValueError(f"archive member '{binary_name}' is not readable")
        fd, tmp_name = tempfile.mkstemp(dir=str(dest_path.parent), prefix=f".{binary_name}.")
        try:
            try:
                with source:
                    while True:
                        chunk = source.read(65536)
                        if not chunk:
                            break
                        os.write(fd, chunk)
            finally:
                os.close(fd)
            # os.chmod on the path, not os.fchmod on the fd — fchmod doesn't
            # exist on Windows before Python 3.13 and this must run on 3.10+.
            os.chmod(tmp_name, 0o755)
            os.replace(tmp_name, dest_path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def _verify_installed_binary(path: Path) -> str | None:
    """Run ``<path> --version`` as a post-install sanity check (catches a
    wrong-arch or truncated binary before the share gate ever trusts it).
    Returns the parsed version string, or None if the binary doesn't run.
    """
    try:
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
            stdin=subprocess.DEVNULL,
            env=_scrubbed_subprocess_env(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    blob = (result.stdout or "") + "\n" + (result.stderr or "")
    match = _VERSION_RE.search(blob)
    return match.group(1) if match else None


def install(*, force: bool = False) -> dict:
    """Install the pinned TruffleHog into ``~/.clawjournal/bin``.

    Returns a result dict with ``status`` plus context fields; never
    raises. Statuses: ``installed``, ``already-installed``,
    ``unsupported-platform``, ``download-failed``, ``checksum-mismatch``,
    ``archive-invalid``, ``verify-failed``, ``install-failed``.
    """
    try:
        return _install(force=force)
    except Exception as exc:
        # Backstop for anything the specific handlers below don't name
        # (e.g. a stray file blocking the bin-dir mkdir). The CLI shows a
        # status, not a traceback; failure direction is always "nothing
        # installed".
        return {
            "status": "install-failed",
            "error": f"{exc.__class__.__name__}: {exc}",
        }


def _install(*, force: bool = False) -> dict:
    target = managed_binary_path()
    binary_name = target.name

    if target.exists() and not force:
        return {
            "status": "already-installed",
            "path": str(target),
            "version": _verify_installed_binary(target),
        }

    key = platform_key()
    if key is None:
        return {
            "status": "unsupported-platform",
            "error": (
                f"no pinned TruffleHog v{PINNED_VERSION} archive for "
                f"{sys.platform}/{platform.machine()}; install it manually "
                "(https://github.com/trufflesecurity/trufflehog#floppy_disk-installation)"
            ),
        }

    expected_sha256 = _ARCHIVE_SHA256[key]
    url = download_url(key)
    target.parent.mkdir(parents=True, exist_ok=True)

    archive_fd, archive_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".trufflehog-download.", suffix=".tar.gz"
    )
    archive_path = Path(archive_name)
    try:
        try:
            actual_sha256 = _download_archive(url, archive_fd)
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            # HTTPException covers e.g. IncompleteRead on a truncated
            # chunked response; URLError is already an OSError subclass
            # but is named for clarity.
            return {"status": "download-failed", "url": url, "error": str(exc)}
        finally:
            os.close(archive_fd)

        if actual_sha256 != expected_sha256:
            # Fail closed: nothing is extracted or installed.
            return {
                "status": "checksum-mismatch",
                "url": url,
                "error": (
                    f"sha256 {actual_sha256} does not match the pinned "
                    f"checksum {expected_sha256} for {key}"
                ),
            }

        try:
            _extract_binary(archive_path, binary_name, target)
        except (tarfile.TarError, ValueError, OSError, EOFError) as exc:
            # EOFError: gzip stream truncated mid-member.
            return {"status": "archive-invalid", "url": url, "error": str(exc)}
    finally:
        try:
            archive_path.unlink()
        except OSError:
            pass

    version = _verify_installed_binary(target)
    if version is None:
        try:
            target.unlink()
        except OSError:
            pass
        return {
            "status": "verify-failed",
            "path": str(target),
            "error": "installed binary failed to execute `--version`; removed it",
        }

    return {"status": "installed", "path": str(target), "version": version}
