"""End-to-end Mode A pipeline: DB -> select -> distill(fake) -> gate -> render."""

from datetime import datetime, timezone

from clawjournal.cli_skill import generate_skill

NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)


class FakeCaller:
    def __init__(self, payload):
        self.payload, self.calls = payload, 0

    def __call__(self, *, system_prompt, task_prompt):
        self.calls += 1
        return self.payload


def test_generate_end_to_end(index_conn, ins):
    ins(index_conn, "fail", fvs=5, modes='["verification_skipped"]', learning="said done before testing")
    ins(index_conn, "win", outcome="resolved", quality=5, learning="repro first then fixed")
    fake = FakeCaller({"rules": [
        {"kind": "avoid", "trigger": "before claiming done", "guidance": "run the test suite first",
         "why": "premature", "taxonomy": "verification_skipped"},
        {"kind": "do", "trigger": "fixing a bug", "guidance": "write a failing repro first", "why": "worked"},
    ]})
    res = generate_skill(index_conn, window_days=7, caller=fake, now=NOW)
    assert fake.calls == 1
    assert len(res.rules) == 2
    assert res.skill_md.startswith("---\nname: clawjournal-lessons")
    assert res.gate_issues == []
    assert res.corpus.total_failures == 1 and res.corpus.total_successes == 1


def test_unattributable_whole_doc_gate_finding_does_not_dead_end(index_conn, ins, monkeypatch):
    # #0: if the whole-doc gate flags something no individual rule reproduces (a context
    # artifact), the install must NOT dead-end (which would persist nothing + leave no
    # rejectable fingerprint, re-blocking every future run).
    from clawjournal.skill import render
    ins(index_conn, "fail", fvs=5, modes='["verification_skipped"]', learning="said done early")
    fake = FakeCaller({"rules": [
        {"kind": "avoid", "trigger": "t", "guidance": "run the test suite first", "why": "w",
         "taxonomy": "verification_skipped"}]})
    monkeypatch.setattr(render, "gate_rendered", lambda text, **kw: ["trufflehog: 1 finding(s)"])
    monkeypatch.setattr(render, "gate_secret_pii_per_rule", lambda rules, **kw: (rules, []))
    res = generate_skill(index_conn, window_days=7, caller=fake, now=NOW)
    assert res.rules and res.gate_issues == []   # not dead-ended; install can proceed


def test_generate_empty_when_no_scored_sessions(index_conn, ins):
    fake = FakeCaller({"rules": []})
    res = generate_skill(index_conn, window_days=7, caller=fake, now=NOW)
    assert res.corpus.is_empty()
    assert res.rules == []
    assert fake.calls == 0  # nothing to distill -> no LLM call


def test_generate_merges_existing_and_skips_rejected(index_conn, ins):
    from clawjournal.skill import store
    from clawjournal.skill.schema import SkillRule

    kept = SkillRule(kind="avoid", trigger="t", guidance="pre-existing kept rule", why="w", support=1)
    store.mark_installed(index_conn, [kept])                      # already installed
    banned = SkillRule(kind="do", trigger="t", guidance="rejected rule", why="w")
    store.upsert_seen(index_conn, banned)
    store.reject(index_conn, store.fingerprint(banned))

    ins(index_conn, "fail", fvs=5, modes='["verification_skipped"]', learning="said done early")
    fake = FakeCaller({"rules": [
        {"kind": "avoid", "trigger": "a", "guidance": "new fresh rule", "why": "w"},
        {"kind": "do", "trigger": "b", "guidance": "rejected rule", "why": "w"},   # must be skipped
    ]})
    res = generate_skill(index_conn, window_days=3650, caller=fake, now=NOW)
    guides = {r.guidance for r in res.rules}
    assert "pre-existing kept rule" in guides       # existing merged in
    assert "new fresh rule" in guides               # new added
    assert "rejected rule" not in guides            # rejected fingerprint skipped
    assert store.fingerprint(SkillRule(kind="avoid", trigger="a", guidance="new fresh rule", why="w")) in res.added_fps
