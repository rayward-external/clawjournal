"""Distill-due nudge (plan §16 CH-1): cheap, bounded, fail-open, never auto-runs."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from clawjournal.skill import store as _store
from clawjournal.skill.due import (
    NUDGE_BURST_SESSIONS,
    distill_due_on_connection,
    emit_session_start_nudge,
)

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)


def _seed_skill_state(conn, *, installed_days_ago=15.0, last_seen_days_ago=None,
                      nudge_enabled=True):
    """Insert one skill_rules row anchoring the user's last skill activity."""
    from clawjournal.config import load_config, save_config
    from clawjournal.skill.due import set_nudge_hook_requested

    cfg = load_config()
    cfg["projects_confirmed"] = True
    save_config(cfg)
    if nudge_enabled:   # the nudge is opt-in via `skill --install-nudge`
        set_nudge_hook_requested(conn, True, now=NOW - timedelta(days=30))
    _store.ensure_table(conn)
    installed = (NOW - timedelta(days=installed_days_ago)).isoformat()
    seen = (NOW - timedelta(days=last_seen_days_ago)).isoformat() \
        if last_seen_days_ago is not None else installed
    conn.execute(
        "INSERT INTO skill_rules (fingerprint, kind, guidance, state, "
        "installed_at, last_seen_at) VALUES ('fp1', 'avoid', 'g', 'kept', ?, ?)",
        (installed, seen),
    )
    conn.commit()


def _seed_sessions(conn, ins, n, *, failures=0, days_ago=2.0):
    start = (NOW - timedelta(days=days_ago)).isoformat()
    for i in range(n):
        ins(conn, f"s{i}", start_time=start,
            fvs=4 if i < failures else None,
            learning="x" if i < failures else None)


def test_nudge_requires_explicit_opt_in(index_conn, ins):
    # the SessionStart hook is shared with auto-upload, so hook presence is not
    # consent: without --install-nudge the check is inert (Codex re-review)
    _seed_skill_state(index_conn, installed_days_ago=15, nudge_enabled=False)
    _seed_sessions(index_conn, ins, 6)
    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "nudge-not-enabled"


def test_uninstall_nudge_silences_an_installed_hook(index_conn, ins):
    # --uninstall-nudge may leave the shared hook installed for uploads; the
    # nudge itself must still stop firing
    from clawjournal.skill.due import set_nudge_hook_requested
    _seed_skill_state(index_conn, installed_days_ago=15)
    _seed_sessions(index_conn, ins, 6)
    assert distill_due_on_connection(index_conn, NOW).due
    set_nudge_hook_requested(index_conn, False)
    lines: list[str] = []
    assert emit_session_start_nudge(
        "claude", now=NOW, conn_factory=lambda: index_conn,
        printer=lines.append) is False
    assert lines == []


def test_never_distilled_is_never_nudged(index_conn):
    # opted in, but no skill_rules rows yet (never ran the pipeline)
    from clawjournal.skill.due import set_nudge_hook_requested
    set_nudge_hook_requested(index_conn, True, now=NOW)
    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "never-distilled"


def test_fresh_install_not_due(index_conn, ins):
    _seed_skill_state(index_conn, installed_days_ago=3)
    _seed_sessions(index_conn, ins, 30)
    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "fresh"


def test_preview_run_counts_as_activity(index_conn, ins):
    # installed long ago but the user ran --preview 2d ago (last_seen bumped)
    _seed_skill_state(index_conn, installed_days_ago=40, last_seen_days_ago=2)
    _seed_sessions(index_conn, ins, 30)
    assert not distill_due_on_connection(index_conn, NOW).due


def test_skill_anchor_uses_latest_instant_across_mixed_offsets(index_conn, ins):
    """A lexically newer timestamp can be an older actual instant."""
    from clawjournal.config import load_config, save_config
    from clawjournal.skill.due import set_nudge_hook_requested

    cfg = load_config()
    cfg["projects_confirmed"] = True
    save_config(cfg)
    set_nudge_hook_requested(index_conn, True, now=NOW - timedelta(days=30))
    _store.ensure_table(index_conn)
    actual_latest = (NOW - timedelta(days=6)).astimezone(
        timezone(timedelta(hours=-12))
    ).isoformat()
    lexical_latest_but_actually_old = (NOW - timedelta(days=7)).astimezone(
        timezone(timedelta(hours=14))
    ).isoformat()
    assert lexical_latest_but_actually_old > actual_latest
    index_conn.executemany(
        "INSERT INTO skill_rules (fingerprint, kind, guidance, state, "
        "installed_at, last_seen_at) VALUES (?, 'avoid', 'g', 'kept', ?, ?)",
        [
            ("fp-latest", actual_latest, actual_latest),
            ("fp-older", lexical_latest_but_actually_old,
             lexical_latest_but_actually_old),
        ],
    )
    index_conn.commit()
    _seed_sessions(index_conn, ins, 6, failures=3)

    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "fresh"
    assert status.days_since == pytest.approx(6.0)


