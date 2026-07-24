"""Tests for the silent auto-update module."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from clawjournal import selfupdate


# ---------- fixtures ----------------------------------------------------------


@pytest.fixture
def isolated_config_dir(tmp_path, monkeypatch):
    """Point CONFIG_DIR at a tmp path so the stamp doesn't leak globally."""
    cfg_dir = tmp_path / "clawjournal_home"
    cfg_dir.mkdir()
    monkeypatch.setattr("clawjournal.selfupdate.CONFIG_DIR", cfg_dir)
    return cfg_dir


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "README").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--quiet", "-m", "init"],
        check=True,
    )


def _commit_file(repo: Path, name: str, content: str, message: str) -> str:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    subprocess.run(["git", "-C", str(repo), "add", name], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--quiet", "-m", message],
        check=True,
    )
    return _head(repo)


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """Create a tiny git repo and pretend the package lives inside it."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setattr("clawjournal.selfupdate._package_repo_root", lambda: repo)
    return repo


def _stub_spawn_ok(monkeypatch, sink: list[Path]) -> None:
    def fake(repo: Path) -> bool:
        sink.append(repo)
        return True
    monkeypatch.setattr("clawjournal.selfupdate._spawn_background_update", fake)


# ---------- basic guards ------------------------------------------------------


def test_opt_out_env_short_circuits(monkeypatch, isolated_config_dir):
    monkeypatch.setenv("CLAWJOURNAL_NO_AUTO_UPDATE", "1")
    assert selfupdate.maybe_self_update() == "opt-out"
    assert not (isolated_config_dir / "last_update_check").exists()


def test_not_a_checkout_returns_quietly(monkeypatch, isolated_config_dir):
    monkeypatch.delenv("CLAWJOURNAL_NO_AUTO_UPDATE", raising=False)
    monkeypatch.setattr("clawjournal.selfupdate._package_repo_root", lambda: None)
    assert selfupdate.maybe_self_update() == "not-a-checkout"


def test_skips_when_config_dir_absent(monkeypatch, tmp_path, fake_repo):
    """Fresh install — no ~/.clawjournal yet — must not be bootstrapped
    by auto-update. Otherwise `events doctor` would stop seeing this as
    a fresh install."""
    monkeypatch.delenv("CLAWJOURNAL_NO_AUTO_UPDATE", raising=False)
    missing = tmp_path / "never_created"
    monkeypatch.setattr("clawjournal.selfupdate.CONFIG_DIR", missing)
    spawned: list[Path] = []
    _stub_spawn_ok(monkeypatch, spawned)
    assert selfupdate.maybe_self_update() == "no-config-dir"
    assert not missing.exists()
    assert spawned == []


# ---------- throttle / clock-skew --------------------------------------------


def test_throttle_skips_when_stamp_fresh(monkeypatch, isolated_config_dir, fake_repo):
    monkeypatch.delenv("CLAWJOURNAL_NO_AUTO_UPDATE", raising=False)
    stamp = isolated_config_dir / "last_update_check"
    stamp.write_bytes(b"")
    now = time.time()
    os.utime(stamp, (now, now))

    spawned: list[Path] = []
    _stub_spawn_ok(monkeypatch, spawned)
    assert selfupdate.maybe_self_update(now=now) == "throttled"
    assert spawned == []


def test_clock_skew_does_not_lock_out_updates(
    monkeypatch, isolated_config_dir, fake_repo
):
    """If the wall clock jumps backward, a stamp from the 'future' must
    not be treated as fresh — otherwise the user is locked out of
    updates until the clock catches up."""
    monkeypatch.delenv("CLAWJOURNAL_NO_AUTO_UPDATE", raising=False)
    stamp = isolated_config_dir / "last_update_check"
    stamp.write_bytes(b"")
    now = time.time()
    # Stamp is one hour in the FUTURE relative to `now` (clock rewound).
    future = now + 3600
    os.utime(stamp, (future, future))

    spawned: list[Path] = []
    _stub_spawn_ok(monkeypatch, spawned)
    assert selfupdate.maybe_self_update(now=now) == "spawned"
    assert spawned == [fake_repo]


# ---------- stamp ordering / transient git failure ---------------------------


def test_stamp_written_before_spawn_so_failures_dont_loop(
    monkeypatch, isolated_config_dir, fake_repo
):
    """If the network hangs forever, the *next* invocation must see the
    fresh throttle stamp and bail out — so the stamp has to be on disk
    by the time _spawn_background_update is called."""
    monkeypatch.delenv("CLAWJOURNAL_NO_AUTO_UPDATE", raising=False)

    stamp_seen_at_spawn = {"existed": False}

    def fake_spawn(repo: Path) -> bool:
        stamp_seen_at_spawn["existed"] = (
            isolated_config_dir / "last_update_check"
        ).exists()
        return True

    monkeypatch.setattr("clawjournal.selfupdate._spawn_background_update", fake_spawn)
    assert selfupdate.maybe_self_update() == "spawned"
    assert stamp_seen_at_spawn["existed"] is True


def test_transient_git_failure_does_not_burn_throttle(
    monkeypatch, isolated_config_dir, fake_repo
):
    """A transient `git status` failure (Windows AV, index lock contention)
    must not write the throttle stamp — otherwise the user is silently
    locked out of updates for an hour after every hiccup."""
    monkeypatch.delenv("CLAWJOURNAL_NO_AUTO_UPDATE", raising=False)

    # Make _git_status_ok report "git failed" (None) without dirtying tree.
    monkeypatch.setattr(
        "clawjournal.selfupdate._git_status_ok",
        lambda repo: (None, "git-status-failed"),
    )
    spawned: list[Path] = []
    _stub_spawn_ok(monkeypatch, spawned)

    result = selfupdate.maybe_self_update()
    assert result == "skip-git-status-failed"
    assert spawned == []
    assert not (isolated_config_dir / "last_update_check").exists()


# ---------- state checks -----------------------------------------------------


def test_dirty_tree_skips(monkeypatch, isolated_config_dir, fake_repo):
    monkeypatch.delenv("CLAWJOURNAL_NO_AUTO_UPDATE", raising=False)
    (fake_repo / "README").write_text("dirty\n")

    spawned: list[Path] = []
    _stub_spawn_ok(monkeypatch, spawned)
    assert selfupdate.maybe_self_update() == "dirty"
    assert spawned == []


def test_non_main_branch_skips(monkeypatch, isolated_config_dir, fake_repo):
    monkeypatch.delenv("CLAWJOURNAL_NO_AUTO_UPDATE", raising=False)
    subprocess.run(
        ["git", "-C", str(fake_repo), "checkout", "--quiet", "-b", "feature"],
        check=True,
    )

    spawned: list[Path] = []
    _stub_spawn_ok(monkeypatch, spawned)
    assert selfupdate.maybe_self_update() == "branch-feature"
    assert spawned == []


def test_spawn_path_fires_for_clean_main_checkout(
    monkeypatch, isolated_config_dir, fake_repo
):
    monkeypatch.delenv("CLAWJOURNAL_NO_AUTO_UPDATE", raising=False)
    spawned: list[Path] = []
    _stub_spawn_ok(monkeypatch, spawned)
    assert selfupdate.maybe_self_update() == "spawned"
    assert spawned == [fake_repo]


# ---------- concurrent invocations -------------------------------------------


def test_concurrent_invocation_is_excluded_by_lock(
    monkeypatch, isolated_config_dir, fake_repo
):
    """If a parallel CLI invocation already holds the lock, this one
    must bail out as throttled and NOT spawn a second update."""
    monkeypatch.delenv("CLAWJOURNAL_NO_AUTO_UPDATE", raising=False)
    lock = isolated_config_dir / "last_update_check.lock"
    # Simulate a peer that just claimed the slot.
    lock.write_bytes(b"")

    spawned: list[Path] = []
    _stub_spawn_ok(monkeypatch, spawned)
    assert selfupdate.maybe_self_update() == "throttled"
    assert spawned == []


def test_stale_lock_is_reclaimed(monkeypatch, isolated_config_dir, fake_repo):
    """A lock older than LOCK_STALE_SECONDS came from a crashed CLI —
    the next invocation must adopt it instead of being permanently
    stuck behind a dead peer."""
    monkeypatch.delenv("CLAWJOURNAL_NO_AUTO_UPDATE", raising=False)
    lock = isolated_config_dir / "last_update_check.lock"
    lock.write_bytes(b"")
    very_old = time.time() - selfupdate.LOCK_STALE_SECONDS * 10
    os.utime(lock, (very_old, very_old))

    spawned: list[Path] = []
    _stub_spawn_ok(monkeypatch, spawned)
    assert selfupdate.maybe_self_update() == "spawned"
    assert spawned == [fake_repo]


def test_lock_younger_than_stale_window_blocks_reclaim(
    monkeypatch, isolated_config_dir, fake_repo
):
    """A lock that's older than nothing (a real peer mid-spawn) must
    block reclaim even though it's much younger than THROTTLE_SECONDS.
    Confirms LOCK_STALE_SECONDS is the cutoff, not THROTTLE_SECONDS."""
    monkeypatch.delenv("CLAWJOURNAL_NO_AUTO_UPDATE", raising=False)
    lock = isolated_config_dir / "last_update_check.lock"
    lock.write_bytes(b"")
    # 10 seconds old — peer just started a spawn. Well within
    # LOCK_STALE_SECONDS (60s) but way under THROTTLE_SECONDS (3600s).
    recent = time.time() - 10
    os.utime(lock, (recent, recent))

    spawned: list[Path] = []
    _stub_spawn_ok(monkeypatch, spawned)
    assert selfupdate.maybe_self_update() == "throttled"
    assert spawned == []


def test_stale_lock_reclaim_only_one_winner_under_contention(
    monkeypatch, isolated_config_dir, fake_repo
):
    """Two threads simultaneously reclaim a stale lock — exactly one
    must win. The os.link atomicity is what makes this safe; an
    unlink-then-O_EXCL implementation would have let both proceed."""
    import threading

    monkeypatch.delenv("CLAWJOURNAL_NO_AUTO_UPDATE", raising=False)
    lock = isolated_config_dir / "last_update_check.lock"
    lock.write_bytes(b"")
    very_old = time.time() - selfupdate.LOCK_STALE_SECONDS * 10
    os.utime(lock, (very_old, very_old))

    spawned: list[Path] = []
    spawn_lock = threading.Lock()

    def fake_spawn(repo: Path) -> bool:
        with spawn_lock:
            spawned.append(repo)
        # Hold the slot briefly so racers genuinely overlap.
        time.sleep(0.05)
        return True
    monkeypatch.setattr("clawjournal.selfupdate._spawn_background_update", fake_spawn)

    barrier = threading.Barrier(8)
    results: list[str] = []
    results_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        r = selfupdate.maybe_self_update()
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one thread should have spawned; the rest must have
    # bailed out as throttled.
    assert results.count("spawned") == 1, results
    assert len(spawned) == 1


# ---------- spawn safety (no shell injection / path quoting) -----------------


def test_spawn_handles_repo_path_with_spaces(monkeypatch, tmp_path, isolated_config_dir):
    """A clone path like '~/code/foo bar/clawjournal' must not break the
    fetch — historically shell=True with f-string interpolation would
    have split this into separate args."""
    repo = tmp_path / "weird path with spaces" / "repo"
    _init_repo(repo)
    monkeypatch.setattr("clawjournal.selfupdate._package_repo_root", lambda: repo)
    monkeypatch.delenv("CLAWJOURNAL_NO_AUTO_UPDATE", raising=False)

    # Don't stub — actually invoke the real spawner. There's no remote
    # configured so `git fetch` will fail in the background, but the
    # important thing is that Popen accepts the command without raising
    # and that maybe_self_update returns "spawned" not "spawn-failed".
    result = selfupdate.maybe_self_update()
    assert result == "spawned"


def test_spawn_handles_repo_path_with_shell_metachars(
    monkeypatch, tmp_path, isolated_config_dir
):
    """Repo paths containing shell metacharacters (`&`, `$`, `'`) would
    break a shell=True spawn even with quoting. The python -c child
    layer makes these safe by passing the path as argv."""
    repo = tmp_path / "weird & path's $name" / "repo"
    _init_repo(repo)
    monkeypatch.setattr("clawjournal.selfupdate._package_repo_root", lambda: repo)
    monkeypatch.delenv("CLAWJOURNAL_NO_AUTO_UPDATE", raising=False)

    result = selfupdate.maybe_self_update()
    assert result == "spawned"


def test_background_update_skips_local_ahead_commit(
    monkeypatch, isolated_config_dir, fake_repo, tmp_path
):
    """The silent child must not update over clean local commits."""
    _wire_remote(fake_repo, tmp_path)
    local_sha = _commit_file(fake_repo, "LOCAL", "local\n", "local ahead")

    result = selfupdate._run_background_update(fake_repo)

    assert result == 0
    assert _head(fake_repo) == local_sha
    assert (fake_repo / "LOCAL").exists()


# ---------- selfupdate_sync --------------------------------------------------


def _wire_remote(repo: Path, tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    # `-b main` is required: without it the bare remote's HEAD defaults
    # to whatever the system's init.defaultBranch is (often `master`),
    # which then causes downstream `git clone` to leave the work tree
    # detached and breaks `git push origin main`.
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "push", "--quiet", "origin", "main"],
        check=True,
    )
    return remote


def test_selfupdate_sync_reports_up_to_date(
    monkeypatch, isolated_config_dir, fake_repo, tmp_path
):
    _wire_remote(fake_repo, tmp_path)
    result = selfupdate.selfupdate_sync(repo=fake_repo, check_only=True)
    assert result["status"] == "up-to-date"


def test_selfupdate_sync_detects_behind(
    monkeypatch, isolated_config_dir, fake_repo, tmp_path
):
    """If the remote has a newer commit, check_only reports 'behind'."""
    remote = _wire_remote(fake_repo, tmp_path)

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "--quiet", str(remote), str(other)], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.name", "t"], check=True)
    (other / "NEW").write_text("new\n")
    subprocess.run(["git", "-C", str(other), "add", "NEW"], check=True)
    subprocess.run(
        ["git", "-C", str(other), "commit", "--quiet", "-m", "upstream advance"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(other), "push", "--quiet", "origin", "main"], check=True
    )

    result = selfupdate.selfupdate_sync(repo=fake_repo, check_only=True)
    assert result["status"] == "behind"
    assert result["head"] != result["upstream"]


def test_selfupdate_sync_applies_fast_forward(
    monkeypatch, isolated_config_dir, fake_repo, tmp_path
):
    remote = _wire_remote(fake_repo, tmp_path)

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "--quiet", str(remote), str(other)], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.name", "t"], check=True)
    remote_sha = _commit_file(other, "NEW", "new\n", "upstream advance")
    subprocess.run(
        ["git", "-C", str(other), "push", "--quiet", "origin", "main"], check=True
    )

    result = selfupdate.selfupdate_sync(repo=fake_repo)

    assert result["status"] == "updated"
    assert _head(fake_repo) == remote_sha
    assert (fake_repo / "NEW").exists()


def test_selfupdate_sync_skips_local_ahead_without_resetting(
    monkeypatch, isolated_config_dir, fake_repo, tmp_path
):
    _wire_remote(fake_repo, tmp_path)
    local_sha = _commit_file(fake_repo, "LOCAL", "local\n", "local ahead")

    result = selfupdate.selfupdate_sync(repo=fake_repo)

    assert result["status"] == "ahead"
    assert _head(fake_repo) == local_sha
    assert (fake_repo / "LOCAL").exists()


def test_selfupdate_sync_skips_diverged_without_resetting(
    monkeypatch, isolated_config_dir, fake_repo, tmp_path
):
    remote = _wire_remote(fake_repo, tmp_path)

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "--quiet", str(remote), str(other)], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.name", "t"], check=True)
    _commit_file(other, "REMOTE", "remote\n", "remote advance")
    subprocess.run(
        ["git", "-C", str(other), "push", "--quiet", "origin", "main"], check=True
    )

    local_sha = _commit_file(fake_repo, "LOCAL", "local\n", "local ahead")

    result = selfupdate.selfupdate_sync(repo=fake_repo)

    assert result["status"] == "diverged"
    assert _head(fake_repo) == local_sha
    assert (fake_repo / "LOCAL").exists()
    assert not (fake_repo / "REMOTE").exists()


def test_selfupdate_sync_force_does_not_override_branch_guard(
    monkeypatch, isolated_config_dir, fake_repo, tmp_path
):
    """--force overrides the dirty-tree check but MUST NOT silently
    rewrite a feature branch to origin/main — that would discard the
    user's commits without warning."""
    _wire_remote(fake_repo, tmp_path)
    subprocess.run(
        ["git", "-C", str(fake_repo), "checkout", "--quiet", "-b", "feature"],
        check=True,
    )
    (fake_repo / "FEATURE").write_text("wip\n")
    subprocess.run(["git", "-C", str(fake_repo), "add", "FEATURE"], check=True)
    subprocess.run(
        ["git", "-C", str(fake_repo), "commit", "--quiet", "-m", "feature wip"],
        check=True,
    )
    feature_sha = subprocess.run(
        ["git", "-C", str(fake_repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    result = selfupdate.selfupdate_sync(repo=fake_repo, force=True)
    assert result["status"] == "branch-feature"

    # Confirm the feature commit is still there — nothing was reset.
    after = subprocess.run(
        ["git", "-C", str(fake_repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert after == feature_sha


def test_selfupdate_sync_force_overrides_dirty(
    monkeypatch, isolated_config_dir, fake_repo, tmp_path
):
    """--force on main with a dirty tree should proceed (and discard
    the WIP) — the user explicitly asked for it."""
    _wire_remote(fake_repo, tmp_path)
    (fake_repo / "README").write_text("uncommitted change\n")

    result = selfupdate.selfupdate_sync(repo=fake_repo, force=True, check_only=True)
    # Either up-to-date or behind — the key assertion is that we got
    # past the dirty guard.
    assert result["status"] in {"up-to-date", "behind"}


def test_cli_selfupdate_command_skips_pre_parse_auto_update():
    from clawjournal.cli import _should_auto_update

    assert _should_auto_update(["clawjournal", "selfupdate", "--check"]) is False
    assert _should_auto_update(["clawjournal", "status"]) is True


def test_cli_should_auto_update_ignores_selfupdate_in_arg_values():
    """The subcommand sits at the first non-flag position. Matching
    'selfupdate' anywhere in argv used to suppress auto-update for
    legitimate commands like `export --output ./selfupdate` or notes
    that contain the word."""
    from clawjournal.cli import _should_auto_update

    # 'selfupdate' as an option value, not the subcommand.
    assert _should_auto_update(
        ["clawjournal", "export", "--output", "./selfupdate"]
    ) is True
    # 'selfupdate' as part of a positional that comes after the real
    # subcommand.
    assert _should_auto_update(
        ["clawjournal", "note", "add", "ran selfupdate"]
    ) is True
    # No args at all -> default export path -> auto-update fires.
    assert _should_auto_update(["clawjournal"]) is True


def test_cli_should_auto_update_skips_help_and_version():
    """argparse prints and exits in milliseconds for -h/--help/--version;
    a background fetch is wasted work."""
    from clawjournal.cli import _should_auto_update

    assert _should_auto_update(["clawjournal", "-h"]) is False
    assert _should_auto_update(["clawjournal", "--help"]) is False
    assert _should_auto_update(["clawjournal", "--version"]) is False
    # But -h paired with a real subcommand still updates — the user is
    # asking for subcommand help and may proceed to run it.
    assert _should_auto_update(["clawjournal", "scan", "--help"]) is True


@pytest.mark.parametrize("json_mode", [False, True])
def test_cli_selfupdate_failure_exits_nonzero(
    monkeypatch, capsys, json_mode
):
    """Automation must not continue after either update stage failed."""
    from clawjournal import cli

    monkeypatch.setattr(
        selfupdate,
        "selfupdate_sync",
        lambda **kwargs: {"status": "fetch-failed", "stderr": "offline"},
    )
    monkeypatch.setattr(
        selfupdate,
        "reinstall",
        lambda **kwargs: {"status": "installer-failed", "stderr": "pip failed"},
    )
    argv = ["clawjournal", "selfupdate", "--reinstall"]
    if json_mode:
        argv.append("--json")
    monkeypatch.setattr("sys.argv", argv)

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    output = capsys.readouterr()
    assert "fetch-failed" in output.out or "Fetch failed" in output.out
    assert "installer-failed" in output.out or "Reinstall failed" in output.out


def test_cli_explicit_reinstall_runs_even_when_checkout_is_current(
    monkeypatch, capsys
):
    """Legacy installs have no pending record, so explicit means explicit."""
    from clawjournal import cli

    monkeypatch.setattr(
        selfupdate,
        "selfupdate_sync",
        lambda **kwargs: {
            "status": "up-to-date",
            "head": "a" * 40,
            "upstream": "a" * 40,
        },
    )
    calls = []
    monkeypatch.setattr(
        selfupdate,
        "reinstall",
        lambda **kwargs: calls.append(kwargs) or {"status": "reinstalled"},
    )
    monkeypatch.setattr(
        "sys.argv", ["clawjournal", "selfupdate", "--reinstall", "--json"]
    )

    cli.main()

    assert calls == [{"capture": True}]
    assert '"status": "reinstalled"' in capsys.readouterr().out


# ---------- pending reinstall -------------------------------------------------


def _make_installer(repo: Path, *, exit_code: int = 0) -> Path:
    """Write a stand-in scripts/install.sh that records that it ran."""
    scripts = repo / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    script = scripts / "install.sh"
    marker = repo / "installer-ran"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" > "{marker}"\n'
        f"exit {exit_code}\n"
    )
    return marker


def test_classify_reinstall_reasons_maps_paths_to_triggers():
    assert selfupdate.classify_reinstall_reasons(["pyproject.toml"]) == ["deps"]
    assert selfupdate.classify_reinstall_reasons(
        ["clawjournal/web/frontend/src/App.tsx"]
    ) == ["frontend"]
    assert selfupdate.classify_reinstall_reasons(
        ["clawjournal/redaction/trufflehog_install.py"]
    ) == ["scanners"]
    # Ordinary Python changes ride along with the editable install for free.
    assert selfupdate.classify_reinstall_reasons(["clawjournal/cli.py"]) == []
    assert selfupdate.classify_reinstall_reasons([]) == []


def test_classify_reinstall_reasons_reports_every_trigger_hit():
    reasons = selfupdate.classify_reinstall_reasons(
        [
            "pyproject.toml",
            "clawjournal/web/frontend/package.json",
            "clawjournal/redaction/betterleaks_install.py",
            "README.md",
        ]
    )
    assert reasons == ["deps", "frontend", "scanners"]


def test_no_pending_record_when_update_touches_only_python(
    isolated_config_dir, fake_repo, tmp_path
):
    """An editable install already picks up plain .py changes."""
    remote = _wire_remote(fake_repo, tmp_path)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "--quiet", str(remote), str(other)], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.name", "t"], check=True)
    _commit_file(other, "clawjournal/cli.py", "x = 1\n", "tweak cli")
    subprocess.run(
        ["git", "-C", str(other), "push", "--quiet", "origin", "main"], check=True
    )

    result = selfupdate.selfupdate_sync(repo=fake_repo)

    assert result["status"] == "updated"
    assert result["reinstall"] == []
    assert selfupdate.read_pending_reinstall() is None
    assert selfupdate.pending_reinstall_notice() is None


def test_dependency_change_records_pending_reinstall(
    isolated_config_dir, fake_repo, tmp_path
):
    remote = _wire_remote(fake_repo, tmp_path)
    before = _head(fake_repo)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "--quiet", str(remote), str(other)], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.name", "t"], check=True)
    after = _commit_file(other, "pyproject.toml", "[project]\n", "add a dependency")
    subprocess.run(
        ["git", "-C", str(other), "push", "--quiet", "origin", "main"], check=True
    )

    result = selfupdate.selfupdate_sync(repo=fake_repo)

    assert result["status"] == "updated"
    assert result["reinstall"] == ["deps"]
    record = selfupdate.read_pending_reinstall()
    assert record["from"] == before
    assert record["to"] == after
    notice = selfupdate.pending_reinstall_notice()
    assert "Python dependencies changed" in notice
    assert "selfupdate --reinstall" in notice


def _push_upstream_change(remote: Path, tmp_path: Path, name: str) -> None:
    """Land a single-file commit on the shared bare remote."""
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "--quiet", str(remote), str(other)], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(other), "config", "user.name", "t"], check=True)
    _commit_file(other, name, "x\n", f"change {name}")
    subprocess.run(
        ["git", "-C", str(other), "push", "--quiet", "origin", "main"], check=True
    )


