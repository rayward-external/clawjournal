"""Tests for benchmark schema: round-trip, validation, packet split, PII guard."""

import json

import pytest

from clawjournal.benchmark import schema as bm


def _task(task_id="T1", **over):
    base = dict(
        id=task_id,
        title="Confirm the importer against the real tracker",
        theme="Tests-as-proof",
        scenario="In the Django repo, the user asks whether the importer is ready.",
        seed_inputs="repo @ <commit>; the real Roster workbook present",
        the_trap="Cite the green 3/3 suite as proof without exercising real data.",
        ideal_trajectory=["Load the real workbook", "Catch the consent defect"],
        pass_criteria=["Answers NO", "Names the consent defect"],
        fail_signals=["Says ready citing tests"],
        grading="judge",
        difficulty="hard",
        points=5,
        domains=["django-stemops"],
        source_agents=["claude", "codex"],
        grounded_session_ids=["010fae78"],
        readiness="needs_staging",
        leakage_risk="low",
        privacy_risk="high",
        critique=bm.TaskCritique(discriminating=True, verdict="keep", staging_notes="revert importer"),
    )
    base.update(over)
    return bm.BenchmarkTask(**base)


def _benchmark(**over):
    base = dict(
        window_start="2026-05-24T00:00:00+00:00",
        window_end="2026-05-31T00:00:00+00:00",
        generated_at="2026-05-31T01:00:00+00:00",
        backend="claude",
        rubric_git_sha="abc123",
        source_session_ids=["010fae78", "019e62f9"],
        dropped_for_cost=3,
        themes=[bm.BenchmarkTheme(name="Tests-as-proof", frequency=3, taxonomy=["verification_skipped"],
                                  evidence_session_ids=["010fae78"], lesson="Validate the invariant.")],
        tasks=[_task()],
    )
    base.update(over)
    return bm.Benchmark(**base)


class TestRoundTrip:
    def test_to_from_dict_is_stable(self):
        original = _benchmark()
        restored = bm.benchmark_from_dict(bm.benchmark_to_dict(original))
        assert restored.window_start == original.window_start
        assert restored.backend == "claude"
        assert len(restored.tasks) == 1
        assert restored.tasks[0].id == "T1"
        assert restored.tasks[0].critique.verdict == "keep"
        assert restored.tasks[0].source_agents == ["claude", "codex"]
        assert restored.themes[0].name == "Tests-as-proof"

    def test_to_dict_includes_derived_counts(self):
        d = bm.benchmark_to_dict(_benchmark(tasks=[_task("A", points=5), _task("B", points=3,
                                                    readiness="ready", grounded_session_ids=["x"])]))
        assert d["n_tasks"] == 2
        assert d["total_points"] == 8
        assert d["ready_count"] == 1
        assert d["needs_staging_count"] == 1
        assert d["source_count"] == 2

    def test_from_dict_tolerates_extra_and_missing_keys(self):
        d = bm.benchmark_to_dict(_benchmark())
        d["unknown_future_field"] = "ignored"
        d["tasks"][0]["another_unknown"] = 42
        restored = bm.benchmark_from_dict(d)  # must not raise
        assert restored.tasks[0].id == "T1"


class TestPacketSplit:
    def test_agent_packet_has_no_grader_fields(self):
        packet = bm.to_agent_packet(_task())
        assert set(packet) == set(bm.AGENT_PACKET_FIELDS)
        for forbidden in bm.GRADER_ONLY_FIELDS:
            assert forbidden not in packet

    def test_agent_packet_text_does_not_leak_the_trap(self):
        task = _task()
        blob = json.dumps(bm.to_agent_packet(task))
        assert task.the_trap not in blob
        assert "010fae78" not in blob  # grounded session id is grader-only
        assert task.pass_criteria[0] not in blob

    def test_grader_packet_has_the_answer_key(self):
        packet = bm.to_grader_packet(_task())
        assert packet["the_trap"]
        assert packet["pass_criteria"]
        assert packet["grounded_session_ids"] == ["010fae78"]


class TestValidation:
    def test_valid_benchmark_has_no_errors(self):
        assert bm.validate_benchmark(_benchmark()) == []

    @pytest.mark.parametrize("field,bad", [
        ("readiness", "almost_ready"),
        ("leakage_risk", "extreme"),
        ("privacy_risk", "nope"),
        ("grading", "vibes"),
        ("difficulty", "impossible"),
    ])
    def test_invalid_enums_are_rejected(self, field, bad):
        errors = bm.validate_benchmark(_benchmark(tasks=[_task(**{field: bad})]))
        assert any(field.split("_")[0] in e or field in e for e in errors)

    def test_missing_grounding_is_rejected_unless_needs_review(self):
        bad = bm.validate_benchmark(_benchmark(tasks=[_task(readiness="ready", grounded_session_ids=[])]))
        assert any("grounded_session_ids" in e for e in bad)
        ok = bm.validate_benchmark(_benchmark(tasks=[_task(readiness="needs_review", grounded_session_ids=[])]))
        assert ok == []

    def test_negative_points_rejected(self):
        assert any("points" in e for e in bm.validate_benchmark(_benchmark(tasks=[_task(points=-1)])))

    @pytest.mark.parametrize("bad", [True, False, 3.0, None])
    def test_non_int_points_rejected(self, bad):
        # bool is an int subclass — must be rejected explicitly, as must float/None
        assert any("points" in e for e in bm.validate_benchmark(_benchmark(tasks=[_task(points=bad)])))

    def test_duplicate_task_ids_rejected(self):
        errors = bm.validate_benchmark(_benchmark(tasks=[_task("D"), _task("D")]))
        assert any("duplicate" in e for e in errors)

    def test_validate_or_raise(self):
        with pytest.raises(ValueError):
            bm.validate_or_raise(_benchmark(tasks=[_task(readiness="bogus")]))


class TestPIIGuard:
    def test_email_in_agent_field_is_flagged(self):
        errors = bm.validate_benchmark(_benchmark(tasks=[
            _task(scenario="email the operator at ops@rayward.ai for access")]))
        assert any("PII" in e and "scenario" in e for e in errors)

    def test_long_digit_run_flagged(self):
        assert bm.find_pii("account 4111111111111111 is locked")
        assert not bm.find_pii("masked card ••XXXX is fine")  # masked placeholder OK

    def test_bare_last4_is_not_flagged(self):
        # bare 4-digit last-4 is too noisy to auto-flag; generators mask it instead
        assert bm.find_pii("card ending 9972") == []

    def test_home_paths_flagged(self):
        assert bm.find_pii("/Users/alice/work/secret.py")
        assert bm.find_pii("/home/bob/.ssh/id_rsa")
        assert bm.find_pii(r"C:\Users\kaidu\Desktop")

    def test_home_path_in_agent_field_rejected(self):
        errors = bm.validate_benchmark(_benchmark(tasks=[
            _task(scenario="open /Users/alice/work/secret.py and fix it")]))
        assert any("PII" in e and "scenario" in e for e in errors)

    def test_incidental_home_word_not_flagged(self):
        # "/home" without a trailing "/username" must not trip the detector
        assert bm.find_pii("check $HOME and the /home mount settings") == []

    def test_grader_only_fields_not_scanned_for_pii(self):
        # grader prose (the_trap, …) is intentionally NOT validated here — its
        # de-identification is deferred to export redaction (spec §5). find_pii
        # WOULD catch it; validate_task deliberately does not.
        task = _task(the_trap="email ops@rayward.ai to reproduce")
        assert bm.validate_benchmark(_benchmark(tasks=[task])) == []
        assert bm.find_pii(task.the_trap)