def test_malformed_skill_anchor_fails_silent(index_conn):
    from clawjournal.skill.due import set_nudge_hook_requested

    set_nudge_hook_requested(index_conn, True, now=NOW - timedelta(days=30))
    _store.ensure_table(index_conn)
    index_conn.execute(
        "INSERT INTO skill_rules (fingerprint, kind, guidance, state, "
        "installed_at, last_seen_at) VALUES "
        "('fp-bad', 'avoid', 'g', 'kept', 'not-a-time', NULL)"
    )
    index_conn.commit()
    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "skill-state-unavailable"


def test_stale_with_activity_is_due(index_conn, ins):
    _seed_skill_state(index_conn, installed_days_ago=15)
    _seed_sessions(index_conn, ins, 6)
    status = distill_due_on_connection(index_conn, NOW)
    assert status.due and status.new_sessions == 6
    assert "clawjournal skill --preview" in status.message
    assert "6 new sessions" in status.message


def test_quiet_window_not_due(index_conn, ins):
    _seed_skill_state(index_conn, installed_days_ago=20)
    _seed_sessions(index_conn, ins, 3)   # below NUDGE_MIN_NEW_SESSIONS
    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "quiet"


def test_between_min_and_stale_needs_an_event(index_conn, ins):
    # 8 days: staleness alone isn't enough — a failure event or a burst is needed
    _seed_skill_state(index_conn, installed_days_ago=8)
    _seed_sessions(index_conn, ins, 6)
    assert distill_due_on_connection(index_conn, NOW).reason == "waiting"

    # failure evidence crosses the event bar
    for i in range(3):
        index_conn.execute(
            "UPDATE sessions SET ai_failure_value_score = 4, ai_learning_summary='x' "
            "WHERE session_id = ?", (f"s{i}",))
    index_conn.commit()
    status = distill_due_on_connection(index_conn, NOW)
    assert status.due and status.failure_sessions == 3
    assert "failure evidence" in status.message


def test_burst_of_sessions_is_an_event(index_conn, ins):
    _seed_skill_state(index_conn, installed_days_ago=8)
    _seed_sessions(index_conn, ins, NUDGE_BURST_SESSIONS)
    assert distill_due_on_connection(index_conn, NOW).due


def test_sessions_before_anchor_do_not_count(index_conn, ins):
    _seed_skill_state(index_conn, installed_days_ago=15)
    _seed_sessions(index_conn, ins, 30, days_ago=20)   # all predate the install
    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "quiet"


def test_out_of_scope_sources_do_not_count(index_conn, ins):
    _seed_skill_state(index_conn, installed_days_ago=15)
    start = (NOW - timedelta(days=2)).isoformat()
    for i in range(10):
        ins(index_conn, f"cur{i}", source="cursor", start_time=start)
    assert distill_due_on_connection(index_conn, NOW).reason == "quiet"


def test_emit_prints_once_then_cools_down(index_conn, ins):
    _seed_skill_state(index_conn, installed_days_ago=15)
    _seed_sessions(index_conn, ins, 6)
    lines: list[str] = []
    emitted = emit_session_start_nudge(
        "claude", now=NOW, conn_factory=lambda: index_conn, printer=lines.append)
    assert emitted and len(lines) == 1
    # the emit closed the connection; reopen for the second check
    from clawjournal.workbench.index import open_index
    conn2 = open_index()
    try:
        status = distill_due_on_connection(conn2, NOW + timedelta(hours=6))
        assert not status.due and status.reason == "cooldown"
    finally:
        conn2.close()


def test_emit_fails_open_without_db():
    assert emit_session_start_nudge("claude", now=NOW, conn_factory=lambda: None) is False


def test_emit_fails_open_on_factory_error():
    def boom():
        raise RuntimeError("no db")
    assert emit_session_start_nudge("claude", now=NOW, conn_factory=boom) is False