def _build_fake_dist(repo: Path) -> Path:
    dist = repo / "clawjournal" / "web" / "frontend" / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    html = dist / "index.html"
    html.write_text("<html></html>")
    return html


def test_background_update_auto_reinstalls_when_needed(
    monkeypatch, isolated_config_dir, fake_repo, tmp_path
):
    """After a background fast-forward, the child finishes the job itself."""
    remote = _wire_remote(fake_repo, tmp_path)
    _build_fake_dist(fake_repo)
    _push_upstream_change(remote, tmp_path, "clawjournal/web/frontend/src/App.tsx")

    calls = []
    monkeypatch.setattr(
        selfupdate, "reinstall",
        lambda repo, **kw: calls.append(repo) or {"status": "installer-failed"},
    )

    assert selfupdate._run_background_update(fake_repo) == 0

    assert calls == [fake_repo]
    # The stubbed reinstall failed, so the record survives as the fallback
    # and the foreground CLI will show the fix-it notice.
    record = selfupdate.read_pending_reinstall()
    assert record["reasons"] == ["frontend"]
    assert "workbench build is stale" in selfupdate.pending_reinstall_notice()


def test_python_only_background_update_skips_the_reinstall(
    monkeypatch, isolated_config_dir, fake_repo, tmp_path
):
    """Plain .py changes ride along with the editable install for free."""
    remote = _wire_remote(fake_repo, tmp_path)
    _build_fake_dist(fake_repo)
    _push_upstream_change(remote, tmp_path, "clawjournal/cli.py")

    calls = []
    monkeypatch.setattr(
        selfupdate, "reinstall",
        lambda repo, **kw: calls.append(repo) or {"status": "reinstalled"},
    )

    assert selfupdate._run_background_update(fake_repo) == 0
    assert calls == []
    assert selfupdate.read_pending_reinstall() is None


