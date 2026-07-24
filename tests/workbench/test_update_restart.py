"""Tests for the daemon's post-update self-restart decision."""

from __future__ import annotations

from pathlib import Path

import pytest

from clawjournal.workbench import daemon


OLD = "a" * 40
NEW = "b" * 40
IDLE = {"in_flight": 0, "last_mutation": 0.0}


@pytest.fixture
def quiet_env(monkeypatch):
    """No --reload supervisor, HEAD moved, install fully reconciled."""
    monkeypatch.delenv(daemon.RELOAD_CHILD_ENV, raising=False)
    monkeypatch.setattr("clawjournal.selfupdate._rev_parse", lambda repo, rev: NEW)
    monkeypatch.setattr(
        "clawjournal.selfupdate.reinstall_in_progress", lambda: False
    )
    monkeypatch.setattr("clawjournal.selfupdate.reinstall_needed", lambda repo: False)
    return Path("/nonexistent-repo")


def test_restarts_when_updated_reconciled_and_idle(quiet_env):
    assert daemon._update_restart_due(
        quiet_env, OLD, now=1000.0, activity=IDLE
    ) == NEW


def test_no_restart_when_head_unchanged(quiet_env, monkeypatch):
    monkeypatch.setattr("clawjournal.selfupdate._rev_parse", lambda repo, rev: OLD)
    assert daemon._update_restart_due(quiet_env, OLD, now=1000.0, activity=IDLE) is None


def test_no_restart_when_head_unreadable(quiet_env, monkeypatch):
    monkeypatch.setattr("clawjournal.selfupdate._rev_parse", lambda repo, rev: None)
    assert daemon._update_restart_due(quiet_env, OLD, now=1000.0, activity=IDLE) is None


def test_no_restart_while_install_is_not_reconciled(quiet_env, monkeypatch):
    """A half-done update (pending reinstall, stale build) must not be served."""
    monkeypatch.setattr("clawjournal.selfupdate.reinstall_needed", lambda repo: True)
    assert daemon._update_restart_due(quiet_env, OLD, now=1000.0, activity=IDLE) is None


def test_no_restart_while_installer_owns_critical_section(
    quiet_env, monkeypatch
):
    """HEAD can move just before the updater persists its pending record."""
    monkeypatch.setattr(
        "clawjournal.selfupdate.reinstall_in_progress", lambda: True
    )
    assert daemon._update_restart_due(
        quiet_env, OLD, now=1000.0, activity=IDLE
    ) is None


def test_no_restart_with_requests_in_flight(quiet_env):
    busy = {"in_flight": 1, "last_mutation": 0.0}
    assert daemon._update_restart_due(quiet_env, OLD, now=1000.0, activity=busy) is None


def test_no_restart_during_benchmark_generation(quiet_env):
    """The restart must not terminate the expensive background worker."""
    assert daemon._BENCHMARK_GEN_LOCK.acquire(blocking=False)
    try:
        assert daemon._update_restart_due(
            quiet_env, OLD, now=1000.0, activity=IDLE
        ) is None
    finally:
        daemon._BENCHMARK_GEN_LOCK.release()


def test_no_restart_during_scoring_or_auto_upload(quiet_env, monkeypatch):
    """All expensive daemon workers must finish before a re-exec."""

    class ActiveThread:
        @staticmethod
        def is_alive() -> bool:
            return True

    scanner = daemon.Scanner()
    scanner._score_thread = ActiveThread()  # type: ignore[assignment]
    assert daemon._update_restart_due(
        quiet_env, OLD, now=1000.0, activity=IDLE, scanner=scanner
    ) is None

    scanner._score_thread = None
    monkeypatch.setattr(daemon, "_auto_upload_run_thread", ActiveThread())
    assert daemon._update_restart_due(
        quiet_env, OLD, now=1000.0, activity=IDLE, scanner=scanner
    ) is None


def test_no_restart_during_background_scan(quiet_env):
    """Do not interrupt a scan while it may be writing the SQLite index."""
    scanner = daemon.Scanner()
    assert scanner._scan_lock.acquire(blocking=False)
    try:
        assert daemon._update_restart_due(
            quiet_env, OLD, now=1000.0, activity=IDLE, scanner=scanner
        ) is None
    finally:
        scanner._scan_lock.release()


def test_no_restart_soon_after_a_mutation(quiet_env):
    recent = {"in_flight": 0, "last_mutation": 990.0}
    assert daemon._update_restart_due(quiet_env, OLD, now=1000.0, activity=recent) is None


