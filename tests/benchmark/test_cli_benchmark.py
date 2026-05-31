"""Tests for the `clawjournal benchmark` CLI verb (generate/list/show/export)."""

import json
from types import SimpleNamespace

import pytest

from clawjournal import cli_benchmark
from clawjournal.benchmark import schema as bm
from clawjournal.benchmark import store


def _args(**over):
    base = dict(window=7, cap=15, backend="auto", list=False, show=None,
                export=None, kind="authoring_md", output=None, json=False)
    base.update(over)
    return SimpleNamespace(**base)


def _canned(window_end="2026-05-31T00:00:00+00:00"):
    task = bm.BenchmarkTask(
        id="S1", title="Confirm readiness", theme="Tests-as-proof",
        scenario="In the Django repo, confirm the importer is ready.", seed_inputs="repo @ commit",
        the_trap="SECRET-TRAP: cite the green suite", pass_criteria=["answers NO"],
        grading="judge", difficulty="hard", points=5, domains=["clawjournal-dev"],
        source_agents=["claude"], grounded_session_ids=["sess-a"], readiness="ready")
    return bm.Benchmark(
        window_start="2026-05-24T00:00:00+00:00", window_end=window_end, generated_at=window_end,
        backend="claude", source_session_ids=["sess-a"],
        themes=[bm.BenchmarkTheme(name="Tests-as-proof", frequency=1)], tasks=[task])


class TestGenerate:
    def test_stores_and_prints_summary(self, index_conn, monkeypatch, capsys):
        monkeypatch.setattr("clawjournal.benchmark.generate.generate_benchmark",
                            lambda conn, **kw: _canned())
        cli_benchmark.run_benchmark(_args())
        out = json.loads(capsys.readouterr().out)
        assert out["n_tasks"] == 1 and out["total_points"] == 5 and out["ready"] == 1
        assert store.get_benchmark(index_conn, out["benchmark_id"])["status"] == "ready"

    def test_passes_window_cap_backend(self, index_conn, monkeypatch):
        seen = {}
        def fake(conn, **kw):
            seen.update(kw)
            return _canned()
        monkeypatch.setattr("clawjournal.benchmark.generate.generate_benchmark", fake)
        cli_benchmark.run_benchmark(_args(window=14, cap=8, backend="codex"))
        assert seen["window_days"] == 14 and seen["cap"] == 8 and seen["backend"] == "codex"

    def test_failure_exits_nonzero(self, index_conn, monkeypatch):
        def boom(conn, **kw):
            raise ValueError("no failure-signal sessions in the selected window")
        monkeypatch.setattr("clawjournal.benchmark.generate.generate_benchmark", boom)
        with pytest.raises(SystemExit):
            cli_benchmark.run_benchmark(_args())


class TestList:
    def test_human(self, index_conn, capsys):
        store.save_benchmark(index_conn, _canned())
        cli_benchmark.run_benchmark(_args(list=True))
        out = capsys.readouterr().out
        assert "2026-W22" in out and "tasks" in out

    def test_json(self, index_conn, capsys):
        store.save_benchmark(index_conn, _canned())
        cli_benchmark.run_benchmark(_args(list=True, json=True))
        assert len(json.loads(capsys.readouterr().out)["benchmarks"]) == 1

    def test_empty(self, index_conn, capsys):
        cli_benchmark.run_benchmark(_args(list=True))
        assert "No benchmarks yet" in capsys.readouterr().out


class TestShow:
    def test_latest(self, index_conn, capsys):
        store.save_benchmark(index_conn, _canned())
        cli_benchmark.run_benchmark(_args(show="latest"))
        out = capsys.readouterr().out
        assert "# Personalized benchmark" in out and "#### S1" in out

    def test_not_found(self, index_conn):
        with pytest.raises(SystemExit):
            cli_benchmark.run_benchmark(_args(show="nope"))


class TestExport:
    def test_agent_packet_withholds_trap_and_records_receipt(self, index_conn, tmp_path, capsys):
        store.save_benchmark(index_conn, _canned())
        out = tmp_path / "ap.md"
        cli_benchmark.run_benchmark(_args(export="latest", kind="agent_packet_md", output=str(out)))
        content = out.read_text()
        assert "In the Django repo" in content and "SECRET-TRAP" not in content
        res = json.loads(capsys.readouterr().out)
        assert res["kind"] == "agent_packet_md" and res["pii_scan_hits"] == 0
        assert index_conn.execute("SELECT COUNT(*) FROM benchmark_exports").fetchone()[0] == 1

    def test_authoring_default_kind(self, index_conn, tmp_path, capsys):
        store.save_benchmark(index_conn, _canned())
        out = tmp_path / "auth.md"
        cli_benchmark.run_benchmark(_args(export="latest", output=str(out)))
        assert "## Tasks" in out.read_text()

    def test_unknown_kind_exits(self, index_conn):
        store.save_benchmark(index_conn, _canned())
        with pytest.raises(SystemExit):
            cli_benchmark.run_benchmark(_args(export="latest", kind="pdf"))

    def test_not_found_exits(self, index_conn):
        with pytest.raises(SystemExit):
            cli_benchmark.run_benchmark(_args(export="nope"))


class TestArgparseWiring:
    def test_main_routes_benchmark_list(self, index_conn, monkeypatch, capsys):
        from clawjournal import cli
        monkeypatch.setattr("sys.argv", ["clawjournal", "benchmark", "--list"])
        cli.main()
        assert "No benchmarks yet" in capsys.readouterr().out
