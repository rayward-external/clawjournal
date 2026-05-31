"""Tests for benchmark persistence: save/get, history, denormalized filter, lifecycle, cascade."""

import sqlite3

import pytest

from clawjournal.benchmark import schema as bm
from clawjournal.benchmark import store


def _task(task_id, **over):
    base = dict(
        id=task_id, title=f"Task {task_id}", theme="Tests-as-proof",
        scenario="scenario text", seed_inputs="seed", the_trap="trap",
        pass_criteria=["passes"], grading="judge", difficulty="hard", points=5,
        domains=["clawjournal-dev"], source_agents=["claude"],
        grounded_session_ids=["sess-a"], readiness="ready",
    )
    base.update(over)
    return bm.BenchmarkTask(**base)


def _benchmark(window_end="2026-05-31T00:00:00+00:00", tasks=None, **over):
    base = dict(
        window_start="2026-05-24T00:00:00+00:00",
        window_end=window_end,
        generated_at=window_end,
        backend="claude",
        source_session_ids=["sess-a", "sess-b"],
        dropped_for_cost=2,
        themes=[bm.BenchmarkTheme(name="Tests-as-proof", frequency=2)],
        tasks=tasks if tasks is not None else [
            _task("S1"),
            _task("S2", readiness="needs_staging", theme="Auth-wall", points=3, source_agents=["codex"]),
        ],
    )
    base.update(over)
    return bm.Benchmark(**base)


class TestSaveGet:
    def test_save_assigns_isoweek_id_and_round_trips(self, index_conn):
        bid = store.save_benchmark(index_conn, _benchmark())
        assert bid.startswith("2026-W")
        got = store.get_benchmark(index_conn, bid)
        assert got is not None
        assert got["status"] == "ready"
        assert got["n_tasks"] == 2
        assert got["total_points"] == 8
        assert got["ready_count"] == 1
        assert got["needs_staging_count"] == 1
        assert len(got["tasks"]) == 2

    def test_latest_returns_newest_ready(self, index_conn):
        store.save_benchmark(index_conn, _benchmark(
            window_end="2026-05-17T00:00:00+00:00"))
        store.save_benchmark(index_conn, _benchmark(
            window_end="2026-05-31T00:00:00+00:00"))
        latest = store.get_latest_benchmark(index_conn)
        assert latest["window_end"] == "2026-05-31T00:00:00+00:00"

    def test_list_summaries_exclude_payload(self, index_conn):
        store.save_benchmark(index_conn, _benchmark())
        rows = store.list_benchmarks(index_conn)
        assert len(rows) == 1
        assert "payload_json" not in rows[0]
        assert "tasks" not in rows[0]
        assert rows[0]["n_tasks"] == 2


class TestHistory:
    def test_keep_every_generation_appends(self, index_conn):
        a = store.save_benchmark(index_conn, _benchmark())
        b = store.save_benchmark(index_conn, _benchmark())  # same window/week
        c = store.save_benchmark(index_conn, _benchmark())
        assert a != b != c
        assert {a, b, c} == {"2026-W22", "2026-W22-2", "2026-W22-3"}
        assert len(store.list_benchmarks(index_conn)) == 3


class TestDenormalizedTasks:
    def test_filter_by_readiness_and_theme_and_agent(self, index_conn):
        bid = store.save_benchmark(index_conn, _benchmark())
        assert {t["task_id"] for t in store.list_tasks(index_conn, bid)} == {
            f"{bid}:S1", f"{bid}:S2"}
        ready = store.list_tasks(index_conn, bid, readiness="ready")
        assert [t["title"] for t in ready] == ["Task S1"]
        auth = store.list_tasks(index_conn, bid, theme="Auth-wall")
        assert [t["task_id"] for t in auth] == [f"{bid}:S2"]
        codex = store.list_tasks(index_conn, bid, source_agent="codex")
        assert [t["task_id"] for t in codex] == [f"{bid}:S2"]

    def test_task_rows_decode_json_fields(self, index_conn):
        bid = store.save_benchmark(index_conn, _benchmark())
        t = store.list_tasks(index_conn, bid, readiness="ready")[0]
        assert t["domains"] == ["clawjournal-dev"]
        assert t["source_agents"] == ["claude"]
        assert t["grounded_session_ids"] == ["sess-a"]