def test_restarts_once_the_mutation_window_has_passed(quiet_env):
    old_enough = {
        "in_flight": 0,
        "last_mutation": 1000.0 - daemon._RESTART_MUTATION_IDLE_SECONDS - 1,
    }
    assert daemon._update_restart_due(
        quiet_env, OLD, now=1000.0, activity=old_enough
    ) == NEW


def test_no_restart_under_the_reload_supervisor(quiet_env, monkeypatch):
    """In dev, the --reload supervisor owns restarts; the child must not race it."""
    monkeypatch.setenv(daemon.RELOAD_CHILD_ENV, "1")
    assert daemon._update_restart_due(quiet_env, OLD, now=1000.0, activity=IDLE) is None


# ---------- activity accounting ----------------------------------------------


def test_request_accounting_tracks_in_flight_and_mutations(monkeypatch):
    monkeypatch.setattr(daemon, "_activity", {"in_flight": 0, "last_mutation": 0.0})

    assert daemon._note_request_start() is True
    assert daemon._snapshot_activity()["in_flight"] == 1

    # GETs finish without moving the mutation clock.
    daemon._note_request_end("GET")
    snap = daemon._snapshot_activity()
    assert snap["in_flight"] == 0
    assert snap["last_mutation"] == 0.0

    # Mutating methods stamp the clock on completion.
    assert daemon._note_request_start() is True
    daemon._note_request_end("POST")
    assert daemon._snapshot_activity()["last_mutation"] > 0.0


def test_request_admission_freezes_atomically_only_when_idle(monkeypatch):
    monkeypatch.setattr(daemon, "_activity", {"in_flight": 0, "last_mutation": 0.0})
    monkeypatch.setattr(daemon, "_request_admission_open", True)

    assert daemon._note_request_start() is True
    assert daemon._freeze_request_admission(now=1000.0) is False

    daemon._note_request_end("GET")
    assert daemon._freeze_request_admission(now=1000.0) is True
    assert daemon._note_request_start() is False
    assert daemon._snapshot_activity()["in_flight"] == 0


def test_request_admission_rechecks_mutation_window(monkeypatch):
    monkeypatch.setattr(
        daemon,
        "_activity",
        {"in_flight": 0, "last_mutation": 995.0},
    )
    monkeypatch.setattr(daemon, "_request_admission_open", True)

    # A mutation that completed after the pre-flight snapshot must cancel the
    # commit even though no request remains in flight.
    assert daemon._freeze_request_admission(now=1000.0) is False
    assert daemon._note_request_start() is True
    daemon._note_request_end("GET")


def test_scanner_stop_prevents_post_scan_scoring(monkeypatch):
    scanner = daemon.Scanner()
    scoring_started = False

    def finish_scan_after_restart_commit():
        scanner._stop_event.set()
        return {}

    def record_scoring(_scanner):
        nonlocal scoring_started
        scoring_started = True

    monkeypatch.setattr(scanner, "_scan_tick", finish_scan_after_restart_commit)
    monkeypatch.setattr(daemon, "trigger_scoring_warmup", record_scoring)

    scanner._run()

    assert scoring_started is False


def test_request_accounting_never_goes_negative(monkeypatch):
    monkeypatch.setattr(daemon, "_activity", {"in_flight": 0, "last_mutation": 0.0})
    daemon._note_request_end(None)  # e.g. a connection that never parsed
    assert daemon._snapshot_activity()["in_flight"] == 0


def test_serve_captures_head_before_starting_auto_update(monkeypatch):
    """A fast updater must not hide the old backend revision from the watcher."""
    from clawjournal import cli, selfupdate

    events: list[str] = []
    captured: dict[str, object] = {}
    repo = Path("/nonexistent-repo")

    monkeypatch.setattr(
        "sys.argv",
        ["clawjournal", "--source", "codex", "serve", "--no-browser"],
    )
    monkeypatch.setattr(selfupdate, "_package_repo_root", lambda: repo)

    def read_head(_repo: Path, _rev: str) -> str:
        events.append("head")
        return OLD

    monkeypatch.setattr(selfupdate, "_rev_parse", read_head)
    monkeypatch.setattr(
        selfupdate, "maybe_self_update", lambda: events.append("update")
    )
    monkeypatch.setattr(selfupdate, "pending_reinstall_notice", lambda: None)
    monkeypatch.setattr("clawjournal.desktop.note_opened", lambda: None)
    monkeypatch.setattr("clawjournal.pricing.ensure_pricing_fresh", lambda: None)
    monkeypatch.setattr(
        daemon, "run_server", lambda **kwargs: captured.update(kwargs)
    )

    cli.main()

    assert events[:2] == ["head", "update"]
    assert captured["startup_head"] == OLD
