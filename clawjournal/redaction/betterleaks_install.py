"""Managed Betterleaks binary install.

``clawjournal betterleaks install`` downloads a pinned release archive
from betterleaks/betterleaks' official GitHub releases, verifies it
against the sha256 checksums vendored below (taken from the release's
``checksums.txt`` at pin time — verification never trusts what the
network just served), extracts the single binary, and installs it
atomically under ``~/.clawjournal/bin/``.

``betterleaks.resolve_binary()`` prefers this managed copy over PATH.

Betterleaks is MIT-licensed, but the invocation posture matches
TruffleHog's anyway: only ever run as a subprocess with a scrubbed
environment (see ``betterleaks.py``), never linked in-process.
"""

from __future__ import annotations

import gzip
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
import zipfile
from pathlib import Path
from typing import Callable

from .betterleaks import (
    _BARE_VERSION_RE,
    _VERSION_RE,
    managed_binary_path,
)
from .trufflehog import _scrubbed_subprocess_env

PINNED_VERSION = "1.6.1"

# sha256 of the official release archives, copied from checksums.txt on
# the v1.6.1 release page. When bumping PINNED_VERSION, refresh every
# row from the new release's checksums.txt — a stale row fails closed.
# Note upstream's arch naming: x64 (not amd64), and .zip on Windows.
_ARCHIVE_SHA256: dict[str, str] = {
    "darwin_arm64": "9996bfcc93fd2ae6976c7902e3b2177766ec1960c1e30a15398609d5177ef3f8",
    "darwin_x64": "07ddb85c4b2c55da6b671a6af667c89d89c68c52aff8fd73a1118a92d373db8c",
    "linux_arm64": "bab9688ba968264ace67b608fc7a7d8f5e61218cde70029d32cbc894e3808fdf",
    "linux_x64": "fbefc700a0bd4522cc952dd2a8f259cdb80526d7e60114aca19bb2d6fdc80f81",
    "windows_arm64": "fc9bc3d554161e4c94f3510f59d6a790c2ce52f25a5b99520efe1e529efa0912",
    "windows_x64": "3ada08a8b19afab75b111e10f38682a64c0582824c9903ce868b09b0c3c2cf37",
}

_DOWNLOAD_URL_TEMPLATE = (
    "https://github.com/betterleaks/betterleaks/releases/download/"
    "v{version}/{filename}"
)

_MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024  # archives are ~10–30 MB
_MAX_BINARY_BYTES = 500 * 1024 * 1024  # decompressed binary cap
_DOWNLOAD_TIMEOUT_SECONDS = 120
_PROGRESS_TICK_BYTES = 16 * 1024 * 1024

DOWNLOAD_FAILED_HINT = (
    "Check your network and proxy settings (HTTPS_PROXY/HTTP_PROXY are honored) "
    "and retry. If this machine is offline, install Betterleaks manually: "
    "https://github.com/betterleaks/betterleaks#installation"
)
CHECKSUM_MISMATCH_HINT = (
    "The downloaded archive does not match the checksum pinned in this clawjournal "
    "release. Common causes: a TLS-intercepting (corporate/campus) proxy rewriting "
    "the download, a truncated transfer, or an outdated clawjournal — run "
    "`clawjournal selfupdate` and retry. Nothing was installed."
)


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
        arch = "x64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        return None

    key = f"{os_part}_{arch}"
    return key if key in _ARCHIVE_SHA256 else None


def archive_filename(key: str) -> str:
    """Upstream asset name for a platform key — tar.gz everywhere
    except Windows, which ships zips."""
    extension = "zip" if key.startswith("windows") else "tar.gz"
    return f"betterleaks_{PINNED_VERSION}_{key}.{extension}"


def download_url(key: str) -> str:
    return _DOWNLOAD_URL_TEMPLATE.format(
        version=PINNED_VERSION, filename=archive_filename(key)
    )


