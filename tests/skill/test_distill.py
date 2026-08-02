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
    system_prompt = fake.calls[0][0]
    assert "CODING-AGENT behavior" in system_prompt
    assert "Never diagnose or make workplace-performance claims" in system_prompt
    assert "EVERY selected case that directly supports" in system_prompt
    assert "rather than a personal trait or overall-work assessment" in system_prompt


def test_evidence_ids_are_limited_to_selected_sessions():
    fake = FakeCaller({"rules": [
        {"kind": "avoid", "trigger": "before done", "guidance": "run tests first",
         "why": "premature", "taxonomy": "verification_skipped",
         "evidence_session_ids": ["s1", "https://evil.test/prompt"]},
    ]})
    rules = distill_skills(_corpus(), caller=fake)
    assert rules[0].evidence_session_ids == ["case-01"]


def test_rule_inherits_cited_candidate_support():
    # a synthetic env/rejection candidate carries its OBJECTIVE session count in
    # support_count and NO taxonomy; a rule that cites it must inherit that count
    # (the taxonomy backfill can't, so the badge would otherwise be ~0 / coincidental).
    env = SkillCandidate("s_env", "proj", "claude", "avoid",
                         title="Recurring Edit error", support_count=12)   # 12 sessions, no taxonomy
    corpus = SkillCorpus(window_start="a", window_end="b", failures=[env])  # -> case-01
    fake = FakeCaller({"rules": [
        {"kind": "avoid", "trigger": "before editing", "guidance": "read the file first",
         "why": "recurring tool error", "taxonomy": "", "evidence_session_ids": ["case-01"]},
    ]})
    (rule,) = distill_skills(corpus, caller=fake)
    assert rule.support == 12                          # objective count reached the rule


def test_uncited_rule_keeps_taxonomy_support():
    # the evidence path must not regress the mode-recurrence backfill for rules that
    # name a taxonomy but cite no case.
    fake = FakeCaller({"rules": [
        {"kind": "avoid", "trigger": "before done", "guidance": "run tests first",
         "why": "premature", "taxonomy": "verification_skipped"},   # no evidence_session_ids
    ]})
    assert distill_skills(_corpus(), caller=fake)[0].support == 4   # still backfilled from mode


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
    from clawjournal.skill.distill import _candidate_aliases
    prompt = build_prompt(corpus, Anonymizer(), _candidate_aliases(corpus))
    assert "AKIAIOSFODNN7EXAMPLE" not in prompt
    assert "raw-/Users/kai/project" not in prompt
    assert "case-01" in prompt


def test_prompt_applies_custom_redactions_before_llm():
    corpus = SkillCorpus(
        window_start="a",
        window_end="b",
        failures=[SkillCandidate(
            "s1",
            "ClientName",
            "codex",
            "avoid",
            title="ClientName failure",
            learning_summary="ClientName leaked into a summary",
            score_reason="See api.internal for context",
        )],
    )
    from clawjournal.skill.distill import _candidate_aliases
    prompt = build_prompt(
        corpus,
        Anonymizer(),
        _candidate_aliases(corpus),
        {"custom_strings": ["ClientName"], "blocked_domains": ["api.internal"]},
    )
    assert "ClientName" not in prompt
    assert "api.internal" not in prompt
    assert "[REDACTED_CUSTOM]" in prompt
    assert "[REDACTED_DOMAIN]" in prompt


def test_distill_defaults_to_frontier_model(monkeypatch):
    # DefaultCaller picks a frontier model per backend (Opus / strong Codex), not
    # the fast scoring default; an explicit --model still wins.
    import clawjournal.skill.distill as d
    monkeypatch.setattr(d, "resolve_backend", lambda b: b if b in ("claude", "codex") else "claude")
    assert d.DefaultCaller(backend="claude").model == "opus"
    assert d.DefaultCaller(backend="claude").effort == "xhigh"
    assert d.DefaultCaller(backend="codex").model == "gpt-5.5"
    assert d.DefaultCaller(backend="codex").effort == "xhigh"
    assert d.DefaultCaller(backend="claude", model="sonnet").model == "sonnet"
    assert d.DefaultCaller(backend="claude", effort="max").effort == "max"


def test_default_caller_isolates_claude_with_safe_mode(monkeypatch):
    import clawjournal.skill.distill as d

    captured = {}
    monkeypatch.setattr(d, "resolve_backend", lambda _backend: "claude")

    def fake_json_call(**kwargs):
        captured.update(kwargs)
        return {"rules": []}

    monkeypatch.setattr(d, "run_agent_json_call", fake_json_call)
    d.DefaultCaller(backend="claude")(system_prompt="sys", task_prompt="task")
    assert captured["claude_safe_mode"] is True
    assert captured["claude_permission_mode"] == "default"
    assert captured["claude_tools"] == ""
    assert "claude_bare" not in captured


