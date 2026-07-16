from __future__ import annotations

import json
import os
import stat

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
