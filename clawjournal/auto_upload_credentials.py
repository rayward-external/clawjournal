"""Private persistence for recurring-upload credentials.

Recurring credentials are deliberately isolated from ``config.json``.  This
module resolves the installation directory at call time (important for tests
and embedders), writes atomically and durably, and fails loudly whenever the
current user-only permission boundary cannot be established.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from . import config as config_module

CREDENTIALS_DIRNAME = "credentials"
CREDENTIALS_FILENAME = "auto_upload.json"
ALLOW_INSECURE_LOOPBACK_ENV = "CLAWJOURNAL_ALLOW_INSECURE_LOOPBACK_RECURRING"

_ALLOWED_FIELDS = frozenset(
    {
        "issuer",
        "api_origin",
        "enrollment_id",
        "active_token",
        "active_token_expires_at",
        "recovery_token",
        "recovery_token_expires_at",
    }
)
_REQUIRED_FIELDS = frozenset(
    {
        "issuer",
        "api_origin",
        "enrollment_id",
        "recovery_token",
        "recovery_token_expires_at",
    }
)


class CredentialStoreError(RuntimeError):
    """The private credential store is missing, corrupt, or not private."""


def credential_path() -> Path:
    """Return the current installation's credential path."""

    return Path(config_module.CONFIG_DIR) / CREDENTIALS_DIRNAME / CREDENTIALS_FILENAME