def test_not_due_prints_nothing(index_conn):
    lines: list[str] = []
    assert emit_session_start_nudge(
        "claude", now=NOW, conn_factory=lambda: index_conn, printer=lines.append) is False
    assert lines == []


# --- hook lifecycle (decoupled from auto-upload enrollment) -------------------

def test_install_nudge_command_sets_flag_and_installs(index_conn, monkeypatch, capsys):
    from clawjournal.cli_skill import _run_nudge_hook_command
    from clawjournal.skill.due import nudge_hook_requested
    calls: list[str] = []
    def install(agent):
        calls.append(agent)
        return [
            {"agent": "claude", "changed": True, "path": "p1", "configured": True},
            {"agent": "codex", "changed": False, "path": "p2", "configured": True},
        ]

    monkeypatch.setattr("clawjournal.auto_upload._snapshot_hook_files", lambda _targets: {})
    monkeypatch.setattr("clawjournal.auto_upload.install_hooks", install)
    _run_nudge_hook_command(install=True)
    assert calls == ["all"]
    assert nudge_hook_requested(index_conn) is True
    out = capsys.readouterr().out
    assert "Nothing runs automatically" in out
    assert "model provider" in out and "failure-evidence counts" in out


def test_install_nudge_rolls_back_marker_when_hook_install_fails(
    index_conn, monkeypatch
):
    from clawjournal.cli_skill import _run_nudge_hook_command
    from clawjournal.skill.due import nudge_hook_requested

    monkeypatch.setattr("clawjournal.auto_upload._snapshot_hook_files", lambda _targets: {})
    monkeypatch.setattr("clawjournal.auto_upload._restore_hook_files", lambda _snapshot: None)
    monkeypatch.setattr(
        "clawjournal.auto_upload.install_hooks",
        lambda agent: (_ for _ in ()).throw(OSError("write denied")),
    )
    with pytest.raises(OSError, match="write denied"):
        _run_nudge_hook_command(install=True)
    assert nudge_hook_requested(index_conn) is False


def test_failed_hook_refresh_preserves_preexisting_nudge_marker(
    index_conn, monkeypatch
):
    from clawjournal.cli_skill import _run_nudge_hook_command
    from clawjournal.skill.due import nudge_hook_requested, set_nudge_hook_requested

    set_nudge_hook_requested(index_conn, True, now=NOW)
    monkeypatch.setattr("clawjournal.auto_upload._snapshot_hook_files", lambda _targets: {})
    monkeypatch.setattr("clawjournal.auto_upload._restore_hook_files", lambda _snapshot: None)
    monkeypatch.setattr(
        "clawjournal.auto_upload.install_hooks",
        lambda agent: (_ for _ in ()).throw(OSError("write denied")),
    )
    with pytest.raises(OSError, match="write denied"):
        _run_nudge_hook_command(install=True)
    assert nudge_hook_requested(index_conn) is True


def test_uninstall_nudge_keeps_only_hooks_uploads_own(index_conn, monkeypatch, capsys):
    from clawjournal.cli_skill import _run_nudge_hook_command
    from clawjournal.skill import due as due_mod
    due_mod.set_nudge_hook_requested(index_conn, True, now=NOW)
    removed: list[str] = []
    monkeypatch.setattr("clawjournal.auto_upload._snapshot_hook_files", lambda _targets: {})
    monkeypatch.setattr(
        "clawjournal.auto_upload.install_hooks",
        lambda agent: [{"agent": agent, "configured": True, "changed": False, "path": agent}],
    )
    monkeypatch.setattr("clawjournal.auto_upload.uninstall_agent_hook",
                        lambda agent: removed.append(agent))
    monkeypatch.setattr("clawjournal.auto_upload.get_auto_upload_enrollment",
                        lambda conn: {"mode": "enabled", "hook_targets": ["claude"]})
    _run_nudge_hook_command(install=False)
    assert removed == ["codex"]
    assert due_mod.nudge_hook_requested(index_conn) is False
    out = capsys.readouterr().out
    assert "kept claude" in out and "removed the shared hook for codex" in out


