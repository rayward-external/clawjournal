"""Distill: one call through the seam, scrub-before-LLM, parse + cap + support."""

from clawjournal.skill.distill import build_prompt, distill_skills
from clawjournal.skill.select import SkillCandidate, SkillCorpus
from clawjournal.redaction.anonymizer import Anonymizer


def _corpus():
    return SkillCorpus(
        window_start="2026-05-24", window_end="2026-05-31",
        failures=[SkillCandidate("s1", "proj", "codex", "avoid",
                                 failure_modes=["verification_skipped"], learning_summary="declared done early")],
        successes=[SkillCandidate("s2", "proj", "codex", "do", learning_summary="repro first")],
        mode_recurrence={"verification_skipped": 4},
        total_failures=1, total_successes=1,
    )


class FakeCaller:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

    def __call__(self, *, system_prompt, task_prompt):
        self.calls.append((system_prompt, task_prompt))
        return self.payload


def test_single_call_and_parse():
    fake = FakeCaller({"rules": [
        {"kind": "avoid", "trigger": "before done", "guidance": "run tests first",
         "why": "premature", "taxonomy": "verification_skipped"},
        {"kind": "do", "trigger": "unfamiliar API", "guidance": "read source first", "why": "worked"},
    ]})
    rules = distill_skills(_corpus(), caller=fake)
    assert len(fake.calls) == 1                       # Mode A == one distill call
    assert [r.kind for r in rules] == ["avoid", "do"]
    assert rules[0].support == 4                      # backfilled from recurrence


def test_evidence_ids_are_limited_to_selected_sessions():
    fake = FakeCaller({"rules": [
        {"kind": "avoid", "trigger": "before done", "guidance": "run tests first",
         "why": "premature", "taxonomy": "verification_skipped",
         "evidence_session_ids": ["s1", "https://evil.test/prompt"]},
    ]})
    rules = distill_skills(_corpus(), caller=fake)
    assert rules[0].evidence_session_ids == ["case-01"]


def test_empty_corpus_no_call():
    fake = FakeCaller({"rules": []})
    empty = SkillCorpus(window_start="a", window_end="b")
    assert distill_skills(empty, caller=fake) == []
    assert fake.calls == []


def test_prompt_is_scrubbed_before_llm():
    # a candidate carrying a secret in its substrate must not reach the prompt raw
    corpus = SkillCorpus(window_start="a", window_end="b",
                         failures=[SkillCandidate("raw-/Users/kai/project", "proj", "codex", "avoid",
                                   learning_summary="leaked AKIAIOSFODNN7EXAMPLE in a config")])
    prompt = build_prompt(corpus, Anonymizer())
    assert "AKIAIOSFODNN7EXAMPLE" not in prompt
    assert "raw-/Users/kai/project" not in prompt
    assert "case-01" in prompt


def test_distill_defaults_to_frontier_model(monkeypatch):
    # DefaultCaller picks a frontier model per backend (Opus / strong Codex), not
    # the fast scoring default; an explicit --model still wins.
    import clawjournal.skill.distill as d
    monkeypatch.setattr(d, "resolve_backend", lambda b: b if b in ("claude", "codex") else "claude")
    assert d.DefaultCaller(backend="claude").model == "opus"
    assert d.DefaultCaller(backend="codex").model == "gpt-5.4-mini"  # known-good fast default
    assert d.DefaultCaller(backend="claude", model="sonnet").model == "sonnet"


def test_distill_degrades_when_backend_resolution_fails(monkeypatch):
    # fix #6: DefaultCaller() resolves the backend and can raise when none is installed;
    # that must degrade to [] inside distill_skills, not escape as a traceback.
    import clawjournal.skill.distill as d

    def boom(_backend):
        raise RuntimeError("Could not detect a supported scoring backend")

    monkeypatch.setattr(d, "resolve_backend", boom)
    assert distill_skills(_corpus(), backend="auto") == []
