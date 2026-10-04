"""Explicit single-machine use of ClawJournal state on network storage."""

from __future__ import annotations

import errno
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from clawjournal import cli
from clawjournal import filesystem as filesystem_module
from clawjournal import support_diagnostics as diagnostics
from clawjournal.workbench import index as index_module
from clawjournal.workbench import index_recovery

NETWORK_STORAGE_FILENAME = filesystem_module.NETWORK_STORAGE_FILENAME

posix_only = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX file modes and advisory locks",
)


def _use_machine(monkeypatch, hostname: str) -> None:
    monkeypatch.setattr(filesystem_module.socket, "gethostname", lambda: hostname)


def _use_filesystem(monkeypatch, filesystem_type: str, risk: str) -> None:
    monkeypatch.setattr(
        filesystem_module,
        "classify_filesystem",
        lambda _path: filesystem_module.FilesystemInfo(filesystem_type, risk),
    )


def _state_storage(state_dir: Path) -> filesystem_module.FilesystemInfo:
    return filesystem_module.classify_state_storage(state_dir / "index.db")


@pytest.fixture
def network_state(tmp_path, monkeypatch) -> Path:
    """A state root that the mount table reports as NFS-backed."""

    install_dir = tmp_path / "state"
    monkeypatch.setattr(index_module, "CONFIG_DIR", install_dir)
    monkeypatch.setattr(index_module, "INDEX_DB", install_dir / "index.db")
    monkeypatch.setattr(index_module, "BLOBS_DIR", install_dir / "blobs")
    _use_filesystem(monkeypatch, "nfs4", "network")
    _use_machine(monkeypatch, "login-01.cluster.example")
    index_recovery._set_health(
        {"status": "ready", "message": "Test index is ready."}
    )
    yield install_dir
    index_recovery._set_health(
        {"status": "ready", "message": "Test index is ready."}
    )


def test_network_storage_without_allowance_explains_both_options(network_state):
    storage = _state_storage(network_state)

    assert storage.storage_migration_required is True
    assert storage.health_fields() == {
        "filesystem_type": "nfs4",
        "storage_risk": "network",
        "storage_migration_required": True,
    }
    message = filesystem_module.storage_migration_message(storage)
    assert "CLAWJOURNAL_HOME" in message
    assert "clawjournal storage allow-network" in message
    # The CLI boundary truncates refusals to 500 characters.
    assert len(message) <= 500
    with pytest.raises(index_module.UnsafeIndexStorageError):
        index_module.open_index()
    assert not network_state.exists()


def test_allowance_lets_this_machine_open_the_index(network_state):
    result, storage = filesystem_module.allow_network_storage(network_state)

    assert result == "allowed"
    assert storage.network_storage_allowed is True
    assert storage.health_fields() == {
        "filesystem_type": "nfs4",
        "storage_risk": "network",
        "storage_migration_required": False,
        "network_storage_claim": "this_machine",
    }
    conn = index_module.open_index()
    conn.close()
    assert (network_state / "index.db").is_file()

    health = index_recovery.initialize_index_health()
    assert health["status"] == "ready"
    assert health["storage_migration_required"] is False
    assert health["network_storage_claim"] == "this_machine"
    assert filesystem_module.allow_network_storage(network_state)[0] == (
        "already_allowed"
    )


@posix_only
def test_allowance_file_is_private_and_omits_the_hostname(network_state):
    filesystem_module.allow_network_storage(network_state)

    allowance = network_state / NETWORK_STORAGE_FILENAME
    assert stat.S_IMODE(allowance.stat().st_mode) == 0o600
    text = allowance.read_text(encoding="utf-8")
    assert "login-01" not in text
    assert set(json.loads(text)) == {
        "version",
        "allowed_at",
        "machine_nonce",
        "machine_digest",
    }
    # The lock probe and temporary files are cleaned up.
    assert [path.name for path in network_state.iterdir()] == [
        NETWORK_STORAGE_FILENAME
    ]


