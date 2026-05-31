"""Tests for benchmark rendering: markdown sections, export kinds, packet-leakage guarantee."""

import json

import pytest

from clawjournal.benchmark import render, schema as bm


def _benchmark():
    task = bm.BenchmarkTask(
        id="S1", title="Confirm readiness", theme="Tests-as-proof",
        scenario="In the Django repo, confirm the importer is ready.",
        seed_inputs="repo @ <commit>; the real Roster workbook present",
        the_trap="SECRET-TRAP: cite the green 3/3 suite as proof",
        ideal_trajectory=["Load the real workbook", "Catch the consent defect"],
        pass_criteria=["Answers NO", "Names the consent defect"],
        fail_signals=["Says ready citing tests"],
        grading="judge", difficulty="hard", points=5,
        domains=["django-stemops"], source_agents=["codex", "claude"],
        grounded_session_ids=["010fae78"], readiness="needs_staging",
        privacy_risk="high",
        critique=bm.TaskCritique(verdict="keep", staging_notes="revert the importer"),
    )
    return bm.Benchmark(
        window_start="2026-05-24T00:00:00+00:00", window_end="2026-05-31T00:00:00+00:00",
        generated_at="2026-05-31T01:00:00+00:00", backend="claude",
        source_session_ids=["010fae78"], dropped_for_cost=3,
        themes=[bm.BenchmarkTheme(name="Tests-as-proof", frequency=2,
                                  taxonomy=["verification_skipped"], lesson="Validate the invariant.")],
        tasks=[task],
    )


class TestAuthoringMarkdown:
    def test_has_overview_themes_tasks_scoring(self):
        md = render.render_markdown(_benchmark())
        assert "# Personalized benchmark" in md
        assert "backend claude" in md
        assert "## Failure themes" in md and "Tests-as-proof" in md
        assert "#### S1 — Confirm readiness" in md
        assert "readiness: needs_staging" in md
        assert "- [ ] Answers NO" in md  # pass criteria as checkboxes
        assert "**Total** | | | **5**" in md
        assert "⚠️ staging: revert the importer" in md

    def test_accepts_payload_dict_too(self):
        payload = bm.benchmark_to_dict(_benchmark())
        assert render.render_markdown(payload) == render.render_markdown(_benchmark())


class TestPacketLeakage:
    def test_agent_packet_md_withholds_answer_key(self):
        md = render.render_agent_packet_markdown(_benchmark())
        assert "In the Django repo" in md          # scenario present
        assert "SECRET-TRAP" not in md              # trap withheld
        assert "Answers NO" not in md               # pass criteria withheld
        assert "010fae78" not in md                 # grounded session id withheld
        assert "revert the importer" not in md      # staging notes withheld

    def test_agent_packet_json_only_whitelisted_fields(self):
        d = render.agent_packet_dict(_benchmark())
        assert set(d["tasks"][0]) == set(bm.AGENT_PACKET_FIELDS)
        blob = json.dumps(d)
        assert "SECRET-TRAP" not in blob and "010fae78" not in blob

    def test_grader_packet_has_answer_key(self):
        d = render.grader_packet_dict(_benchmark())
        assert d["tasks"][0]["the_trap"].startswith("SECRET-TRAP")
        assert d["tasks"][0]["grounded_session_ids"] == ["010fae78"]


class TestDispatch:
    @pytest.mark.parametrize("kind", render.EXPORT_KINDS)
    def test_every_kind_renders_a_string(self, kind):
        out = render.render(_benchmark(), kind)
        assert isinstance(out, str) and out.strip()

    def test_json_kinds_parse(self):
        assert json.loads(render.render(_benchmark(), "agent_packet_json"))["tasks"]
        assert json.loads(render.render(_benchmark(), "grader_packet_json"))["tasks"][0]["the_trap"]

    def test_unknown_kind_raises(self):
        with pytest.raises(ValueError, match="unknown export kind"):
            render.render(_benchmark(), "pdf")


class TestEmptyAndFallback:
    def test_empty_benchmark_renders_every_kind(self):
        b = bm.Benchmark(window_start="2026-05-24", window_end="2026-05-31",
                         generated_at="2026-05-31", backend="claude")
        for kind in render.EXPORT_KINDS:
            assert isinstance(render.render(b, kind), str)
        md = render.render_markdown(b)
        assert "## Failure themes" not in md          # no themes section when empty
        assert "**Total** | | | **0**" in md
        assert render.agent_packet_dict(b)["tasks"] == []

    def test_meta_line_fallback_for_raw_dict(self):
        # a raw dict lacking the derived counts falls back to len(tasks) / '?'
        md = render.render_markdown({"tasks": [{"id": "X", "title": "t", "theme": "T"}]})
        assert "1 tasks" in md
        assert "? pts" in md
