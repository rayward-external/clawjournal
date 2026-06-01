"""Integration tests for the benchmark daemon API (GET endpoints, export, async generate)."""

import json
import time
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread

import pytest

from clawjournal.benchmark import schema as bm
from clawjournal.benchmark import store
from clawjournal.benchmark.select import WeekSlice
from clawjournal.workbench import daemon as dmod
from clawjournal.workbench.daemon import (
    WorkbenchHandler,
    _benchmark_is_stale,
    _run_benchmark_generation,
)
from clawjournal.workbench.index import open_index


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
    monkeypatch.setattr("clawjournal.workbench.daemon.CONFIG_DIR", tmp_path)
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


def _canned(window_end="2026-05-31T00:00:00+00:00"):
    task = bm.BenchmarkTask(
        id="S1", title="Confirm readiness", theme="Th",
        scenario="In the repo, confirm X is ready.", the_trap="SECRET-TRAP",
        pass_criteria=["c"], grading="judge", difficulty="hard", points=5,
        grounded_session_ids=["sess-a"], readiness="ready", source_agents=["claude"])
    return bm.Benchmark(
        window_start="2026-05-24T00:00:00+00:00", window_end=window_end, generated_at=window_end,
        backend="claude", source_session_ids=["sess-a"],
        themes=[bm.BenchmarkTheme(name="Th", frequency=2)], tasks=[task])


def _save(benchmark=None):
    conn = open_index()
    try:
        return store.save_benchmark(conn, benchmark or _canned())
    finally:
        conn.close()


class TestGetEndpoints:
    def test_list_get_status(self, api):
        bid = _save()
        s, data = _get(api, "/api/benchmarks")
        assert s == 200 and [b["benchmark_id"] for b in data["benchmarks"]] == [bid]
        s, data = _get(api, f"/api/benchmarks/{bid}")
        assert s == 200 and data["n_tasks"] == 1 and len(data["tasks"]) == 1
        s, data = _get(api, f"/api/benchmarks/{bid}/status")
        assert s == 200 and data["status"] == "ready" and data["stage"] is None

    def test_latest_stale_flags(self, api):
        _save(_canned(window_end="2000-01-01T00:00:00+00:00"))
        _, data = _get(api, "/api/benchmarks/latest")
        assert data["stale"] is True
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        _save(_canned(window_end=recent))
        _, data = _get(api, "/api/benchmarks/latest")
        assert data["benchmark"]["window_end"] == recent and data["stale"] is False

    def test_trend(self, api):
        _save()
        s, data = _get(api, "/api/benchmarks/trend")
        assert s == 200 and data["themes"]["Th"] == [2]

    def test_not_found(self, api):
        assert _get(api, "/api/benchmarks/nope")[0] == 404
        assert _get(api, "/api/benchmarks/nope/status")[0] == 404


class TestExport:
    def test_agent_packet_withholds_trap_and_records_receipt(self, api):
        bid = _save()
        s, data = _post(api, f"/api/benchmarks/{bid}/export", {"kind": "agent_packet_md"})
        assert s == 200
        assert "In the repo" in data["content"] and "SECRET-TRAP" not in data["content"]
        assert data["pii_scan_hits"] == 0 and data["path"].endswith("agent_packet_md.md")
        assert Path(data["path"]).read_text() == data["content"]
        conn = open_index()
        try:
            n = conn.execute("SELECT COUNT(*) FROM benchmark_exports WHERE benchmark_id=?",
                             (bid,)).fetchone()[0]
        finally:
            conn.close()
        assert n == 1

    def test_unknown_kind_400(self, api):
        bid = _save()
        assert _post(api, f"/api/benchmarks/{bid}/export", {"kind": "pdf"})[0] == 400

    def test_export_not_found_404(self, api):
        assert _post(api, "/api/benchmarks/nope/export", {"kind": "authoring_md"})[0] == 404

    def test_export_non_ready_409(self, api):
        conn = open_index()
        bid = store.insert_generating(conn, window_start="2026-05-24T00:00:00+00:00",
                                      window_end="2026-05-31T00:00:00+00:00")
        conn.close()
        assert _post(api, f"/api/benchmarks/{bid}/export", {"kind": "authoring_md"})[0] == 409