def test_another_machine_stays_blocked_until_it_takes_over(
    network_state,
    monkeypatch,
):
    filesystem_module.allow_network_storage(network_state)
    conn = index_module.open_index()
    conn.close()

    _use_machine(monkeypatch, "login-02.cluster.example")
    storage = _state_storage(network_state)
    assert storage.network_storage_claim == "other_machine"
    assert storage.storage_migration_required is True
    with pytest.raises(index_module.UnsafeIndexStorageError) as exc_info:
        index_module.open_index()
    assert "--take-over" in str(exc_info.value)
    health = index_recovery.inspect_index_health()
    assert health["status"] == "unavailable"
    assert health["code"] == "storage_migration_required"
    assert health["network_storage_claim"] == "other_machine"
    assert filesystem_module.allow_network_storage(network_state)[0] == (
        "claimed_elsewhere"
    )

    result, storage = filesystem_module.allow_network_storage(
        network_state,
        take_over=True,
    )
    assert result == "taken_over"
    assert storage.network_storage_allowed is True
    conn = index_module.open_index()
    conn.close()

    _use_machine(monkeypatch, "login-01.cluster.example")
    with pytest.raises(index_module.UnsafeIndexStorageError):
        index_module.open_index()


def test_running_workbench_fails_closed_after_another_machine_takes_over(
    network_state,
    monkeypatch,
):
    filesystem_module.allow_network_storage(network_state)
    assert index_recovery.initialize_index_health()["status"] == "ready"

    _use_machine(monkeypatch, "login-02.cluster.example")
    filesystem_module.allow_network_storage(network_state, take_over=True)
    _use_machine(monkeypatch, "login-01.cluster.example")

    health = index_recovery.synchronize_index_health()
    assert health["status"] == "unavailable"
    assert health["storage_migration_required"] is True
    assert health["network_storage_claim"] == "other_machine"


@pytest.mark.parametrize(
    "content",
    ("", "{not json", json.dumps([]), json.dumps({"version": 1})),
    ids=("empty", "malformed", "not-object", "missing-machine"),
)
def test_unreadable_allowance_fails_closed(network_state, content):
    network_state.mkdir()
    (network_state / NETWORK_STORAGE_FILENAME).write_text(content, encoding="utf-8")

    storage = _state_storage(network_state)

    assert storage.network_storage_claim == "other_machine"
    assert storage.storage_migration_required is True
    assert filesystem_module.allow_network_storage(network_state)[0] == (
        "claimed_elsewhere"
    )
    assert filesystem_module.allow_network_storage(
        network_state,
        take_over=True,
    )[0] == "taken_over"


@pytest.mark.parametrize(
    ("filesystem_type", "risk"),
    (("ext4", "local"), ("unknown", "unknown")),
)
def test_allowance_is_not_written_for_local_or_unknown_storage(
    network_state,
    monkeypatch,
    filesystem_type,
    risk,
):
    _use_filesystem(monkeypatch, filesystem_type, risk)

    result, storage = filesystem_module.allow_network_storage(network_state)

    assert result == "not_needed"
    assert storage == filesystem_module.FilesystemInfo(filesystem_type, risk)
    assert not network_state.exists()


def test_copied_allowance_does_not_affect_local_storage(network_state, monkeypatch):
    filesystem_module.allow_network_storage(network_state)
    _use_machine(monkeypatch, "laptop.example")
    _use_filesystem(monkeypatch, "ext4", "local")

    assert _state_storage(network_state) == filesystem_module.FilesystemInfo(
        "ext4", "local"
    )
    conn = index_module.open_index()
    conn.close()


@posix_only
def test_filesystem_without_file_locks_is_not_allowed(network_state, monkeypatch):
    import fcntl

    def unsupported(*_args):
        raise OSError(errno.ENOSYS, "Function not implemented")

    monkeypatch.setattr(fcntl, "lockf", unsupported)

    result, storage = filesystem_module.allow_network_storage(network_state)

    assert result == "locks_unsupported"
    assert storage.storage_migration_required is True
    assert list(network_state.iterdir()) == []


def test_concurrent_allowance_from_another_machine_is_not_overwritten(
    network_state,
    monkeypatch,
):
    def another_machine_wins(directory: Path) -> bool:
        # Another machine writes its allowance after this one checked for an
        # existing allowance but before it writes its own.
        _use_machine(monkeypatch, "login-02.cluster.example")
        nonce = "0" * 64
        (directory / NETWORK_STORAGE_FILENAME).write_text(
            json.dumps({
                "version": 1,
                "machine_nonce": nonce,
                "machine_digest": (
                    filesystem_module._network_storage_machine_digest(nonce)
                ),
            }),
            encoding="utf-8",
        )
        _use_machine(monkeypatch, "login-01.cluster.example")
        return True

    monkeypatch.setattr(
        filesystem_module,
        "_file_locks_supported",
        another_machine_wins,
    )

    result, storage = filesystem_module.allow_network_storage(network_state)

    assert result == "claimed_elsewhere"
    assert storage.storage_migration_required is True
    _use_machine(monkeypatch, "login-02.cluster.example")
    assert _state_storage(network_state).network_storage_allowed is True


