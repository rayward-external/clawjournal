"""Privacy guards: agent packets never leak the answer key, and benchmarks never
enter the share/export path."""

import json
from pathlib import Path

from clawjournal.benchmark import render
from clawjournal.benchmark import schema as bm
from clawjournal.benchmark import store

# Sentinels planted in every grader-only field; none may surface in an agent packet.
GRADER_SENTINELS = [
    "TRAP_SENTINEL", "IDEAL_SENTINEL", "CRITERIA_SENTINEL", "FAIL_SENTINEL",
    "NOTES_SENTINEL", "STAGING_SENTINEL",
]
SESSION_SENTINEL = "SESSIONID_SENTINEL"


def _sentinel_benchmark():
    task = bm.BenchmarkTask(
        id="S1", title="agent-safe title", theme="Th",
        scenario="agent-safe scenario text", seed_inputs="agent-safe seed inputs",
        the_trap="TRAP_SENTINEL", ideal_trajectory=["IDEAL_SENTINEL"],
        pass_criteria=["CRITERIA_SENTINEL"], fail_signals=["FAIL_SENTINEL"],
        grounded_session_ids=[SESSION_SENTINEL], readiness="ready", points=3,
        critique=bm.TaskCritique(notes="NOTES_SENTINEL", staging_notes="STAGING_SENTINEL"))
    return bm.Benchmark(
        window_start="2026-05-24T00:00:00+00:00", window_end="2026-05-31T00:00:00+00:00",
        generated_at="2026-05-31T00:00:00+00:00", themes=[bm.BenchmarkTheme(name="Th")],
        tasks=[task])


class TestPacketLeakage:
    def test_schema_agent_packet_has_no_grader_content(self):
        blob = json.dumps(bm.to_agent_packet(_sentinel_benchmark().tasks[0]))
        for s in GRADER_SENTINELS + [SESSION_SENTINEL]:
            assert s not in blob
        assert "agent-safe scenario text" in blob

    def test_render_agent_packet_md_has_no_grader_content(self):
        md = render.render_agent_packet_markdown(_sentinel_benchmark())
        for s in GRADER_SENTINELS + [SESSION_SENTINEL]:
            assert s not in md
        assert "agent-safe scenario text" in md

    def test_render_agent_packet_json_has_no_grader_content(self):
        blob = json.dumps(render.agent_packet_dict(_sentinel_benchmark()))
        for s in GRADER_SENTINELS + [SESSION_SENTINEL]:
            assert s not in blob

    def test_grader_packet_does_carry_the_answer_key(self):
        # the grader packet is the answer key — it MUST contain the sentinels
        blob = json.dumps(render.grader_packet_dict(_sentinel_benchmark()))
        for s in GRADER_SENTINELS + [SESSION_SENTINEL]:
            assert s in blob


class TestDenormalizedStorage:
    def test_benchmark_tasks_table_stores_no_grader_prose(self, index_conn):
        store.save_benchmark(index_conn, _sentinel_benchmark())
        row = index_conn.execute("SELECT * FROM benchmark_tasks").fetchone()
        blob = json.dumps({k: row[k] for k in row.keys()})
        # grader prose (trap/ideal/criteria/fail/notes/staging) lives only in payload_json
        for s in GRADER_SENTINELS:
            assert s not in blob
        # grounded_session_ids IS stored here by design (filtering), so it is present
        assert SESSION_SENTINEL in blob


class TestShareExportExclusion:
    # Benchmarks are a separate table family and must NEVER be wired into the
    # session share/export bundle. Pin it so a future change can't quietly do so.
    SHARE_EXPORT_MODULES = [
        "clawjournal/export/markdown.py",
        "clawjournal/export/training_data.py",
    ]
    BENCHMARK_TABLES = ("benchmarks", "benchmark_tasks", "benchmark_exports")

    def test_export_modules_never_reference_benchmark_tables(self):
        for mod in self.SHARE_EXPORT_MODULES:
            src = Path(mod).read_text()
            for tbl in self.BENCHMARK_TABLES:
                assert tbl not in src, (
                    f"{mod} references benchmark table {tbl!r} — benchmarks must never "
                    "enter the session share/export path")

    def test_export_modules_do_not_import_the_benchmark_package(self):
        for mod in self.SHARE_EXPORT_MODULES:
            src = Path(mod).read_text()
            assert "benchmark" not in src.lower(), (
                f"{mod} mentions 'benchmark' — the share/export path must stay benchmark-free")