def _download_archive(
    url: str,
    dest_fd: int,
    progress: Callable[[str], None] | None = None,
) -> str:
    """Stream ``url`` into ``dest_fd``; return the payload's sha256 hex.

    Raises ``urllib.error.URLError``/``OSError`` on network trouble and
    ``ValueError`` if the payload exceeds the size cap. ``progress``
    (if given) receives a start line and a tick every ~16 MB so a slow
    link doesn't look like a hang.
    """
    digest = hashlib.sha256()
    total = 0
    request = urllib.request.Request(url, headers={"User-Agent": "clawjournal"})
    with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
        if progress is not None:
            length = getattr(response, "headers", None)
            length = length.get("Content-Length") if length else None
            size = f" ({int(length) // (1024 * 1024)} MB)" if length and str(length).isdigit() else ""
            progress(f"Downloading Betterleaks v{PINNED_VERSION}{size} from GitHub releases…")
        next_tick = _PROGRESS_TICK_BYTES
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
            if progress is not None and total >= next_tick:
                progress(f"  … {total // (1024 * 1024)} MB")
                next_tick += _PROGRESS_TICK_BYTES
    return digest.hexdigest()


def _stage_member_stream(source, member_size: int, binary_name: str, dest_dir: Path) -> Path:
    """Stream an open archive member into a staged temp file under
    ``dest_dir`` and return its path. The staged file is chmod 0o755 and
    fsynced but NOT published: the caller verifies it executes before
    os.replace()-ing it into place, so a bad download can never displace
    a working managed binary."""
    if member_size > _MAX_BINARY_BYTES:
        raise ValueError("binary member exceeds size cap")
    fd, tmp_name = tempfile.mkstemp(dir=str(dest_dir), prefix=f".{binary_name}.")
    try:
        try:
            with source:
                while True:
                    chunk = source.read(65536)
                    if not chunk:
                        break
                    os.write(fd, chunk)
            os.fsync(fd)
        finally:
            os.close(fd)
        # os.chmod on the path, not os.fchmod on the fd — fchmod doesn't
        # exist on Windows before Python 3.13 and this must run on 3.10+.
        os.chmod(tmp_name, 0o755)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return Path(tmp_name)


def _extract_binary(archive_path: Path, binary_name: str, dest_dir: Path) -> Path:
    """Stage exactly the ``binary_name`` member of the release archive
    (tar.gz on darwin/linux, zip on Windows) into a temp file under
    ``dest_dir`` and return its path. Member names are matched exactly —
    anything resembling a path (``/``, ``..``) is never written, so a
    malicious archive cannot traverse out of the target directory.

    Raises ``ValueError`` if the member is absent or oversized, and
    ``tarfile.TarError``/``zipfile.BadZipFile`` on a corrupt archive.
    """
    if archive_path.suffix == ".zip":
        with zipfile.ZipFile(archive_path) as archive:
            member = None
            for candidate in archive.infolist():
                if candidate.filename == binary_name and not candidate.is_dir():
                    member = candidate
                    break
            if member is None:
                raise ValueError(f"archive has no '{binary_name}' member")
            return _stage_member_stream(
                archive.open(member), member.file_size, binary_name, dest_dir
            )

    with tarfile.open(archive_path, mode="r:gz") as tar:
        member = None
        for candidate in tar:
            if candidate.name == binary_name and candidate.isfile():
                member = candidate
                break
        if member is None:
            raise ValueError(f"archive has no '{binary_name}' member")
        source = tar.extractfile(member)
        if source is None:
            raise ValueError(f"archive member '{binary_name}' is not readable")
        return _stage_member_stream(source, member.size, binary_name, dest_dir)


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
    match = _VERSION_RE.search(blob) or _BARE_VERSION_RE.search(blob)
    return match.group(1) if match else None