def test_distill_degrades_when_backend_resolution_fails(monkeypatch):
    # fix #6: DefaultCaller() resolves the backend and can raise when none is installed;
    # that must degrade to [] inside distill_skills, not escape as a traceback.
    import clawjournal.skill.distill as d

    def boom(_backend):
        raise RuntimeError("Could not detect a supported scoring backend")

    monkeypatch.setattr(d, "resolve_backend", boom)
    assert distill_skills(_corpus(), backend="auto") == []


def test_distill_error_classification():
    # #2/#3: distinguish a transient failure (no downgrade) from a plan-unavailable model
    # (downgrade) and an old-CLI flag rejection (relax flags).
    from clawjournal.skill.distill import _distill_flag_unsupported, _distill_plan_unavailable
    assert _distill_flag_unsupported("error: unknown option '--safe-mode'")
    assert _distill_flag_unsupported("unexpected argument --effort found")
    assert not _distill_flag_unsupported("Request timed out after 240s")     # transient
    assert _distill_plan_unavailable("model opus is not available on your plan")
    assert not _distill_plan_unavailable("HTTP 429 rate limit exceeded")
    assert not _distill_plan_unavailable("model opus is temporarily unavailable due to rate limits")
    assert not _distill_plan_unavailable("you are out of credits")


def test_default_caller_never_relaxes_tool_isolation(monkeypatch):
    import clawjournal.skill.distill as d
    captured = {}
    monkeypatch.setattr(d, "resolve_backend", lambda _b: "claude")
    monkeypatch.setattr(d, "run_agent_json_call",
                        lambda **kw: (captured.update(kw), {"rules": []})[1])
    d.DefaultCaller(backend="claude")(system_prompt="s", task_prompt="t")
    assert captured["claude_permission_mode"] == "default"
    assert captured["claude_tools"] == ""


# --- CH-2 must-cover: objective candidates cannot be silently dropped --------

def _objective_corpus(n=5):
    from clawjournal.skill.turns import EnvExcerpt
    env = SkillCandidate(
        "env-signature-0", "proj", "claude", "avoid",
        title="Recurring Bash error", support_count=n,
        learning_summary="Objective environment feedback: recurring tool error",
        pivotal_excerpts=[EnvExcerpt(
            action="Bash: pytest -x",
            error="ModuleNotFoundError: No module named 'foo'",
            recovery="Bash: pip install foo")],
    )
    return SkillCorpus(window_start="a", window_end="b", failures=[env])


def test_must_cover_block_lists_objective_candidates():
    from clawjournal.skill.distill import _candidate_aliases
    corpus = _objective_corpus()
    prompt = build_prompt(corpus, Anonymizer(), _candidate_aliases(corpus))
    assert "MUST-COVER" in prompt
    assert "case-01" in prompt


def test_must_cover_block_absent_without_objective_candidates():
    from clawjournal.skill.distill import _candidate_aliases
    corpus = _corpus()
    prompt = build_prompt(corpus, Anonymizer(), _candidate_aliases(corpus))
    assert "MUST-COVER" not in prompt


def test_uncovered_objective_candidate_gets_deterministic_fallback():
    corpus = _objective_corpus(n=5)
    fake = FakeCaller({"rules": [
        {"kind": "do", "trigger": "t", "guidance": "read source", "why": "w"}]})  # cites nothing
    rules = distill_skills(corpus, caller=fake)
    assert len(fake.calls) == 1                       # STILL exactly one call — no re-ask
    fallbacks = [r for r in rules if "auto-added" in r.why]
    assert len(fallbacks) == 1
    fb = fallbacks[0]
    assert fb.kind == "avoid"
    assert fb.evidence_session_ids == ["case-01"]
    assert fb.support == 5
    # guidance embeds the NORMALIZED signature (stable fingerprint), not the
    # verbatim error head; the run-specific recovery sample rides in `why`.
    assert "no module named 'foo'" in fb.guidance
    assert "pip install foo" in fb.why


def test_covered_objective_candidate_gets_no_fallback():
    corpus = _objective_corpus()
    fake = FakeCaller({"rules": [
        {"kind": "avoid", "trigger": "t", "guidance": "read the file first", "why": "w",
         "evidence_session_ids": ["case-01"]}]})
    rules = distill_skills(corpus, caller=fake)
    assert len(rules) == 1
    assert "auto-added" not in rules[0].why


