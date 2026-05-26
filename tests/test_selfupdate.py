"""Tests for the silent auto-update module."""

from __future__ import annotations

import os
import subprocess
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
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
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