def test_doctor_inspects_an_index_on_allowed_network_storage(network_state):
    filesystem_module.allow_network_storage(network_state)
    conn = index_module.open_index()
    conn.close()

    report = diagnostics.collect_index_diagnostics(state_dir=network_state)

    assert report["storage"] == {
        "filesystem_type": "nfs4",
        "storage_risk": "network",
        "storage_migration_required": False,
    }
    assert report["index"]["exists"] is True
    assert report["index"]["health_code"] == "healthy"
    assert str(network_state) not in json.dumps(report)


def _run_cli(monkeypatch, *argv: str) -> None:
    monkeypatch.setattr(cli, "_should_auto_update", lambda argv=None: False)
    monkeypatch.setattr(sys, "argv", ["clawjournal", *argv])
    cli.main()


def test_cli_allow_network_reports_each_outcome(network_state, monkeypatch, capsys):
    _run_cli(monkeypatch, "storage", "allow-network")
    output = capsys.readouterr().out
    assert "network storage (nfs4) for this machine only" in output
    assert "--take-over" in output
    assert str(network_state) not in output

    _run_cli(monkeypatch, "storage", "allow-network")
    assert "already allowed" in capsys.readouterr().out

    _use_machine(monkeypatch, "login-02.cluster.example")
    with pytest.raises(SystemExit) as exc_info:
        _run_cli(monkeypatch, "storage", "allow-network")
    assert exc_info.value.code == 1
    assert "--take-over" in capsys.readouterr().err

    _run_cli(monkeypatch, "storage", "allow-network", "--take-over")
    assert "Moved" in capsys.readouterr().out
    assert _state_storage(network_state).network_storage_allowed is True


def test_cli_allow_network_changes_nothing_on_local_storage(
    network_state,
    monkeypatch,
    capsys,
):
    _use_filesystem(monkeypatch, "ext4", "local")

    _run_cli(monkeypatch, "storage", "allow-network")

    assert "nothing was changed" in capsys.readouterr().out
    assert not network_state.exists()


@posix_only
def test_index_symlinks_cannot_use_independent_machine_allowances(
    tmp_path,
    monkeypatch,
):
    """Two local state roots must not both authorize the same network index."""

    shared = tmp_path / "network" / "index.db"
    shared.parent.mkdir()
    shared.write_bytes(b"shared index must remain untouched")
    mountinfo = "\n".join((
        "36 25 0:32 / / rw,relatime - ext4 /dev/root rw",
        f"37 36 0:33 / {shared.parent.as_posix()} rw - nfs4 server:/home rw",
    ))
    monkeypatch.setattr(filesystem_module.sys, "platform", "linux")
    monkeypatch.setattr(filesystem_module, "_read_linux_mountinfo", lambda: mountinfo)
    monkeypatch.setattr(
        filesystem_module,
        "_file_locks_supported",
        lambda _: pytest.fail("must not probe the local directory's locks"),
    )

    for hostname in ("node-a", "node-b"):
        _use_machine(monkeypatch, hostname)
        state_dir = tmp_path / hostname
        state_dir.mkdir()
        database = state_dir / "index.db"
        database.symlink_to(shared)
        # Even a previously written allowance matching this machine cannot
        # authorize an index stored separately from its lease and state.
        nonce = "0" * 64
        (state_dir / NETWORK_STORAGE_FILENAME).write_text(json.dumps({
            "version": 1,
            "machine_nonce": nonce,
            "machine_digest": filesystem_module._network_storage_machine_digest(nonce),
        }), encoding="utf-8")
        original_allowance = (state_dir / NETWORK_STORAGE_FILENAME).read_bytes()
        assert filesystem_module.classify_filesystem(state_dir).storage_risk == "local"
        storage = filesystem_module.classify_state_storage(database)
        assert storage.network_storage_claim == "index_symlink"
        assert storage.storage_migration_required is True
        for take_over in (False, True):
            assert filesystem_module.allow_network_storage(
                state_dir, take_over=take_over,
            )[0] == "index_symlink"
        with pytest.raises(index_module.UnsafeIndexStorageError, match="symlink"):
            index_module.open_existing_index(database=database)
        monkeypatch.setattr(index_module, "INDEX_DB", database)
        with pytest.raises(index_module.UnsafeIndexStorageError, match="symlink"):
            index_module.open_index()
        assert not (state_dir / index_module.INDEX_CONNECTION_LEASE_FILENAME).exists()
        assert (state_dir / NETWORK_STORAGE_FILENAME).read_bytes() == original_allowance
    assert shared.read_bytes() == b"shared index must remain untouched"