def test_uninstall_nudge_removes_hook_when_uploads_do_not(index_conn, monkeypatch, capsys):
    from clawjournal.cli_skill import _run_nudge_hook_command
    removed: list[str] = []
    monkeypatch.setattr("clawjournal.auto_upload._snapshot_hook_files", lambda _targets: {})
    monkeypatch.setattr("clawjournal.auto_upload.uninstall_agent_hook",
                        lambda agent: removed.append(agent))
    monkeypatch.setattr("clawjournal.auto_upload.get_auto_upload_enrollment",
                        lambda conn: None)
    _run_nudge_hook_command(install=False)
    assert removed == ["claude", "codex"]
    assert "hook removed" in capsys.readouterr().out


def test_reconcile_shared_hooks_respects_nudge_flag(index_conn, monkeypatch):
    from clawjournal import auto_upload
    from clawjournal.skill import due as due_mod
    installed: list[str] = []
    removed: list[str] = []
    monkeypatch.setattr(
        auto_upload, "install_hooks",
        lambda agent: installed.append(agent) or [
            {"configured": True, "agent": agent}
        ],
    )
    monkeypatch.setattr(auto_upload, "uninstall_agent_hook", removed.append)

    auto_upload._reconcile_shared_hooks(index_conn, upload_targets=[])
    assert installed == [] and removed == ["claude", "codex"]

    removed.clear()
    due_mod.set_nudge_hook_requested(index_conn, True, now=NOW)
    auto_upload._reconcile_shared_hooks(index_conn, upload_targets=[])
    assert installed == ["all"] and removed == []


def test_unknown_nudge_ownership_neither_creates_nor_removes_hooks(
    index_conn, monkeypatch
):
    from clawjournal import auto_upload
    installed: list[str] = []
    removed: list[str] = []
    monkeypatch.setattr(auto_upload, "install_hooks", lambda agent: installed.append(agent))
    monkeypatch.setattr(auto_upload, "uninstall_agent_hook", removed.append)
    monkeypatch.setattr(
        "clawjournal.skill.due.nudge_hook_ownership", lambda conn: None
    )
    auto_upload._reconcile_shared_hooks(index_conn, upload_targets=[])
    assert installed == [] and removed == []


def test_unknown_upload_ownership_neither_creates_nor_removes_hooks(
    index_conn, monkeypatch
):
    from clawjournal import auto_upload

    installed: list[str] = []
    removed: list[str] = []
    monkeypatch.setattr(auto_upload, "install_hooks", lambda agent: installed.append(agent))
    monkeypatch.setattr(auto_upload, "uninstall_agent_hook", removed.append)
    monkeypatch.setattr(
        auto_upload, "get_auto_upload_enrollment",
        lambda conn: (_ for _ in ()).throw(sqlite3.OperationalError("busy")),
    )
    auto_upload._reconcile_shared_hooks(index_conn)
    assert installed == [] and removed == []


def test_pending_enable_intent_owns_exact_hook_targets():
    from clawjournal.auto_upload import _enrollment_hook_targets

    assert _enrollment_hook_targets({
        "mode": "off",
        "last_result_code": "enrollment_pending",
        "hook_targets": ["claude"],
    }) == {"claude"}


@pytest.mark.parametrize(
    "enrollment",
    [
        {"mode": "enabled", "hook_targets": []},
        {"mode": "paused", "hook_targets": []},
        {"mode": "off", "last_result_code": "enrollment_pending", "hook_targets": []},
        {"mode": "future-mode", "hook_targets": ["claude"]},
    ],
)
def test_corrupt_active_enrollment_ownership_preserves_without_creating(
    index_conn, monkeypatch, enrollment
):
    from clawjournal import auto_upload

    assert auto_upload._enrollment_hook_targets(enrollment) is None
    installed: list[str] = []
    removed: list[str] = []
    monkeypatch.setattr(auto_upload, "get_auto_upload_enrollment", lambda conn: enrollment)
    monkeypatch.setattr(auto_upload, "install_hooks", lambda agent: installed.append(agent))
    monkeypatch.setattr(auto_upload, "uninstall_agent_hook", removed.append)
    auto_upload._reconcile_shared_hooks(index_conn)
    assert installed == [] and removed == []


def test_malformed_nudge_marker_preserves_without_creating(index_conn, monkeypatch):
    from clawjournal import auto_upload
    from clawjournal.skill.due import (
        nudge_hook_ownership,
        nudge_hook_requested,
        set_nudge_hook_requested,
    )

    set_nudge_hook_requested(index_conn, True, now=NOW)
    index_conn.execute(
        "UPDATE skill_nudge_state SET value = 'truncated' "
        "WHERE key = 'nudge_hook_requested_at'"
    )
    index_conn.commit()
    assert nudge_hook_requested(index_conn) is False
    assert nudge_hook_ownership(index_conn) is None
    installed: list[str] = []
    removed: list[str] = []
    monkeypatch.setattr(auto_upload, "install_hooks", lambda agent: installed.append(agent))
    monkeypatch.setattr(auto_upload, "uninstall_agent_hook", removed.append)
    auto_upload._reconcile_shared_hooks(index_conn, upload_targets=[])
    assert installed == [] and removed == []


