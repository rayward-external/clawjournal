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
    try:
        parsed = urlsplit(candidate)
        parsed_port = parsed.port
    except ValueError as exc:
        raise CredentialStoreError(
            f"{field} must be an exact HTTPS origin, or explicitly enabled loopback HTTP origin"
        ) from exc
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
    port = f":{parsed_port}" if parsed_port is not None else ""
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


def _set_windows_private_acl(path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    token_query = 0x0008
    token_user_class = 1
    error_insufficient_buffer = 122
    acl_revision = 2
    acl_size_information = 2
    access_allowed_ace_type = 0
    object_inherit_ace = 0x01
    container_inherit_ace = 0x02
    inherited_ace = 0x10
    file_all_access = 0x001F01FF
    se_file_object = 1
    dacl_security_information = 0x00000004
    protected_dacl_security_information = 0x80000000
    se_dacl_protected = 0x1000

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

    class TokenUser(ctypes.Structure):
        _fields_ = [("user", SidAndAttributes)]

    class Acl(ctypes.Structure):
        _fields_ = [
            ("revision", ctypes.c_ubyte),
            ("sbz1", ctypes.c_ubyte),
            ("size", wintypes.WORD),
            ("ace_count", wintypes.WORD),
            ("sbz2", wintypes.WORD),
        ]

    class AceHeader(ctypes.Structure):
        _fields_ = [
            ("ace_type", ctypes.c_ubyte),
            ("ace_flags", ctypes.c_ubyte),
            ("ace_size", wintypes.WORD),
        ]

    class AccessAllowedAce(ctypes.Structure):
        _fields_ = [
            ("header", AceHeader),
            ("mask", wintypes.DWORD),
            ("sid_start", wintypes.DWORD),
        ]

    class AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("ace_count", wintypes.DWORD),
            ("acl_bytes_in_use", wintypes.DWORD),
            ("acl_bytes_free", wintypes.DWORD),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi32.GetLengthSid.restype = wintypes.DWORD
    advapi32.InitializeAcl.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
    advapi32.InitializeAcl.restype = wintypes.BOOL
    advapi32.AddAccessAllowedAceEx.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    advapi32.AddAccessAllowedAceEx.restype = wintypes.BOOL
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi32.EqualSid.restype = wintypes.BOOL

    def raise_last_error(api_name: str) -> None:
        error = ctypes.get_last_error()
        raise OSError(error, f"{api_name} failed: {ctypes.FormatError(error).strip()}")

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
    ):
        raise_last_error("OpenProcessToken")
    try:
        required = wintypes.DWORD()
        ctypes.set_last_error(0)
        advapi32.GetTokenInformation(
            token, token_user_class, None, 0, ctypes.byref(required)
        )
        if ctypes.get_last_error() != error_insufficient_buffer or required.value == 0:
            raise_last_error("GetTokenInformation")
        token_buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            token_user_class,
            token_buffer,
            required.value,
            ctypes.byref(required),
        ):
            raise_last_error("GetTokenInformation")
        current_sid = ctypes.cast(
            token_buffer, ctypes.POINTER(TokenUser)
        ).contents.user.sid
        sid_length = advapi32.GetLengthSid(current_sid)
        if sid_length == 0:
            raise_last_error("GetLengthSid")

        acl_size = ctypes.sizeof(Acl) + AccessAllowedAce.sid_start.offset + sid_length
        acl_buffer = ctypes.create_string_buffer(acl_size)
        acl_pointer = ctypes.cast(acl_buffer, ctypes.c_void_p)
        if not advapi32.InitializeAcl(acl_pointer, acl_size, acl_revision):
            raise_last_error("InitializeAcl")
        inheritance = object_inherit_ace | container_inherit_ace if path.is_dir() else 0
        if not advapi32.AddAccessAllowedAceEx(
            acl_pointer,
            acl_revision,
            inheritance,
            file_all_access,
            current_sid,
        ):
            raise_last_error("AddAccessAllowedAceEx")

        security_information = (
            dacl_security_information | protected_dacl_security_information
        )
        status = advapi32.SetNamedSecurityInfoW(
            str(path),
            se_file_object,
            security_information,
            None,
            None,
            acl_pointer,
            None,
        )
        if status != 0:
            raise OSError(
                status,
                f"SetNamedSecurityInfoW failed: {ctypes.FormatError(status).strip()}",
            )

        stored_acl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        status = advapi32.GetNamedSecurityInfoW(
            str(path),
            se_file_object,
            dacl_security_information,
            None,
            None,
            ctypes.byref(stored_acl),
            None,
            ctypes.byref(descriptor),
        )
        if status != 0:
            raise OSError(
                status,
                f"GetNamedSecurityInfoW failed: {ctypes.FormatError(status).strip()}",
            )
        try:
            control = wintypes.WORD()
            revision = wintypes.DWORD()
            if not advapi32.GetSecurityDescriptorControl(
                descriptor, ctypes.byref(control), ctypes.byref(revision)
            ):
                raise_last_error("GetSecurityDescriptorControl")
            acl_info = AclSizeInformation()
            if not advapi32.GetAclInformation(
                stored_acl,
                ctypes.byref(acl_info),
                ctypes.sizeof(acl_info),
                acl_size_information,
            ):
                raise_last_error("GetAclInformation")
            ace_pointer = ctypes.c_void_p()
            if acl_info.ace_count != 1 or not advapi32.GetAce(
                stored_acl, 0, ctypes.byref(ace_pointer)
            ):
                raise CredentialStoreError(
                    "credential ACL does not contain exactly one access rule"
                )
            ace = ctypes.cast(
                ace_pointer, ctypes.POINTER(AccessAllowedAce)
            ).contents
            ace_sid = ctypes.c_void_p(
                ace_pointer.value + AccessAllowedAce.sid_start.offset
            )
            if (
                not control.value & se_dacl_protected
                or ace.header.ace_type != access_allowed_ace_type
                or ace.header.ace_flags & inherited_ace
                or ace.header.ace_flags & (object_inherit_ace | container_inherit_ace)
                != inheritance
                or ace.mask != file_all_access
                or not advapi32.EqualSid(ace_sid, current_sid)
            ):
                raise CredentialStoreError(
                    "credential ACL does not grant full control exclusively to the current user"
                )
        finally:
            if descriptor:
                kernel32.LocalFree(descriptor)
    finally:
        kernel32.CloseHandle(token)


def _require_private_mode(path: Path, expected: int) -> None:
    if os.name == "nt":
        try:
            _set_windows_private_acl(path)
        except (OSError, CredentialStoreError) as exc:
            raise CredentialStoreError(
                f"could not establish a current-user-only Windows ACL for {path}: {exc}"
            ) from exc
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
