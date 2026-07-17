from __future__ import annotations

import json
import os
import stat
import subprocess

import pytest

from clawjournal import auto_upload_credentials as store


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store.config_module, "CONFIG_DIR", tmp_path / "install")
    return tmp_path


def _record(**overrides):
    record = {
        "issuer": "https://data.rayward.ai",
        "api_origin": "https://data.rayward.ai",
        "enrollment_id": "enrollment-1",
        "active_token": "active-secret",
        "active_token_expires_at": "2026-08-14T00:00:00+00:00",
        "recovery_token": "recovery-secret",
        "recovery_token_expires_at": "2026-09-14T00:00:00+00:00",
    }
    record.update(overrides)
    return record


def test_credentials_are_private_and_round_trip(isolated_store):
    path = store.write_credentials(_record())

    assert store.load_credentials()["active_token"] == "active-secret"
    if os.name != "nt":
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "active-secret" not in str(path)


def test_config_dir_is_resolved_at_call_time(tmp_path, monkeypatch):
    first = tmp_path / "one"
    second = tmp_path / "two"
    monkeypatch.setattr(store.config_module, "CONFIG_DIR", first)
    store.write_credentials(_record())
    monkeypatch.setattr(store.config_module, "CONFIG_DIR", second)

    assert store.load_credentials(required=False) is None
    store.write_credentials(_record(enrollment_id="enrollment-2"))
    assert json.loads((second / "credentials" / "auto_upload.json").read_text())[
        "enrollment_id"
    ] == "enrollment-2"


def test_remove_active_token_keeps_only_recovery_authority(isolated_store):
    store.write_credentials(_record())

    tombstone = store.remove_active_token()

    assert tombstone["active_token"] is None
    assert tombstone["active_token_expires_at"] is None
    assert tombstone["recovery_token"] == "recovery-secret"
    assert store.load_credentials()["active_token"] is None


def test_invalid_origin_and_unknown_fields_fail_loudly(isolated_store):
    with pytest.raises(store.CredentialStoreError, match="exact HTTPS origin"):
        store.write_credentials(_record(api_origin="http://data.rayward.ai/api"))
    with pytest.raises(store.CredentialStoreError, match="unsupported credential fields"):
        store.write_credentials(_record(manual_upload_token="must-not-be-here"))
    with pytest.raises(store.CredentialStoreError, match="same pinned origin"):
        store.write_credentials(_record(issuer="https://other.example"))


@pytest.mark.parametrize(
    "origin",
    (
        "http://localhost:18000",
        "http://127.0.0.1:18000",
        "http://[::1]:18000",
    ),
)
def test_explicit_local_mode_allows_private_loopback_credentials(
    isolated_store, monkeypatch, origin
):
    monkeypatch.setenv(store.ALLOW_INSECURE_LOOPBACK_ENV, "1")

    path = store.write_credentials(_record(issuer=origin, api_origin=origin))

    loaded = store.load_credentials()
    assert loaded["issuer"] == origin
    assert loaded["api_origin"] == origin
    if os.name != "nt":
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_loopback_credentials_require_explicit_local_mode(isolated_store):
    with pytest.raises(store.CredentialStoreError, match="exact HTTPS origin"):
        store.write_credentials(
            _record(
                issuer="http://127.0.0.1:18000",
                api_origin="http://127.0.0.1:18000",
            )
        )


@pytest.mark.parametrize(
    "origin",
    (
        "http://data.rayward.ai",
        "http://192.168.1.10:18000",
        "http://127.0.0.1.evil.example:18000",
        "http://user@127.0.0.1:18000",
        "http://127.0.0.1:18000/path",
        "http://127.0.0.1:18000?query=yes",
        "http://127.0.0.1:18000#fragment",
    ),
)
def test_explicit_local_mode_rejects_non_exact_or_non_loopback_credentials(
    isolated_store, monkeypatch, origin
):
    monkeypatch.setenv(store.ALLOW_INSECURE_LOOPBACK_ENV, "1")

    with pytest.raises(store.CredentialStoreError, match="exact HTTPS origin"):
        store.write_credentials(_record(issuer=origin, api_origin=origin))


def test_explicit_local_mode_keeps_issuer_origin_exact(
    isolated_store, monkeypatch
):
    monkeypatch.setenv(store.ALLOW_INSECURE_LOOPBACK_ENV, "1")

    with pytest.raises(store.CredentialStoreError, match="same pinned origin"):
        store.write_credentials(
            _record(
                issuer="http://localhost:18000",
                api_origin="http://127.0.0.1:18000",
            )
        )


def test_insecure_existing_file_is_rejected(isolated_store):
    path = store.write_credentials(_record())
    if os.name == "nt":
        pytest.skip("POSIX permission assertion")
    path.chmod(0o644)

    with pytest.raises(store.CredentialStoreError, match="mode 0644"):
        store.load_credentials()


def test_windows_acl_passes_target_via_env_not_positional(tmp_path, monkeypatch):
    # Create the target before patching os.name — patching it to "nt" makes
    # pathlib build a WindowsPath, which cannot instantiate on POSIX. The nt
    # branch of _require_private_mode itself constructs no Path, so calling it
    # directly exercises the ACL invocation safely.
    target = tmp_path / "credentials;$(whoami)'"
    target.mkdir()
    calls = []

    class _Done:
        returncode = 0

    def fake_run(argv, **kwargs):
        calls.append({"argv": list(argv), "env": kwargs.get("env")})
        return _Done()

    monkeypatch.setattr(store.subprocess, "run", fake_run)
    monkeypatch.setattr(store.os, "name", "nt")

    store._require_private_mode(target, 0o700)

    assert len(calls) == 1
    argv = calls[0]["argv"]
    # powershell.exe -NoProfile -NonInteractive -Command <script> : the path
    # must NOT be a trailing positional (that never populates $args).
    assert argv[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
    assert len(argv) == 5
    assert "$env:CLAWJOURNAL_ACL_TARGET" in argv[4]
    assert "$args" not in argv[4]
    assert all(str(target) not in arg for arg in argv)
    assert calls[0]["env"]["CLAWJOURNAL_ACL_TARGET"] == str(target)


@pytest.mark.parametrize("failure", ("nonzero", "launch-error", "timeout"))
def test_windows_acl_failure_is_fail_closed(tmp_path, monkeypatch, failure):
    target = tmp_path / "credentials"
    target.mkdir()

    class _Failed:
        returncode = 1

    def fake_run(_argv, **_kwargs):
        if failure == "launch-error":
            raise OSError("PowerShell unavailable")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("powershell.exe", 15)
        return _Failed()

    monkeypatch.setattr(store.subprocess, "run", fake_run)
    monkeypatch.setattr(store.os, "name", "nt")

    with pytest.raises(
        store.CredentialStoreError,
        match="could not establish a current-user-only Windows ACL",
    ):
        store._require_private_mode(target, 0o700)