def test_skill_rules_table_present_but_empty_is_never_distilled(index_conn):
    from clawjournal.skill.due import set_nudge_hook_requested
    set_nudge_hook_requested(index_conn, True, now=NOW)
    _store.ensure_table(index_conn)   # table exists, no rows (e.g. everything rejected)
    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "never-distilled"


def test_unparseable_start_time_never_counts_as_activity(index_conn, ins):
    # 'unknown' compares lexicographically greater than any ISO anchor; select.py
    # excludes such rows from the corpus and the nudge must not count them either
    # (else it re-nudges forever about sessions the run can never use).
    _seed_skill_state(index_conn, installed_days_ago=15)
    for i in range(6):
        ins(index_conn, f"bad{i}", start_time="unknown")
    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "quiet"


def test_confirmed_source_scope_bounds_the_count(index_conn, ins):
    # a user scoped to claude must not be nudged about codex-only activity the
    # suggested `clawjournal skill` run would then exclude from selection
    from clawjournal.config import load_config, save_config
    cfg = load_config()
    cfg["source"] = "claude"
    save_config(cfg)
    _seed_skill_state(index_conn, installed_days_ago=15)
    _seed_sessions(index_conn, ins, 6)   # default source=codex
    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "quiet"


def test_excluded_projects_do_not_count(index_conn, ins):
    # config `--exclude proj` normalizes to claude:proj — same semantics as the
    # select/export gates, so only the matching claude sessions stop counting
    from clawjournal.config import load_config, save_config
    cfg = load_config()
    cfg["excluded_projects"] = ["proj"]
    save_config(cfg)
    _seed_skill_state(index_conn, installed_days_ago=15)
    start = (NOW - timedelta(days=2)).isoformat()
    for i in range(6):
        ins(index_conn, f"c{i}", source="claude", project="proj", start_time=start)
    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "quiet"


def test_held_sessions_do_not_count(index_conn, ins):
    # the nudge line reaches agent context, so even aggregate counts must honor
    # the hold-state gate (Codex review, PR #181)
    _seed_skill_state(index_conn, installed_days_ago=15)
    start = (NOW - timedelta(days=2)).isoformat()
    for i in range(6):
        ins(index_conn, f"h{i}", start_time=start, hold_state="pending_review")
    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "quiet"


def test_malformed_iso_lookalike_start_time_never_counts(index_conn, ins):
    # '9999-99-99garbage' passes the GLOB prefix but must fail the precise
    # parse; future-dated rows are bounded out too
    _seed_skill_state(index_conn, installed_days_ago=15)
    for i in range(3):
        ins(index_conn, f"m{i}", start_time="9999-99-99garbage")
    for i in range(3):
        ins(index_conn, f"f{i}", start_time="2027-01-01T00:00:00+00:00")
    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "quiet"


def test_held_rows_cannot_crowd_out_eligible_ones(index_conn, ins):
    # the row cap is applied AFTER the cheap eligibility filters, so a pile of
    # held sessions can no longer hide real activity (Codex re-review)
    from clawjournal.skill.due import _PAGE_SIZE
    _seed_skill_state(index_conn, installed_days_ago=15)
    start_old = (NOW - timedelta(days=5)).isoformat()
    for i in range(_PAGE_SIZE * 3):
        ins(index_conn, f"held{i}", start_time=start_old, hold_state="pending_review")
    start_new = (NOW - timedelta(days=1)).isoformat()
    for i in range(6):
        ins(index_conn, f"ok{i}", start_time=start_new)
    status = distill_due_on_connection(index_conn, NOW)
    assert status.due and status.new_sessions == 6