class TestGenerate:
    def _seed_failure(self):
        conn = open_index()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO sessions (session_id, project, source, indexed_at, start_time, "
            "review_status, ai_failure_value_score, ai_failure_modes, ai_learning_summary) "
            "VALUES (?,?,?,?,?, 'new', 5, '[\"x\"]', 'lesson')",
            ("s1", "p", "codex", now, now))
        conn.commit()
        conn.close()

    def test_async_generate_then_poll_ready(self, api, monkeypatch):
        self._seed_failure()

        def fake(conn, *, week_slice=None, **kw):
            b = _canned()
            b.window_start, b.window_end = week_slice.window_start, week_slice.window_end
            b.generated_at = week_slice.window_end
            return b
        monkeypatch.setattr("clawjournal.benchmark.generate.generate_benchmark", fake)

        s, data = _post(api, "/api/benchmarks/generate", {})
        assert s == 202 and data["status"] == "generating"
        bid = data["benchmark_id"]
        for _ in range(60):
            st = _get(api, f"/api/benchmarks/{bid}/status")[1]
            if st["status"] != "generating":
                break
            time.sleep(0.05)
        assert st["status"] == "ready"
        assert _get(api, f"/api/benchmarks/{bid}")[1]["n_tasks"] == 1

    def test_no_candidates_400(self, api):
        assert _post(api, "/api/benchmarks/generate", {})[0] == 400

    def test_busy_returns_409(self, api):
        dmod._BENCHMARK_GEN_LOCK.acquire()
        try:
            assert _post(api, "/api/benchmarks/generate", {})[0] == 409
        finally:
            dmod._BENCHMARK_GEN_LOCK.release()


class TestWorkerUnit:
    def test_is_stale(self):
        assert _benchmark_is_stale(None) is True
        assert _benchmark_is_stale({"generated_at": "2000-01-01T00:00:00+00:00"}) is True
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        assert _benchmark_is_stale({"generated_at": recent}) is False

    def _slice(self):
        return WeekSlice(window_start="2026-05-24T00:00:00+00:00",
                         window_end="2026-05-31T00:00:00+00:00",
                         candidates=[], total_candidates=0, dropped_for_cost=0)

    def test_worker_finalizes(self, api, monkeypatch):
        sl = self._slice()
        conn = open_index()
        bid = store.insert_generating(conn, window_start=sl.window_start, window_end=sl.window_end)
        conn.close()

        def fake(conn, *, week_slice=None, **kw):
            b = _canned()
            b.window_start, b.window_end = week_slice.window_start, week_slice.window_end
            b.generated_at = week_slice.window_end
            return b
        monkeypatch.setattr("clawjournal.benchmark.generate.generate_benchmark", fake)

        dmod._BENCHMARK_GEN_LOCK.acquire()  # the worker releases it (mirrors prod)
        _run_benchmark_generation(bid, sl)
        conn = open_index()
        got = store.get_benchmark(conn, bid)
        conn.close()
        assert got["status"] == "ready" and got["n_tasks"] == 1
        assert not dmod._BENCHMARK_GEN_LOCK.locked()

    def test_worker_marks_failed(self, api, monkeypatch):
        sl = self._slice()
        conn = open_index()
        bid = store.insert_generating(conn, window_start=sl.window_start, window_end=sl.window_end)
        conn.close()
        monkeypatch.setattr("clawjournal.benchmark.generate.generate_benchmark",
                            lambda conn, **kw: (_ for _ in ()).throw(RuntimeError("kaboom")))
        dmod._BENCHMARK_GEN_LOCK.acquire()
        _run_benchmark_generation(bid, sl)
        conn = open_index()
        got = store.get_benchmark(conn, bid)
        conn.close()
        assert got["status"] == "failed" and "kaboom" in got["error"]
        assert not dmod._BENCHMARK_GEN_LOCK.locked()


class TestFeatures:
    def test_default_enabled_when_key_missing(self, api, monkeypatch):
        # Older configs lack the key → the gate defaults ON.
        monkeypatch.setattr("clawjournal.config.load_config", lambda: {})
        status, body = _get(api, "/api/features")
        assert status == 200
        assert body == {"benchmark_tab_enabled": True}

    def test_respects_disabled_flag(self, api, monkeypatch):
        monkeypatch.setattr("clawjournal.config.load_config",
                            lambda: {"benchmark_tab_enabled": False})
        status, body = _get(api, "/api/features")
        assert status == 200
        assert body == {"benchmark_tab_enabled": False}