def install(
    *,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Install the pinned Betterleaks into ``~/.clawjournal/bin``.

    Returns a result dict with ``status`` plus context fields (and a
    ``hint`` with remediation advice on network/checksum failures);
    never raises. Statuses: ``installed``, ``already-installed``,
    ``unsupported-platform``, ``download-failed``, ``checksum-mismatch``,
    ``archive-invalid``, ``verify-failed``, ``install-failed``.

    ``progress`` (if given) receives human-readable download progress
    lines — the CLI passes ``print`` so a slow link doesn't look hung.
    """
    try:
        return _install(force=force, progress=progress)
    except Exception as exc:
        # Backstop for anything the specific handlers below don't name
        # (e.g. a stray file blocking the bin-dir mkdir). The CLI shows a
        # status, not a traceback; failure direction is always "nothing
        # installed".
        return {
            "status": "install-failed",
            "error": f"{exc.__class__.__name__}: {exc}",
        }


def _install(
    *,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict:
    target = managed_binary_path()
    binary_name = target.name

    # "Already installed" must mean "the share gate will use it AND it
    # is the pinned version" — an unexecutable file, a directory, or an
    # off-pin copy from an older clawjournal falls through to a fresh
    # install (the atomic publish below makes overwriting safe).
    if not force and target.is_file() and os.access(target, os.X_OK):
        current = _verify_installed_binary(target)
        if current == PINNED_VERSION:
            return {
                "status": "already-installed",
                "path": str(target),
                "version": current,
            }

    key = platform_key()
    if key is None:
        return {
            "status": "unsupported-platform",
            "error": (
                f"no pinned Betterleaks v{PINNED_VERSION} archive for "
                f"{sys.platform}/{platform.machine()}; install it manually "
                "(https://github.com/betterleaks/betterleaks#installation)"
            ),
        }

    expected_sha256 = _ARCHIVE_SHA256[key]
    url = download_url(key)
    target.parent.mkdir(parents=True, exist_ok=True)

    archive_suffix = ".zip" if key.startswith("windows") else ".tar.gz"
    archive_fd, archive_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".betterleaks-download.", suffix=archive_suffix
    )
    archive_path = Path(archive_name)
    staged: Path | None = None
    try:
        try:
            actual_sha256 = _download_archive(url, archive_fd, progress=progress)
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            # HTTPException covers e.g. IncompleteRead on a truncated
            # chunked response; URLError is already an OSError subclass
            # but is named for clarity.
            return {
                "status": "download-failed",
                "url": url,
                "error": str(exc),
                "hint": DOWNLOAD_FAILED_HINT,
            }
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
                "hint": CHECKSUM_MISMATCH_HINT,
            }

        # Failure domains are reported distinctly: a bad archive is
        # "archive-invalid" (re-download might help), local file I/O is
        # "install-failed" (the archive already passed its checksum).
        # gzip.BadGzipFile and EOFError are raised by truncated/corrupt
        # streams mid-extract; BadGzipFile subclasses OSError so it must
        # be matched before the OSError clause.
        try:
            staged = _extract_binary(archive_path, binary_name, target.parent)
        except (tarfile.TarError, zipfile.BadZipFile, gzip.BadGzipFile, EOFError, ValueError) as exc:
            return {"status": "archive-invalid", "url": url, "error": str(exc)}
        except OSError as exc:
            return {
                "status": "install-failed",
                "error": f"local file I/O failed while staging the binary: {exc}",
            }

        # Verify BEFORE publish: a binary that doesn't run (wrong arch,
        # truncation the checksum somehow missed) never displaces an
        # existing working managed binary.
        version = _verify_installed_binary(staged)
        if version is None:
            return {
                "status": "verify-failed",
                "path": str(target),
                "error": (
                    "downloaded binary failed to execute `--version`; "
                    "the existing managed binary (if any) was left untouched"
                ),
            }

        try:
            os.replace(staged, target)
        except OSError as exc:
            # E.g. Windows refusing to replace a running .exe, or a
            # directory squatting on the target path.
            return {
                "status": "install-failed",
                "error": (
                    f"could not move the verified binary into place at "
                    f"{target}: {exc}"
                ),
            }
        staged = None  # published — nothing to clean up
    finally:
        for leftover in (archive_path, staged):
            if leftover is None:
                continue
            try:
                leftover.unlink()
            except OSError:
                pass

    return {"status": "installed", "path": str(target), "version": version}