def test_future_dated_rows_cannot_crowd_out_eligible_ones(index_conn, ins):
    # both time bounds are applied before the cap, so a pile of future-dated
    # rows can't consume it (Codex re-review round 2)
    from clawjournal.skill.due import _PAGE_SIZE
    _seed_skill_state(index_conn, installed_days_ago=15)
    future = (NOW + timedelta(days=400)).isoformat()
    for i in range(_PAGE_SIZE * 3):
        ins(index_conn, f"fut{i}", start_time=future)
    start = (NOW - timedelta(days=1)).isoformat()
    for i in range(6):
        ins(index_conn, f"ok{i}", start_time=start)
    status = distill_due_on_connection(index_conn, NOW)
    assert status.due and status.new_sessions == 6


def test_excluded_project_rows_cannot_crowd_out_eligible_ones(index_conn, ins):
    from clawjournal.config import load_config, save_config
    from clawjournal.skill.due import _PAGE_SIZE
    cfg = load_config()
    cfg["excluded_projects"] = ["secret"]
    save_config(cfg)
    _seed_skill_state(index_conn, installed_days_ago=15)
    old = (NOW - timedelta(days=5)).isoformat()
    for i in range(_PAGE_SIZE * 3):
        ins(index_conn, f"ex{i}", source="claude", project="secret", start_time=old)
    recent = (NOW - timedelta(days=1)).isoformat()
    for i in range(6):
        ins(index_conn, f"ok{i}", source="claude", project="fine", start_time=recent)
    status = distill_due_on_connection(index_conn, NOW)
    assert status.due and status.new_sessions == 6


def test_active_embargo_rows_cannot_crowd_out_eligible_ones(index_conn, ins):
    # an ACTIVE embargo is only detectable via the release gate, i.e. after SQL;
    # paging (not a single LIMIT) keeps such rows from consuming the budget
    from clawjournal.skill.due import _PAGE_SIZE
    _seed_skill_state(index_conn, installed_days_ago=15)
    recent = (NOW - timedelta(days=1)).isoformat()
    future = (NOW + timedelta(days=30)).isoformat()
    for i in range(_PAGE_SIZE * 3):
        ins(index_conn, f"emb{i}", start_time=recent, hold_state="embargoed")
    index_conn.execute("UPDATE sessions SET embargo_until = ?", (future,))
    older = (NOW - timedelta(days=5)).isoformat()
    for i in range(6):
        ins(index_conn, f"ok{i}", start_time=older)
    status = distill_due_on_connection(index_conn, NOW)
    assert status.due and status.new_sessions == 6


def test_legacy_claude_exclusion_rows_cannot_crowd_out_eligible_ones(index_conn, ins):
    # the legacy `claude:<long-path>` exclusion form is resolved in Python, so
    # those rows also survive SQL and must not consume the budget either
    from clawjournal.config import load_config, save_config
    from clawjournal.skill.due import _PAGE_SIZE
    cfg = load_config()
    cfg["excluded_projects"] = ["claude:Users-kai-code-my-proj"]
    save_config(cfg)
    _seed_skill_state(index_conn, installed_days_ago=15)
    recent = (NOW - timedelta(days=1)).isoformat()
    for i in range(_PAGE_SIZE * 3):
        ins(index_conn, f"leg{i}", source="claude", project="claude:my-proj",
            start_time=recent)
    older = (NOW - timedelta(days=5)).isoformat()
    for i in range(6):
        ins(index_conn, f"ok{i}", source="claude", project="claude:other",
            start_time=older)
    status = distill_due_on_connection(index_conn, NOW)
    assert status.due and status.new_sessions == 6


def test_expired_embargo_counts_again(index_conn, ins):
    # an embargo that has lapsed is shareable again; the SQL prefilter must not
    # hard-exclude it before the release gate can resolve that
    _seed_skill_state(index_conn, installed_days_ago=15)
    start = (NOW - timedelta(days=2)).isoformat()
    # The wall-clock representation sorts AFTER NOW even though the instant is
    # four hours in the past. SQL must compare instants, not ISO text.
    past = (NOW - timedelta(hours=4)).astimezone(
        timezone(timedelta(hours=14))
    ).isoformat()
    for i in range(6):
        ins(index_conn, f"emb{i}", start_time=start, hold_state="embargoed")
    index_conn.execute("UPDATE sessions SET embargo_until = ?", (past,))
    index_conn.commit()
    status = distill_due_on_connection(index_conn, NOW)
    assert status.due and status.new_sessions == 6


def test_active_embargo_still_does_not_count(index_conn, ins):
    _seed_skill_state(index_conn, installed_days_ago=15)
    start = (NOW - timedelta(days=2)).isoformat()
    future = (NOW + timedelta(days=30)).isoformat()
    for i in range(6):
        ins(index_conn, f"emb{i}", start_time=start, hold_state="embargoed")
    index_conn.execute("UPDATE sessions SET embargo_until = ?", (future,))
    index_conn.commit()
    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "quiet"


