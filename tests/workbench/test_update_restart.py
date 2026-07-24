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


def test_no_restart_with_requests_in_flight(quiet_env):
    busy = {"in_flight": 1, "last_mutation": 0.0}
    assert daemon._update_restart_due(quiet_env, OLD, now=1000.0, activity=busy) is None


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

    daemon._note_request_start()
    assert daemon._snapshot_activity()["in_flight"] == 1

    # GETs finish without moving the mutation clock.
    daemon._note_request_end("GET")
    snap = daemon._snapshot_activity()
    assert snap["in_flight"] == 0
    assert snap["last_mutation"] == 0.0

    # Mutating methods stamp the clock on completion.
    daemon._note_request_start()
    daemon._note_request_end("POST")
    assert daemon._snapshot_activity()["last_mutation"] > 0.0


def test_request_accounting_never_goes_negative(monkeypatch):
    monkeypatch.setattr(daemon, "_activity", {"in_flight": 0, "last_mutation": 0.0})
    daemon._note_request_end(None)  # e.g. a connection that never parsed
    assert daemon._snapshot_activity()["in_flight"] == 0
