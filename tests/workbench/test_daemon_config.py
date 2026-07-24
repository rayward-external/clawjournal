"""Tests for the daemon config endpoints and the scoring-warmup decline gate."""

import json
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread

import pytest

from clawjournal.config import load_config, save_config
from clawjournal.workbench import daemon as dmod
from clawjournal.workbench.daemon import WorkbenchHandler
from clawjournal.workbench.index import open_index, upsert_sessions


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
    monkeypatch.setattr("clawjournal.workbench.daemon.CONFIG_DIR", tmp_path)
    # Isolate config.json to tmp so config writes never touch the real ~/.clawjournal.
    cfg_dir = tmp_path / ".clawjournal"
    monkeypatch.setattr("clawjournal.config.CONFIG_DIR", cfg_dir)
    monkeypatch.setattr("clawjournal.config.CONFIG_FILE", cfg_dir / "config.json")
    open_index().close()  # bootstrap DB + api_token
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), WorkbenchHandler)
    port = srv.server_address[1]
    Thread(target=srv.serve_forever, daemon=True).start()
    yield port
    srv.shutdown()


def _auth():
    from clawjournal.paths import API_TOKEN_FILENAME
    from clawjournal.workbench.index import INDEX_DB
    token = (Path(str(INDEX_DB)).parent / API_TOKEN_FILENAME).read_text().strip()
    return {"Authorization": f"Bearer {token}"}


def _get(port, path):
    c = HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers=_auth())
    r = c.getresponse()
    return r.status, json.loads(r.read().decode())


def _post(port, path, data=None):
    c = HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", path, body=json.dumps(data or {}).encode(),
              headers={"Content-Type": "application/json", **_auth()})
    r = c.getresponse()
    return r.status, json.loads(r.read().decode())


class TestGetConfig:
    def test_returns_whitelisted_subset_and_options(self, api):
        status, body = _get(api, "/api/config")
        assert status == 200
        for key in (
            "source", "projects_confirmed", "ai_pii_review_enabled",
            "scorer_backend", "benchmark_tab_enabled", "scoring_warmup_declined",
            "source_choices", "scorer_backend_choices", "scorer_backend_detected",
        ):
            assert key in body
        assert isinstance(body["source_choices"], list)
        assert "all" in body["source_choices"]
        # The deprecated 'both' alias is not offered.
        assert "both" not in body["source_choices"]

    def test_never_exposes_secrets(self, api):
        save_config({
            "verified_email_token": "SECRET",
            "recurring_enrollment_grant": "ONE-TIME-GRANT",
            "publish_attestation": "X",
        })
        _, body = _get(api, "/api/config")
        for secret in (
            "verified_email_token",
            "recurring_enrollment_grant",
            "publish_attestation",
            "pending_verification_email",
        ):
            assert secret not in body


class TestUpdateConfig:
    def test_source_persisted(self, api):
        status, body = _post(api, "/api/config", {"source": "all"})
        assert status == 200
        assert body["source"] == "all"
        assert load_config()["source"] == "all"

    def test_scorer_backend_set_and_cleared(self, api):
        _post(api, "/api/config", {"scorer_backend": "claude"})
        cfg = load_config()
        assert cfg["scorer_backend"] == "claude"
        assert cfg.get("scorer_backend_confirmed_at")
        _post(api, "/api/config", {"scorer_backend": "none"})
        cfg = load_config()
        assert "scorer_backend" not in cfg
        assert "scorer_backend_confirmed_at" not in cfg

    def test_booleans_flip(self, api):
        _post(api, "/api/config", {"ai_pii_review_enabled": True, "benchmark_tab_enabled": False})
        cfg = load_config()
        assert cfg["ai_pii_review_enabled"] is True
        assert cfg["benchmark_tab_enabled"] is False

    def test_confirm_projects(self, api):
        _post(api, "/api/config", {"confirm_projects": True})
        assert load_config()["projects_confirmed"] is True

    def test_scoring_warmup_declined_toggle(self, api):
        # The Settings "Background AI scoring" toggle round-trips through here:
        # disabling sets the decline; re-enabling pops it.
        _post(api, "/api/config", {"scoring_warmup_declined": True})
        assert load_config()["scoring_warmup_declined"] is True
        assert _get(api, "/api/config")[1]["scoring_warmup_declined"] is True
        _post(api, "/api/config", {"scoring_warmup_declined": False})
        assert load_config().get("scoring_warmup_declined") in (None, False)
        assert _get(api, "/api/config")[1]["scoring_warmup_declined"] is False

    def test_invalid_source_400(self, api):
        assert _post(api, "/api/config", {"source": "bogus"})[0] == 400

    def test_invalid_backend_400(self, api):
        assert _post(api, "/api/config", {"scorer_backend": "bogus"})[0] == 400

    def test_empty_body_400(self, api):
        assert _post(api, "/api/config", {})[0] == 400

    def test_preserves_redact_strings(self, api):
        # The append/merge invariant: a config write must not clobber lists the
        # whitelist never touches.
        save_config({"redact_strings": ["KEEPME"]})
        _post(api, "/api/config", {"source": "all"})
        cfg = load_config()
        assert cfg["redact_strings"] == ["KEEPME"]
        assert cfg["source"] == "all"