def test_frontend_change_is_moot_on_cli_only_machines(
    monkeypatch, isolated_config_dir, fake_repo, tmp_path
):
    """No built workbench -> a frontend-only update needs no reinstall."""
    remote = _wire_remote(fake_repo, tmp_path)
    _push_upstream_change(remote, tmp_path, "clawjournal/web/frontend/src/App.tsx")

    calls = []
    monkeypatch.setattr(
        selfupdate, "reinstall",
        lambda repo, **kw: calls.append(repo) or {"status": "reinstalled"},
    )

    assert selfupdate._run_background_update(fake_repo) == 0
    assert calls == []
    assert selfupdate.read_pending_reinstall() is None


def test_scanner_pin_change_is_moot_without_managed_scanners(
    isolated_config_dir, fake_repo, tmp_path
):
    remote = _wire_remote(fake_repo, tmp_path)
    _push_upstream_change(
        remote, tmp_path, "clawjournal/redaction/trufflehog_install.py"
    )

    result = selfupdate.selfupdate_sync(repo=fake_repo)

    assert result["status"] == "updated"
    assert result["reinstall"] == []
    assert selfupdate.read_pending_reinstall() is None


def test_scanner_pin_change_records_with_managed_scanners(
    isolated_config_dir, fake_repo, tmp_path
):
    bin_dir = isolated_config_dir / "bin"
    bin_dir.mkdir()
    (bin_dir / "trufflehog").write_text("")
    remote = _wire_remote(fake_repo, tmp_path)
    _push_upstream_change(
        remote, tmp_path, "clawjournal/redaction/betterleaks_install.py"
    )

    result = selfupdate.selfupdate_sync(repo=fake_repo)

    assert result["status"] == "updated"
    assert result["reinstall"] == ["scanners"]