def test_active_embargoes_do_not_consume_the_scan_budget(index_conn, ins):
    # the full hold gate is expressed in SQL, so held rows are never scanned at
    # all — 2000 of them used to starve the six real sessions behind them
    from clawjournal.skill.due import _SCAN_BUDGET
    _seed_skill_state(index_conn, installed_days_ago=15)
    recent = (NOW - timedelta(days=1)).isoformat()
    # The wall-clock representation sorts BEFORE NOW even though the instant is
    # four hours in the future. All active rows must be removed before LIMIT.
    future = (NOW + timedelta(hours=4)).astimezone(
        timezone(timedelta(hours=-12))
    ).isoformat()
    for i in range(_SCAN_BUDGET):
        ins(index_conn, f"emb{i}", start_time=recent, hold_state="embargoed")
    index_conn.execute("UPDATE sessions SET embargo_until = ?", (future,))
    older = (NOW - timedelta(days=5)).isoformat()
    for i in range(6):
        ins(index_conn, f"ok{i}", start_time=older)
    status = distill_due_on_connection(index_conn, NOW)
    assert status.due and status.new_sessions == 6


def test_short_final_page_over_threshold_still_reports_a_floor(index_conn, ins):
    # stopping early because the count settled every threshold makes it a floor,
    # even when the last page was short (the flag must not key off the loop exit)
    from clawjournal.skill.due import _PAGE_SIZE, NUDGE_BURST_SESSIONS
    assert NUDGE_BURST_SESSIONS < 30 < _PAGE_SIZE
    _seed_skill_state(index_conn, installed_days_ago=15)
    _seed_sessions(index_conn, ins, 30)
    status = distill_due_on_connection(index_conn, NOW)
    assert status.due and status.new_sessions == NUDGE_BURST_SESSIONS
    assert f"{NUDGE_BURST_SESSIONS}+" in status.message


def test_far_offset_timestamps_are_not_excluded_lexically(index_conn, ins):
    # a -12:00 session is in-window by instant but reads as out of range under a
    # raw string compare; SQL bounds are widened and the parse decides
    _seed_skill_state(index_conn, installed_days_ago=15)
    stamp = (NOW - timedelta(days=1)).astimezone(
        timezone(timedelta(hours=-12))).isoformat()
    for i in range(6):
        ins(index_conn, f"tz{i}", start_time=stamp)
    status = distill_due_on_connection(index_conn, NOW)
    assert status.due and status.new_sessions == 6


def test_saturated_count_is_reported_as_a_floor(index_conn, ins):
    from clawjournal.skill.due import _PAGE_SIZE
    _seed_skill_state(index_conn, installed_days_ago=15)
    start = (NOW - timedelta(days=1)).isoformat()
    for i in range(_PAGE_SIZE * 3):
        ins(index_conn, f"s{i}", start_time=start)
    status = distill_due_on_connection(index_conn, NOW)
    assert status.due and "+" in status.message  # a floor, not a total


def test_nudge_hook_requested_flag_round_trip(index_conn):
    from clawjournal.skill.due import nudge_hook_requested, set_nudge_hook_requested
    assert nudge_hook_requested(index_conn) is False   # table may not even exist
    set_nudge_hook_requested(index_conn, True, now=NOW)
    assert nudge_hook_requested(index_conn) is True
    set_nudge_hook_requested(index_conn, False)
    assert nudge_hook_requested(index_conn) is False


def test_rule_less_run_still_cools_the_nudge(index_conn, ins):
    # a gate-blocked / empty-distill run writes no skill_rules row; the run
    # marker must still count as conscious activity so the user is not re-nagged
    # every cooldown about a pipeline they just tried
    from clawjournal.skill.due import record_skill_run
    _seed_skill_state(index_conn, installed_days_ago=15)
    _seed_sessions(index_conn, ins, 6)
    assert distill_due_on_connection(index_conn, NOW).due
    record_skill_run(index_conn, now=NOW - timedelta(days=1))
    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "fresh"