class TestScoringWarmupDecline:
    def test_features_includes_flag(self, api):
        _, body = _get(api, "/api/features")
        assert body["scoring_warmup_declined"] is False

    def test_decline_persists(self, api):
        status, body = _post(api, "/api/scoring/warmup", {"decline": True})
        assert status == 200
        assert body["status"] == "declined"
        assert load_config()["scoring_warmup_declined"] is True
        # And it now shows up in /api/features.
        assert _get(api, "/api/features")[1]["scoring_warmup_declined"] is True

    def test_confirm_clears_decline(self, api):
        _post(api, "/api/scoring/warmup", {"decline": True})
        assert load_config().get("scoring_warmup_declined") is True
        # Confirm must clear the decline even though no scanner is running here.
        _post(api, "/api/scoring/warmup", {"confirm_backend": True, "backend": "claude"})
        assert load_config().get("scoring_warmup_declined") in (None, False)


class TestScoringBatchCancel:
    def test_toggle_off_mid_batch_stops_scoring(self, tmp_path, monkeypatch):
        # Isolate index + config to tmp.
        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        cfg_dir = tmp_path / ".clawjournal"
        monkeypatch.setattr("clawjournal.config.CONFIG_DIR", cfg_dir)
        monkeypatch.setattr("clawjournal.config.CONFIG_FILE", cfg_dir / "config.json")
        now = datetime.now(timezone.utc)
        conn = open_index()
        upsert_sessions(conn, [
            {
                "session_id": f"s{i}",
                "project": "test-project",
                "source": "claude",
                "model": "m",
                "start_time": (now - timedelta(minutes=20 + i)).isoformat(),
                "end_time": (now - timedelta(minutes=10 + i)).isoformat(),
                "messages": [{"role": "user", "content": "Fix it"}],
                "stats": {"user_messages": 1, "assistant_messages": 0, "tool_uses": 0},
            }
            for i in range(3)
        ])
        conn.close()

        fake_sessions = [{"session_id": f"s{i}"} for i in range(3)]
        monkeypatch.setattr(dmod, "query_unscored_sessions", lambda *a, **k: fake_sessions)
        monkeypatch.setattr(dmod, "_persist_scoring_result", lambda *a, **k: True)
        monkeypatch.setattr(dmod, "_maybe_create_trace_note", lambda *a, **k: None)

        calls = []

        def fake_score(conn, sid, backend="auto"):
            calls.append(sid)
            # Simulate the user turning OFF background scoring after the 1st trace.
            cfg = load_config()
            cfg["scoring_warmup_declined"] = True
            save_config(cfg)
            return {"ok": True}

        monkeypatch.setattr("clawjournal.scoring.scoring.score_session", fake_score)

        scored = dmod.Scanner().score_unscored_once()
        # Only the first trace was scored; the loop broke before egressing more.
        assert calls == ["s0"]
        assert scored == 1


class TestTriggerWarmupGate:
    def test_declined_short_circuits_without_scoring(self, monkeypatch):
        monkeypatch.setattr(dmod, "load_config", lambda: {"scoring_warmup_declined": True})
        calls = {"n": 0}

        class FakeScanner:
            def trigger_auto_score(self, **kw):
                calls["n"] += 1
                return {"status": "started"}

        result = dmod.trigger_scoring_warmup(FakeScanner())
        assert result["status"] == "declined"
        assert calls["n"] == 0