def test_undiffable_update_assumes_reinstall_is_needed(
    monkeypatch, isolated_config_dir, fake_repo
):
    """A spurious notice is cheaper than silently serving a stale build."""
    monkeypatch.setattr(
        "clawjournal.selfupdate._changed_paths", lambda *a, **k: None
    )
    reasons = selfupdate._record_reinstall_needs(fake_repo, "a" * 40, "b" * 40)
    assert reasons == ["unknown"]
    assert "could not be inspected" in selfupdate.pending_reinstall_notice()


def test_consecutive_updates_accumulate_reasons(isolated_config_dir):
    """Two pulls before one reinstall must not lose the first pull's needs."""
    selfupdate.record_pending_reinstall("a" * 40, "b" * 40, ["deps"])
    selfupdate.record_pending_reinstall("b" * 40, "c" * 40, ["frontend"])

    record = selfupdate.read_pending_reinstall()
    assert record["reasons"] == ["deps", "frontend"]
    # `from` stays pinned to the last revision that was actually installed.
    assert record["from"] == "a" * 40
    assert record["to"] == "c" * 40


def test_record_pending_reinstall_ignores_empty_reasons(isolated_config_dir):
    selfupdate.record_pending_reinstall("a" * 40, "b" * 40, [])
    assert selfupdate.read_pending_reinstall() is None