@posix_only
def test_symlink_to_the_whole_state_directory_remains_usable(
    network_state,
    tmp_path,
):
    filesystem_module.allow_network_storage(network_state)
    conn = index_module.open_index()
    conn.close()
    alias = tmp_path / "state-alias"
    alias.symlink_to(network_state, target_is_directory=True)

    assert _state_storage(alias).network_storage_allowed is True
    conn = index_module.open_existing_index(database=alias / "index.db")
    conn.close()


@pytest.mark.parametrize("blocked_operation", ["read", "symlink_check"])
def test_stalled_storage_checks_are_bounded_and_reuse_one_worker(
    network_state,
    monkeypatch,
    capsys,
    blocked_operation,
):
    filesystem_module.allow_network_storage(network_state)
    assert index_recovery.initialize_index_health()["status"] == "ready"
    monkeypatch.setattr(filesystem_module, "_NETWORK_STORAGE_CHECK_TIMEOUT", 0.02)
    entered = threading.Event()
    release = threading.Event()
    calls = []
    method = "read_text" if blocked_operation == "read" else "is_symlink"
    original = getattr(Path, method)
    blocked_name = NETWORK_STORAGE_FILENAME if method == "read_text" else "index.db"

    def stalled(path, *args, **kwargs):
        if path.name == blocked_name:
            calls.append(path)
            entered.set()
            release.wait(5)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, method, stalled)
    try:
        started = time.monotonic()
        storage = _state_storage(network_state)
        assert entered.wait(1)
        assert time.monotonic() - started < 1
        assert storage.network_storage_claim == "unavailable"
        assert storage.storage_migration_required is True
        database = network_state / "index.db"
        pending = filesystem_module._NETWORK_STORAGE_CHECKS[database]
        health = index_recovery.begin_index_health_check()
        assert health["status"] == "unavailable"
        assert health["network_storage_claim"] == "unavailable"
        assert index_recovery.synchronize_index_health()["status"] == "unavailable"
        report = diagnostics.collect_index_diagnostics(state_dir=network_state)
        assert report["index"]["health_code"] == "network_storage_not_inspected"
        with pytest.raises(index_module.UnsafeIndexStorageError):
            index_module.open_index()
        for take_over in (False, True):
            assert filesystem_module.allow_network_storage(
                network_state, take_over=take_over,
            )[0] == "storage_unavailable"
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(monkeypatch, "storage", "allow-network", "--take-over")
        assert exc_info.value.code == 1
        error = capsys.readouterr().err
        assert "check" in error
        assert "--take-over" not in error
        assert str(network_state) not in error
        assert len(calls) == 1
        assert filesystem_module._NETWORK_STORAGE_CHECKS[database] is pending
    finally:
        release.set()
        assert entered.wait(1)
        # Wait until the background I/O really finished before undoing mocks.
        pending = filesystem_module._NETWORK_STORAGE_CHECKS.get(network_state / "index.db")
        if pending is not None:
            assert pending.completed.wait(1)

    # Completed reads are never cached: a later check must see a new owner.
    assert network_state / "index.db" not in filesystem_module._NETWORK_STORAGE_CHECKS
    _use_machine(monkeypatch, "node-b")
    assert _state_storage(network_state).network_storage_claim == "other_machine"


@posix_only
def test_cli_explains_refused_index_symlink(network_state, monkeypatch, capsys):
    network_state.mkdir()
    (network_state / "index.db").symlink_to(network_state / "other.db")

    with pytest.raises(SystemExit) as exc_info:
        _run_cli(monkeypatch, "storage", "allow-network", "--take-over")

    assert exc_info.value.code == 1
    error = capsys.readouterr().err
    assert "symlink" in error
    assert "whole state directory" in error
    assert str(network_state) not in error
    assert not (network_state / NETWORK_STORAGE_FILENAME).exists()