class TestGeneratingLifecycle:
    def test_insert_generating_then_finalize(self, index_conn):
        bid = store.insert_generating(
            index_conn, window_start="2026-05-24T00:00:00+00:00",
            window_end="2026-05-31T00:00:00+00:00", backend="claude")
        row = store.get_benchmark(index_conn, bid)
        assert row["status"] == "generating"
        store.update_status(index_conn, bid, stage="deep-reading 3/14")
        assert store.get_benchmark(index_conn, bid)["stage"] == "deep-reading 3/14"
        store.finalize_benchmark(index_conn, bid, _benchmark())
        done = store.get_benchmark(index_conn, bid)
        assert done["status"] == "ready"
        assert done["stage"] is None
        assert done["n_tasks"] == 2
        assert len(store.list_tasks(index_conn, bid)) == 2

    def test_failed_status(self, index_conn):
        bid = store.insert_generating(
            index_conn, window_start="2026-05-24T00:00:00+00:00",
            window_end="2026-05-31T00:00:00+00:00")
        store.update_status(index_conn, bid, status="failed", error="backend not found")
        row = store.get_benchmark(index_conn, bid)
        assert row["status"] == "failed"
        assert row["error"] == "backend not found"
        assert store.get_latest_benchmark(index_conn) is None  # only ready counts


class TestExportsAndCascade:
    def test_record_export(self, index_conn):
        bid = store.save_benchmark(index_conn, _benchmark())
        eid = store.record_export(index_conn, bid, kind="agent_packet_md", path="/tmp/x.md",
                                  redaction_summary={"emails": 0})
        row = index_conn.execute(
            "SELECT * FROM benchmark_exports WHERE export_id = ?", (eid,)).fetchone()
        assert row["kind"] == "agent_packet_md"
        assert row["benchmark_id"] == bid

    def test_delete_cascades_tasks_and_exports(self, index_conn):
        bid = store.save_benchmark(index_conn, _benchmark())
        store.record_export(index_conn, bid, kind="authoring_md")
        store.delete_benchmark(index_conn, bid)
        assert store.get_benchmark(index_conn, bid) is None
        assert index_conn.execute(
            "SELECT COUNT(*) FROM benchmark_tasks WHERE benchmark_id = ?", (bid,)).fetchone()[0] == 0
        assert index_conn.execute(
            "SELECT COUNT(*) FROM benchmark_exports WHERE benchmark_id = ?", (bid,)).fetchone()[0] == 0

    def test_foreign_key_enforced_on_orphan_task(self, index_conn):
        with pytest.raises(sqlite3.IntegrityError):
            index_conn.execute(
                "INSERT INTO benchmark_tasks (task_id, benchmark_id) VALUES ('x', 'no-such-run')")
            index_conn.commit()


class TestTieBreak:
    def test_latest_and_trend_deterministic_on_tied_generated_at(self, index_conn):
        # three same-week regenerations share generated_at; rowid breaks the tie
        ids = [
            store.save_benchmark(index_conn, _benchmark(
                themes=[bm.BenchmarkTheme(name="Tests-as-proof", frequency=freq)]))
            for freq in (1, 2, 3)
        ]
        assert store.get_latest_benchmark(index_conn)["benchmark_id"] == ids[-1]
        assert store.get_theme_trend(index_conn)["themes"]["Tests-as-proof"] == [1, 2, 3]