def test_fallbacks_capped_and_highest_support_first():
    from clawjournal.skill.turns import EnvExcerpt, TurnExcerpt

    def env(i, n):
        name = "abc"[i]
        return SkillCandidate(
            f"env-signature-{i}", "p", "claude", "avoid",
            title=f"Recurring Tool{name} error", support_count=n,
            pivotal_excerpts=[EnvExcerpt(f"Tool{name}: x",
                                         f"err-{name} exploded badly", "")])

    rejection = SkillCandidate(
        "human-rejection", "p", "claude", "avoid",
        title="User-Rejected Actions", support_count=9,
        pivotal_excerpts=[TurnExcerpt("attempted: rm", "rejected: destructive", "")])
    corpus = SkillCorpus(window_start="a", window_end="b",
                         failures=[env(0, 3), env(1, 7), rejection])
    rules = distill_skills(corpus, caller=FakeCaller({"rules": []}))
    assert len(rules) == 2                            # capped at MAX_FALLBACK_RULES
    assert rules[0].title == "Ask Before Rejected Actions"   # support 9 first
    assert "err-b" in rules[1].guidance                       # support 7 env second


def test_backend_failure_still_degrades_without_fallback():
    # a raised call keeps the existing degrade-to-[] contract: fallbacks only
    # guarantee coverage of a SUCCESSFUL distill, never replace a failed one.
    class Boom:
        def __call__(self, *, system_prompt, task_prompt):
            raise RuntimeError("timeout")

    assert distill_skills(_objective_corpus(), caller=Boom(), model="opus") == []


def test_fallback_guidance_is_scrubbed():
    from clawjournal.skill.turns import EnvExcerpt
    env = SkillCandidate(
        "env-signature-0", "proj", "claude", "avoid",
        title="Recurring Bash error", support_count=4,
        pivotal_excerpts=[EnvExcerpt("Bash: cat config",
                                     "leaked AKIAIOSFODNN7EXAMPLE token", "")])
    corpus = SkillCorpus(window_start="a", window_end="b", failures=[env])
    rules = distill_skills(corpus, caller=FakeCaller({"rules": []}))
    assert rules
    assert "AKIAIOSFODNN7EXAMPLE" not in rules[0].guidance


def test_env_fallback_fingerprint_stable_across_first_seen_sessions():
    # the excerpt text varies by whichever session the signature scan saw first;
    # the fallback's fingerprint (kind + guidance) must NOT — else a --rejected
    # fallback reappears as [NEW] under a fresh fingerprint every window roll.
    from clawjournal.skill import store as _store
    from clawjournal.skill.turns import EnvExcerpt

    def one(error, recovery):
        env = SkillCandidate(
            "env-signature-0", "proj", "claude", "avoid",
            title="Recurring Bash error", support_count=4,
            pivotal_excerpts=[EnvExcerpt("Bash: pytest", error, recovery)])
        corpus = SkillCorpus(window_start="a", window_end="b", failures=[env])
        (rule,) = distill_skills(corpus, caller=FakeCaller({"rules": []}))
        return rule

    r1 = one("KeyError: 'x' in /tmp/aaa/file.py line 12", "Bash: fix-a")
    r2 = one("KeyError: 'x' in /tmp/bbb/other.py line 99", "Bash: fix-b")
    assert _store.fingerprint(r1) == _store.fingerprint(r2)
    assert "fix-a" in r1.why and "fix-b" in r2.why    # the sample still surfaces


def test_fallback_templates_survive_the_render_gates():
    # the guarantee is only real if the templates actually pass the gates a
    # distilled rule passes; a template drifting into the hard-deny or policy
    # regexes would silently void must-cover.
    from clawjournal.skill import render as _render
    from clawjournal.skill.turns import EnvExcerpt, TurnExcerpt

    env = SkillCandidate(
        "env-signature-0", "p", "claude", "avoid",
        title="Recurring Bash error", support_count=4,
        pivotal_excerpts=[EnvExcerpt("Bash: npm test",
                                     "Cannot find module 'left-pad'",
                                     "Bash: npm install")])
    rejection = SkillCandidate(
        "human-rejection", "p", "claude", "avoid",
        title="User-Rejected Actions", support_count=5,
        pivotal_excerpts=[TurnExcerpt("attempted: push", "rejected: force push", "")])
    corpus = SkillCorpus(window_start="a", window_end="b", failures=[env, rejection])
    rules = distill_skills(corpus, caller=FakeCaller({"rules": []}))
    assert len(rules) == 2
    kept, blocked = _render.gate_rules(rules)
    assert blocked == [] and len(kept) == 2
    kept, secret_blocked = _render.gate_secret_pii_per_rule(kept)
    assert secret_blocked == [] and len(kept) == 2