def test_first_rule_less_run_is_a_valid_anchor(index_conn, ins):
    """The first completed run may create no skill_rules table or rows."""
    from clawjournal.config import load_config, save_config
    from clawjournal.skill.due import record_skill_run, set_nudge_hook_requested

    cfg = load_config()
    cfg["projects_confirmed"] = True
    save_config(cfg)
    set_nudge_hook_requested(index_conn, True, now=NOW - timedelta(days=30))
    record_skill_run(index_conn, now=NOW - timedelta(days=15))
    _seed_sessions(index_conn, ins, 6)

    status = distill_due_on_connection(index_conn, NOW)
    assert status.due and status.new_sessions == 6


def test_unconfirmed_projects_fail_closed_to_no_nudge(index_conn, ins):
    from clawjournal.config import load_config, save_config

    _seed_skill_state(index_conn, installed_days_ago=15)
    _seed_sessions(index_conn, ins, 6)
    cfg = load_config()
    cfg["projects_confirmed"] = False
    save_config(cfg)

    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "scope-unconfirmed"
    assert status.new_sessions == 0


def test_broken_config_is_silent_and_never_widens_scope(
    index_conn, ins, tmp_config, capsys
):
    _seed_skill_state(index_conn, installed_days_ago=15)
    _seed_sessions(index_conn, ins, 6)
    tmp_config.write_text("{broken", encoding="utf-8")

    status = distill_due_on_connection(index_conn, NOW)
    captured = capsys.readouterr()
    assert not status.due and status.reason == "scope-unavailable"
    assert status.new_sessions == 0
    assert captured.out == "" and captured.err == ""


def test_malformed_excluded_projects_never_widens_scope(index_conn, ins):
    from clawjournal.config import load_config, save_config

    _seed_skill_state(index_conn, installed_days_ago=15)
    _seed_sessions(index_conn, ins, 6)
    cfg = load_config()
    cfg["excluded_projects"] = "secret"  # type: ignore[typeddict-item]
    save_config(cfg)

    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "scope-unavailable"
    assert status.new_sessions == 0


def test_effective_policy_read_failure_never_widens_scope(
    index_conn, ins, monkeypatch
):
    _seed_skill_state(index_conn, installed_days_ago=15)
    _seed_sessions(index_conn, ins, 6)
    monkeypatch.setattr(
        "clawjournal.workbench.index.get_effective_share_settings",
        lambda conn, cfg: (_ for _ in ()).throw(sqlite3.OperationalError("busy")),
    )

    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "scope-unavailable"
    assert status.new_sessions == 0


def test_uninstall_delete_failure_is_not_reported_as_success(
    index_conn, monkeypatch, capsys
):
    from clawjournal.cli_skill import _run_nudge_hook_command
    from clawjournal.skill.due import nudge_hook_requested, set_nudge_hook_requested

    set_nudge_hook_requested(index_conn, True, now=NOW)
    index_conn.execute(
        "CREATE TRIGGER reject_nudge_delete BEFORE DELETE ON skill_nudge_state "
        "BEGIN SELECT RAISE(ABORT, 'delete denied'); END"
    )
    index_conn.commit()
    removed: list[str] = []
    monkeypatch.setattr("clawjournal.auto_upload._snapshot_hook_files", lambda _targets: {})
    monkeypatch.setattr("clawjournal.auto_upload._restore_hook_files", lambda _snapshot: None)
    monkeypatch.setattr(
        "clawjournal.auto_upload.uninstall_agent_hook", removed.append
    )

    with pytest.raises(sqlite3.IntegrityError, match="delete denied"):
        _run_nudge_hook_command(install=False)
    assert nudge_hook_requested(index_conn) is True
    assert removed == []
    assert "Nudge disabled" not in capsys.readouterr().out


def test_uninstall_preserves_hooks_when_upload_ownership_is_unknown(
    index_conn, monkeypatch, capsys
):
    from clawjournal.cli_skill import _run_nudge_hook_command
    from clawjournal.skill.due import set_nudge_hook_requested

    set_nudge_hook_requested(index_conn, True, now=NOW)
    removed: list[str] = []
    monkeypatch.setattr("clawjournal.auto_upload._snapshot_hook_files", lambda _targets: {})
    monkeypatch.setattr(
        "clawjournal.auto_upload.uninstall_agent_hook", removed.append
    )
    monkeypatch.setattr(
        "clawjournal.auto_upload.get_auto_upload_enrollment",
        lambda conn: (_ for _ in ()).throw(sqlite3.OperationalError("busy")),
    )

    _run_nudge_hook_command(install=False)
    assert removed == []
    assert "preserved conservatively" in capsys.readouterr().out