class TestSaveRobustness:
    def test_resave_same_id_preserves_exports(self, index_conn):
        b = _benchmark()
        bid = store.save_benchmark(index_conn, b)  # populates b.benchmark_id
        store.record_export(index_conn, bid, kind="authoring_md")
        store.save_benchmark(index_conn, b)  # re-save same id must not cascade-wipe exports
        n = index_conn.execute(
            "SELECT COUNT(*) FROM benchmark_exports WHERE benchmark_id = ?", (bid,)).fetchone()[0]
        assert n == 1

    def test_finalize_unknown_id_raises_cleanly(self, index_conn):
        with pytest.raises(ValueError, match="no placeholder"):
            store.finalize_benchmark(index_conn, "2099-W99", _benchmark())
        assert index_conn.execute("SELECT COUNT(*) FROM benchmarks").fetchone()[0] == 0
        assert index_conn.execute("SELECT COUNT(*) FROM benchmark_tasks").fetchone()[0] == 0

    def test_finalize_window_mismatch_raises(self, index_conn):
        bid = store.insert_generating(
            index_conn, window_start="2026-05-24T00:00:00+00:00",
            window_end="2026-05-31T00:00:00+00:00")
        with pytest.raises(ValueError, match="window mismatch"):
            store.finalize_benchmark(index_conn, bid, _benchmark(window_end="2026-06-07T00:00:00+00:00"))


class TestPayloadEdgeCases:
    def test_malformed_payload_json_read_safely(self, index_conn):
        index_conn.execute(
            "INSERT INTO benchmarks (benchmark_id, window_start, window_end, generated_at, "
            "status, payload_json) VALUES ('bad','2026-05-24','2026-05-31','2026-05-31','ready','{not json')")
        index_conn.commit()
        got = store.get_benchmark(index_conn, "bad")
        assert got["status"] == "ready"
        assert "tasks" not in got
        trend = store.get_theme_trend(index_conn)
        assert trend["themes"] == {}
        assert [r["benchmark_id"] for r in trend["runs"]] == ["bad"]

    def test_zero_task_benchmark(self, index_conn):
        bid = store.save_benchmark(index_conn, _benchmark(tasks=[]))
        got = store.get_benchmark(index_conn, bid)
        assert got["n_tasks"] == 0 and got["total_points"] == 0
        assert got["ready_count"] == 0 and got["needs_staging_count"] == 0
        assert store.list_tasks(index_conn, bid) == []
        assert store.get_latest_benchmark(index_conn)["benchmark_id"] == bid

    def test_unicode_round_trips(self, index_conn):
        u = "测试 émoji 🚀 á"
        bid = store.save_benchmark(index_conn, _benchmark(tasks=[_task("U", title=u, theme=u, scenario=u)]))
        assert store.get_benchmark(index_conn, bid)["tasks"][0]["title"] == u
        assert store.list_tasks(index_conn, bid)[0]["title"] == u

    def test_large_scenario_round_trips(self, index_conn):
        big = "x" * 200_000
        bid = store.save_benchmark(index_conn, _benchmark(tasks=[_task("L", scenario=big)]))
        assert store.get_benchmark(index_conn, bid)["tasks"][0]["scenario"] == big


class TestTrend:
    def test_theme_trend_matrix_oldest_to_newest(self, index_conn):
        store.save_benchmark(index_conn, _benchmark(
            window_end="2026-05-17T00:00:00+00:00",
            themes=[bm.BenchmarkTheme(name="Tests-as-proof", frequency=1)]))
        store.save_benchmark(index_conn, _benchmark(
            window_end="2026-05-31T00:00:00+00:00",
            themes=[bm.BenchmarkTheme(name="Tests-as-proof", frequency=4),
                    bm.BenchmarkTheme(name="Auth-wall", frequency=2)]))
        trend = store.get_theme_trend(index_conn)
        assert [r["window_end"] for r in trend["runs"]] == [
            "2026-05-17T00:00:00+00:00", "2026-05-31T00:00:00+00:00"]
        assert trend["themes"]["Tests-as-proof"] == [1, 4]
        assert trend["themes"]["Auth-wall"] == [0, 2]  # absent in week 1