def test_clear_pending_reinstall_is_idempotent(isolated_config_dir):
    selfupdate.record_pending_reinstall("a" * 40, "b" * 40, ["deps"])
    selfupdate.clear_pending_reinstall()
    assert selfupdate.read_pending_reinstall() is None
    selfupdate.clear_pending_reinstall()  # no record left — must not raise


def test_corrupt_pending_record_is_ignored(isolated_config_dir):
    (isolated_config_dir / selfupdate.PENDING_REINSTALL_FILENAME).write_text("{not json")
    assert selfupdate.read_pending_reinstall() is None
    assert selfupdate.pending_reinstall_notice() is None


# ---------- reinstall ---------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer invocation")
def test_installer_command_matches_what_is_installed(
    isolated_config_dir, fake_repo
):
    _make_installer(fake_repo)

    # Nothing optional installed -> plain CLI reinstall.
    assert selfupdate._installer_command(fake_repo) == [
        "sh", str(fake_repo / "scripts" / "install.sh")
    ]

    # A built workbench must be rebuilt, or the reinstall reintroduces the
    # exact staleness this mechanism exists to prevent.
    dist = fake_repo / "clawjournal" / "web" / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>")
    assert "--with-frontend" in selfupdate._installer_command(fake_repo)

    bin_dir = isolated_config_dir / "bin"
    bin_dir.mkdir()
    (bin_dir / "trufflehog").write_text("")
    assert "--with-sharing" in selfupdate._installer_command(fake_repo)


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer invocation")
def test_installer_command_preserves_pending_optional_intent(
    isolated_config_dir, fake_repo
):
    """A failed attempt may remove the artifact that originally set the flag."""
    _make_installer(fake_repo)
    selfupdate.record_pending_reinstall(
        "a" * 40, "b" * 40, ["frontend", "scanners"]
    )

    command = selfupdate._installer_command(fake_repo)

    assert "--with-frontend" in command
    assert "--with-sharing" in command