def _validate_origin(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CredentialStoreError(f"{field} must be a non-empty HTTPS origin")
    candidate = value.strip()
    parsed = urlsplit(candidate)
    hostname = (parsed.hostname or "").lower()
    scheme = parsed.scheme.lower()
    local_http = (
        os.environ.get(ALLOW_INSECURE_LOOPBACK_ENV) == "1"
        and scheme == "http"
        and hostname in {"localhost", "127.0.0.1", "::1"}
    )
    if (
        (scheme != "https" and not local_http)
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise CredentialStoreError(
            f"{field} must be an exact HTTPS origin, or explicitly enabled loopback HTTP origin"
        )
    port = f":{parsed.port}" if parsed.port is not None else ""
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{scheme}://{rendered_host}{port}"


def _validate_record(record: Mapping[str, Any]) -> dict[str, str | None]:
    unknown = set(record) - _ALLOWED_FIELDS
    if unknown:
        raise CredentialStoreError(
            f"unsupported credential fields: {', '.join(sorted(unknown))}"
        )
    missing = [field for field in sorted(_REQUIRED_FIELDS) if not record.get(field)]
    if missing:
        raise CredentialStoreError(
            f"missing credential fields: {', '.join(missing)}"
        )
    active_token = record.get("active_token")
    active_expiry = record.get("active_token_expires_at")
    if bool(active_token) != bool(active_expiry):
        raise CredentialStoreError(
            "active_token and active_token_expires_at must be present together"
        )
    normalized: dict[str, str | None] = {
        "issuer": _validate_origin(record.get("issuer"), field="issuer"),
        "api_origin": _validate_origin(record.get("api_origin"), field="api_origin"),
    }
    if normalized["issuer"] != normalized["api_origin"]:
        raise CredentialStoreError("issuer and api_origin must be the same pinned origin")
    for field in _ALLOWED_FIELDS - {"issuer", "api_origin"}:
        value = record.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise CredentialStoreError(f"{field} must be a non-empty string")
        normalized[field] = value.strip() if isinstance(value, str) else None
    return normalized


def _require_private_mode(path: Path, expected: int) -> None:
    if os.name == "nt":
        # POSIX mode bits do not establish a Windows credential boundary.
        # Replace the DACL with one FullControl ACE for the current user and
        # disable inheritance.  PowerShell/.NET ships with supported Windows
        # Python versions; any failure blocks enrollment rather than falling
        # back to a best-effort chmod.
        # The target path is passed via an environment variable, NOT a
        # positional argument: `powershell.exe -Command <script> <path>` does
        # not populate $args (only -File does), so $args[0] would be $null and
        # every ACL call would fail. Reading $env avoids that and cannot be
        # command-injected the way string interpolation could.
        script = r"""
$ErrorActionPreference = 'Stop'
$target = $env:CLAWJOURNAL_ACL_TARGET
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$isDirectory = Test-Path -LiteralPath $target -PathType Container
if ($isDirectory) {
  $grant = "*$($identity.Value):(OI)(CI)F"
} else {
  $grant = "*$($identity.Value):F"
}
& icacls.exe $target /inheritance:r | Out-Null
if ($LASTEXITCODE -ne 0) {
  throw "icacls failed with exit code $LASTEXITCODE"
}
$existingAcl = Get-Acl -LiteralPath $target
$otherIdentities = @(
  $existingAcl.GetAccessRules(
    $true,
    $true,
    [System.Security.Principal.SecurityIdentifier]
  ) |
    Where-Object { $_.IdentityReference -ne $identity } |
    ForEach-Object { $_.IdentityReference.Value } |
    Sort-Object -Unique
)
foreach ($otherIdentity in $otherIdentities) {
  & icacls.exe $target /remove "*$otherIdentity" | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "icacls could not remove an existing credential ACL entry"
  }
}
& icacls.exe $target /grant:r $grant | Out-Null
if ($LASTEXITCODE -ne 0) {
  throw "icacls failed with exit code $LASTEXITCODE"
}
$acl = Get-Acl -LiteralPath $target
$rules = @($acl.GetAccessRules(
  $true,
  $true,
  [System.Security.Principal.SecurityIdentifier]
))
if (-not $acl.AreAccessRulesProtected -or $rules.Count -eq 0) {
  throw 'credential ACL is not protected or has no access rules'
}
$unexpected = @($rules | Where-Object {
  $_.IdentityReference -ne $identity -or
  $_.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow
})
$fullControl = @($rules | Where-Object {
  ($_.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::FullControl) -eq
    [System.Security.AccessControl.FileSystemRights]::FullControl
})
if ($unexpected.Count -ne 0 -or $fullControl.Count -eq 0) {
  throw 'credential ACL does not grant full control exclusively to the current user'
}
"""
        try:
            child_env = os.environ.copy()
            child_env["CLAWJOURNAL_ACL_TARGET"] = str(path)
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=child_env,
                check=False,
                timeout=15,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CredentialStoreError(
                f"could not establish a current-user-only Windows ACL for {path}"
            ) from exc
        if completed.returncode != 0:
            detail = (
                getattr(completed, "stderr", "")
                or getattr(completed, "stdout", "")
                or ""
            ).strip()
            suffix = f": {detail}" if detail else ""
            raise CredentialStoreError(
                f"could not establish a current-user-only Windows ACL for {path}{suffix}"
            )
        return
    actual = stat.S_IMODE(path.stat().st_mode)
    if actual != expected:
        raise CredentialStoreError(
            f"private credential path {path} has mode {actual:04o}; expected {expected:04o}"
        )


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
        _require_private_mode(path, 0o700)
    except OSError as exc:
        raise CredentialStoreError(
            f"could not establish private credential directory {path}"
        ) from exc


def write_credentials(record: Mapping[str, Any]) -> Path:
    """Atomically persist a validated credential record and fsync it."""

    normalized = _validate_record(record)
    path = credential_path()
    _ensure_private_directory(path.parent)
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":")) + "\n"
    fd = -1
    temp_path: str | None = None
    try:
        fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=".auto-upload-", suffix=".tmp")
        if os.name == "nt":
            os.chmod(temp_path, stat.S_IREAD | stat.S_IWRITE)
        else:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            fd = -1
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        assert temp_path is not None
        _require_private_mode(Path(temp_path), 0o600)
        os.replace(temp_path, path)
        temp_path = None
        _require_private_mode(path, 0o600)
        if hasattr(os, "O_DIRECTORY"):
            dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except (OSError, CredentialStoreError) as exc:
        raise CredentialStoreError("could not durably persist recurring credentials") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    return path


def load_credentials(*, required: bool = True) -> dict[str, str | None] | None:
    """Load credentials only after verifying directory and file privacy."""

    path = credential_path()
    if not path.exists():
        if required:
            raise CredentialStoreError("recurring credentials are not available")
        return None
    try:
        _require_private_mode(path.parent, 0o700)
        _require_private_mode(path, 0o600)
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialStoreError("could not read recurring credentials") from exc
    if not isinstance(raw, dict):
        raise CredentialStoreError("recurring credential file must contain an object")
    return _validate_record(raw)


def remove_active_token() -> dict[str, str | None] | None:
    """Remove upload authority while retaining the recovery tombstone."""

    record = load_credentials(required=False)
    if record is None:
        return None
    record["active_token"] = None
    record["active_token_expires_at"] = None
    write_credentials(record)
    return record


def delete_credentials() -> None:
    """Delete a fully reconciled credential tombstone durably."""

    path = credential_path()
    try:
        path.unlink(missing_ok=True)
        if path.parent.exists() and hasattr(os, "O_DIRECTORY"):
            dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except OSError as exc:
        raise CredentialStoreError("could not delete recurring credentials") from exc