def test_finalize_install_preserves_unrequested_optional_reasons(
    isolated_config_dir, fake_repo
):
    selfupdate.record_pending_reinstall(
        "a" * 40, "b" * 40, ["deps", "frontend", "scanners"]
    )

    result = selfupdate.finalize_install(repo=fake_repo)

    assert result["status"] == "finalized-partial"
    assert result["remaining"] == ["frontend", "scanners"]
    assert selfupdate.read_pending_reinstall()["reasons"] == [
        "frontend",
        "scanners",
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer invocation")
def test_reinstall_runs_installer_and_clears_notice(isolated_config_dir, fake_repo):
    marker = _make_installer(fake_repo)
    selfupdate.record_pending_reinstall("a" * 40, "b" * 40, ["deps"])

    result = selfupdate.reinstall(repo=fake_repo, capture=True)

    assert result["status"] == "reinstalled"
    assert marker.exists()
    assert selfupdate.read_pending_reinstall() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer invocation")
def test_failed_reinstall_keeps_the_notice(isolated_config_dir, fake_repo):
    _make_installer(fake_repo, exit_code=1)
    selfupdate.record_pending_reinstall("a" * 40, "b" * 40, ["deps"])

    result = selfupdate.reinstall(repo=fake_repo, capture=True)

    assert result["status"] == "installer-failed"
    # The install is still stale, so the user must still be told.
    assert selfupdate.read_pending_reinstall() is not None


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer invocation")
def test_reinstall_disables_auto_update_in_the_child(isolated_config_dir, fake_repo):
    """The installer shells out to the CLI it is installing."""
    scripts = fake_repo / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    marker = fake_repo / "env-seen"
    (scripts / "install.sh").write_text(
        "#!/bin/sh\n"
        f'echo "${{CLAWJOURNAL_NO_AUTO_UPDATE:-unset}}" > "{marker}"\n'
    )

    assert selfupdate.reinstall(repo=fake_repo, capture=True)["status"] == "reinstalled"
    assert marker.read_text().strip() == "1"


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer invocation")
def test_reinstall_partial_when_workbench_build_is_still_stale(
    isolated_config_dir, fake_repo
):
    """install.sh exits 0 even when the frontend build is skipped (no npm).

    Clearing the record on exit code alone would retire the fix-it notice
    exactly when it is still needed.
    """
    _make_installer(fake_repo)  # exits 0 without building anything
    html = _build_fake_dist(fake_repo)
    src = fake_repo / "clawjournal" / "web" / "frontend" / "src"
    src.mkdir(parents=True)
    newer = src / "App.tsx"
    newer.write_text("x")
    now = time.time()
    os.utime(html, (now - 100, now - 100))
    os.utime(newer, (now, now))
    selfupdate.record_pending_reinstall("a" * 40, "b" * 40, ["deps", "frontend"])

    result = selfupdate.reinstall(repo=fake_repo, capture=True)

    assert result["status"] == "reinstalled-partial"
    # deps were reconciled by the installer; only the stale build nags on.
    assert selfupdate.read_pending_reinstall()["reasons"] == ["frontend"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer invocation")
def test_reinstall_partial_when_failed_build_removes_dist(
    isolated_config_dir, fake_repo
):
    """Missing output is worse than stale output and must remain pending."""
    html = _build_fake_dist(fake_repo)
    src = fake_repo / "clawjournal" / "web" / "frontend" / "src"
    src.mkdir(parents=True)
    newer = src / "App.tsx"
    newer.write_text("x")
    now = time.time()
    os.utime(html, (now - 100, now - 100))
    os.utime(newer, (now, now))
    scripts = fake_repo / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "install.sh").write_text(
        "#!/bin/sh\n"
        f'rm -f "{html}"\n'
    )
    selfupdate.record_pending_reinstall(
        "a" * 40, "b" * 40, ["frontend"]
    )

    result = selfupdate.reinstall(repo=fake_repo, capture=True)

    assert result["status"] == "reinstalled-partial"
    assert result["remaining"] == ["frontend"]
    assert not html.exists()
    assert selfupdate.read_pending_reinstall()["reasons"] == ["frontend"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer invocation")
def test_reinstall_clears_when_workbench_build_is_current(
    isolated_config_dir, fake_repo
):
    _make_installer(fake_repo)
    html = _build_fake_dist(fake_repo)
    src = fake_repo / "clawjournal" / "web" / "frontend" / "src"
    src.mkdir(parents=True)
    older = src / "App.tsx"
    older.write_text("x")
    now = time.time()
    os.utime(older, (now - 100, now - 100))
    os.utime(html, (now, now))
    selfupdate.record_pending_reinstall("a" * 40, "b" * 40, ["frontend"])

    result = selfupdate.reinstall(repo=fake_repo, capture=True)

    assert result["status"] == "reinstalled"
    assert selfupdate.read_pending_reinstall() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer invocation")
def test_reinstall_refuses_to_race_another_installer(isolated_config_dir, fake_repo):
    """Concurrent pip/npm runs corrupt each other — one installer at a time."""
    import fcntl

    marker = _make_installer(fake_repo)
    lock = isolated_config_dir / selfupdate.REINSTALL_LOCK_FILENAME
    peer_fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(peer_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    try:
        result = selfupdate.reinstall(repo=fake_repo, capture=True)
    finally:
        fcntl.flock(peer_fd, fcntl.LOCK_UN)
        os.close(peer_fd)

    assert result["status"] == "reinstall-in-progress"
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer invocation")
def test_persistent_unlocked_reinstall_file_is_reused(isolated_config_dir, fake_repo):
    marker = _make_installer(fake_repo)
    lock = isolated_config_dir / selfupdate.REINSTALL_LOCK_FILENAME
    lock.write_text("")

    result = selfupdate.reinstall(repo=fake_repo, capture=True)

    assert result["status"] == "reinstalled"
    assert marker.exists()
    assert lock.exists()


def test_reinstall_lock_has_one_winner_under_contention(isolated_config_dir):
    """The persistent lock file must never admit two simultaneous owners."""
    barrier = threading.Barrier(16)
    claimed = threading.Barrier(16)
    results = []
    results_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        won = selfupdate._claim_reinstall_lock()
        with results_lock:
            results.append(won)
        # Keep the winner's OS lock held until every contender has tried.
        claimed.wait()
        if won:
            selfupdate._release_reinstall_lock()

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1
    assert results.count(False) == 15


def test_reinstall_without_installer_script_reports_cleanly(
    isolated_config_dir, fake_repo
):
    result = selfupdate.reinstall(repo=fake_repo, capture=True)
    assert result["status"] == "installer-missing"


def test_reinstall_outside_a_checkout_reports_cleanly(monkeypatch, isolated_config_dir):
    monkeypatch.setattr("clawjournal.selfupdate._package_repo_root", lambda: None)
    assert selfupdate.reinstall()["status"] == "not-a-checkout"


# ---------- reinstall_needed --------------------------------------------------


def test_reinstall_needed_false_when_nothing_pending(isolated_config_dir, fake_repo):
    assert selfupdate.reinstall_needed(repo=fake_repo) is False


def test_reinstall_needed_true_with_pending_record(isolated_config_dir, fake_repo):
    selfupdate.record_pending_reinstall("a" * 40, "b" * 40, ["deps"])
    assert selfupdate.reinstall_needed(repo=fake_repo) is True


def test_reinstall_needed_true_when_workbench_build_is_stale(
    isolated_config_dir, fake_repo
):
    html = _build_fake_dist(fake_repo)
    src = fake_repo / "clawjournal" / "web" / "frontend" / "src"
    src.mkdir(parents=True)
    newer = src / "App.tsx"
    newer.write_text("x")
    now = time.time()
    os.utime(html, (now - 100, now - 100))
    os.utime(newer, (now, now))
    assert selfupdate.reinstall_needed(repo=fake_repo) is True


def test_reinstall_needed_false_outside_a_checkout(monkeypatch, isolated_config_dir):
    monkeypatch.setattr("clawjournal.selfupdate._package_repo_root", lambda: None)
    assert selfupdate.reinstall_needed() is False
