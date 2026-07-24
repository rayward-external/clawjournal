"""Silent, throttled, background auto-update for the editable git checkout.

Inspired by gstack: every CLI invocation triggers a fast update check
(throttled to once an hour, network-failure-safe, completely silent).
The fast-forward runs in a detached subprocess so it can't slow down the
user; the editable install means the next invocation picks up new code.
When an update needs more than a pull (new dependencies, a stale
workbench build, bumped scanner pins), the detached child also reruns
the project's installer so the next invocation is fully aligned.

The check is a no-op when any of these is true:
  - opt-out env var ``CLAWJOURNAL_NO_AUTO_UPDATE`` is set to a truthy value
  - the install isn't an editable git checkout (e.g. PyPI wheel)
  - ``~/.clawjournal/`` doesn't exist yet (fresh install)
  - the checkout has uncommitted changes (don't clobber WIP)
  - the checkout is on a non-main branch (don't surprise contributors)
  - the checkout has local-only commits or diverged history
  - the throttle stamp is younger than the throttle window
  - another concurrent CLI invocation already claimed the throttle slot
"""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import CONFIG_DIR

STAMP_FILENAME = "last_update_check"
PENDING_REINSTALL_FILENAME = "pending_reinstall.json"
FRONTEND_BUILD_REVISION_FILENAME = ".clawjournal-build-revision"
THROTTLE_SECONDS = 60 * 60  # once per hour
FETCH_TIMEOUT_SECONDS = 8
APPLY_TIMEOUT_SECONDS = 5
DIFF_TIMEOUT_SECONDS = 5
# An npm install + Vite build on a cold cache is the long pole here.
REINSTALL_TIMEOUT_SECONDS = 15 * 60
REINSTALL_TERMINATE_GRACE_SECONDS = 5.0
REINSTALL_LOCK_FILENAME = "reinstall.lock"
INSTALL_LOCK_HELD_ENV = "CLAWJOURNAL_INSTALL_LOCK_HELD"
ACTIVE_PYTHON_ENV = "CLAWJOURNAL_ACTIVE_PYTHON"
DEFAULT_BRANCH = "main"
DEFAULT_REMOTE = "origin"

OPT_OUT_ENV = "CLAWJOURNAL_NO_AUTO_UPDATE"
DEBUG_ENV = "CLAWJOURNAL_AUTO_UPDATE_DEBUG"

_update_lock_guard = threading.Lock()
_update_lock_fd: int | None = None
_reinstall_lock_guard = threading.Lock()
_reinstall_lock_fd: int | None = None


def _truthy(value: str | None) -> bool:
    return bool(value) and value.strip().lower() not in {"", "0", "false", "no", "off"}


def _debug(msg: str) -> None:
    if _truthy(os.environ.get(DEBUG_ENV)):
        print(f"[selfupdate] {msg}", file=sys.stderr)


def _package_repo_root() -> Path | None:
    """Return the git checkout root that contains this package, or None."""
    here = Path(__file__).resolve().parent.parent
    if (here / ".git").exists():
        return here
    return None


def _stamp_path() -> Path:
    return CONFIG_DIR / STAMP_FILENAME


def _throttle_fresh(now: float, *, window: int = THROTTLE_SECONDS) -> bool:
    """Return True when the throttle stamp is younger than `window`.

    A negative delta (system clock jumped backward — VM resume, NTP
    correction) is treated as "not fresh" so the user isn't locked out
    of updates for hours while the clock catches up.
    """
    stamp = _stamp_path()
    try:
        mtime = stamp.stat().st_mtime
    except FileNotFoundError:
        return False
    except OSError:
        return False
    delta = now - mtime
    if delta < 0:
        return False
    return delta < window


def _acquire_advisory_lock(path: Path) -> int | None:
    """Return a locked fd, or None when another process owns the lock."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return None
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, OSError):
        try:
            os.close(fd)
        except OSError:
            pass
        return None
    return fd


def _release_advisory_lock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except (ImportError, OSError):
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _claim_throttle_slot(now: float) -> bool:
    """Claim the brief update-spawn slot with a crash-safe OS lock."""
    del now  # retained for the testable/public helper signature
    global _update_lock_fd

    with _update_lock_guard:
        if _update_lock_fd is not None:
            return False
        fd = _acquire_advisory_lock(_stamp_path().with_suffix(".lock"))
        if fd is None:
            return False
        _update_lock_fd = fd
        return True


def _write_stamp(now: float) -> None:
    """Refresh the throttle stamp's mtime. Called after state checks pass."""
    stamp = _stamp_path()
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch(exist_ok=True)
        os.utime(stamp, (now, now))
    except OSError as exc:
        _debug(f"could not write stamp: {exc}")


def _release_lock() -> None:
    global _update_lock_fd

    with _update_lock_guard:
        fd = _update_lock_fd
        _update_lock_fd = None
    if fd is not None:
        _release_advisory_lock(fd)


def _git(repo: Path, *args: str, timeout: float | None = None,
         capture: bool = False) -> subprocess.CompletedProcess[bytes] | None:
    """Run a git subcommand silently. Returns None on any error."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _debug(f"git {' '.join(args)} failed: {exc}")
        return None


def _git_status_ok(repo: Path) -> tuple[bool | None, str]:
    """Return (is_dirty, error_kind). is_dirty is None when git itself failed.

    Distinguishing "definitely clean" from "couldn't tell" is important:
    a transient `git status` failure (Windows AV, index lock contention)
    should NOT count as dirty and burn the throttle slot.
    """
    result = _git(repo, "status", "--porcelain", "--untracked-files=no",
                  timeout=2, capture=True)
    if result is None:
        return (None, "git-unavailable")
    if result.returncode != 0:
        return (None, "git-status-failed")
    return (bool(result.stdout.strip()), "")


def _current_branch(repo: Path) -> str | None:
    result = _git(repo, "rev-parse", "--abbrev-ref", "HEAD",
                  timeout=2, capture=True)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace").strip() or None


def _rev_parse(repo: Path, rev: str) -> str | None:
    result = _git(repo, "rev-parse", rev, timeout=2, capture=True)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.decode("ascii", "replace").strip() or None


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool | None:
    result = _git(repo, "merge-base", "--is-ancestor", ancestor, descendant,
                  timeout=2, capture=True)
    if result is None:
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def _history_relation(repo: Path, head: str, upstream: str) -> str:
    """Classify local HEAD relative to upstream."""
    if head == upstream:
        return "up-to-date"

    head_is_ancestor = _is_ancestor(repo, head, upstream)
    upstream_is_ancestor = _is_ancestor(repo, upstream, head)
    if head_is_ancestor is None or upstream_is_ancestor is None:
        return "ancestry-failed"
    if head_is_ancestor:
        return "behind"
    if upstream_is_ancestor:
        return "ahead"
    return "diverged"


# ---------- pending reinstall -------------------------------------------------
#
# A fast-forward only moves source files. The editable install picks up plain
# ``.py`` changes for free, but three kinds of change leave the *installed*
# tool inconsistent with the checkout until the installer is rerun:
#
#   deps      new/changed requirements are never pip-installed by a git pull
#   frontend  ``web/frontend/dist/`` is gitignored, so a pulled UI change does
#             not reach the built assets that ``clawjournal serve`` ships
#   scanners  a bumped PINNED_VERSION does not re-download the binary
#
# The background updater finishes the job itself: after a fast-forward that
# hits any trigger, it reruns the project's installer (quietly, at most once
# per update, never touching uncommitted work — the ff-only pull already
# guaranteed a clean main checkout). The pending record doubles as the
# fallback: when that reinstall can't complete — no npm, no network, a
# concurrent installer — the record survives and every foreground invocation
# prints a one-line fix-it notice until the install is reconciled.

_REINSTALL_TRIGGERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("deps", ("pyproject.toml",)),
    ("frontend", ("clawjournal/web/frontend/",)),
    (
        "scanners",
        (
            "clawjournal/redaction/betterleaks_install.py",
            "clawjournal/redaction/trufflehog_install.py",
        ),
    ),
)

_REINSTALL_REASON_TEXT = {
    "deps": "Python dependencies changed",
    "frontend": "the workbench build is stale",
    "scanners": "the pinned secret scanners changed",
    "unknown": "the update could not be inspected",
}


def _pending_reinstall_path() -> Path:
    return CONFIG_DIR / PENDING_REINSTALL_FILENAME


def _changed_paths(repo: Path, old_sha: str, new_sha: str) -> list[str] | None:
    """Repo-relative paths touched between two commits, or None if git failed."""
    result = _git(repo, "diff", "--name-only", f"{old_sha}..{new_sha}",
                  timeout=DIFF_TIMEOUT_SECONDS, capture=True)
    if result is None or result.returncode != 0:
        return None
    text = result.stdout.decode("utf-8", "replace")
    return [line.strip() for line in text.splitlines() if line.strip()]


def classify_reinstall_reasons(paths: list[str]) -> list[str]:
    """Which reinstall triggers the given changed paths hit, in table order."""
    reasons = []
    for reason, prefixes in _REINSTALL_TRIGGERS:
        if any(path.startswith(prefix) for path in paths for prefix in prefixes):
            reasons.append(reason)
    return reasons


def read_pending_reinstall() -> dict[str, object] | None:
    """Return the recorded pending-reinstall state, or None if there is none."""
    try:
        raw = _pending_reinstall_path().read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("reasons"):
        return None
    return data


def record_pending_reinstall(from_sha: str, to_sha: str, reasons: list[str]) -> None:
    """Record that the checkout has moved ahead of what is installed.

    Merges with any existing record: two background updates in a row must
    not lose the first one's reasons, and ``from`` stays pinned to the last
    revision that was actually installed. Written atomically because the
    detached background child writes this while a foreground CLI reads it.
    """
    if not reasons:
        return
    existing = read_pending_reinstall() or {}
    prior = existing.get("reasons")
    payload = {
        "from": existing.get("from") or from_sha,
        "to": to_sha,
        "reasons": sorted(set(reasons) | set(prior if isinstance(prior, list) else [])),
    }
    path = _pending_reinstall_path()
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        _debug(f"could not record pending reinstall: {exc}")
        try:
            tmp.unlink()
        except OSError:
            pass


def clear_pending_reinstall() -> None:
    try:
        _pending_reinstall_path().unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        _debug(f"could not clear pending reinstall: {exc}")


def _replace_pending_reinstall(
    record: dict[str, object], reasons: list[str]
) -> None:
    """Replace a pending record with a verified remainder, or remove it."""
    if not reasons:
        clear_pending_reinstall()
        return
    payload = {
        "from": record.get("from") or "",
        "to": record.get("to") or "",
        "reasons": sorted(set(reasons)),
    }
    path = _pending_reinstall_path()
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        _debug(f"could not narrow pending reinstall: {exc}")
        try:
            tmp.unlink()
        except OSError:
            pass


def _record_reinstall_needs(repo: Path, old_sha: str, new_sha: str) -> list[str]:
    """Classify an applied update and persist what it means. Returns reasons.

    Reasons that cannot matter on this machine are dropped: a stale
    workbench build is meaningless without a built workbench, and a bumped
    scanner pin without managed scanners simply means the new pin installs
    whenever the scanners are first installed. Recording them anyway would
    nag the user toward a reinstall that changes nothing they use.
    """
    paths = _changed_paths(repo, old_sha, new_sha)
    if paths is None:
        # Couldn't diff. Assume the worst: a spurious notice costs the user
        # one command, a missed one means silently serving a stale workbench.
        reasons = ["unknown"]
    else:
        reasons = classify_reinstall_reasons(paths)
        if not _frontend_is_built(repo):
            reasons = [r for r in reasons if r != "frontend"]
        if not _sharing_is_installed():
            reasons = [r for r in reasons if r != "scanners"]
    record_pending_reinstall(old_sha, new_sha, reasons)
    return reasons


def record_install_sync(repo: Path, old_sha: str, new_sha: str) -> list[str]:
    """Persist work introduced by a direct installer's checkout sync.

    Installers call this before pip or optional component work begins, so a
    later failure cannot hide dependency, frontend, or scanner drift.
    """
    return _record_reinstall_needs(repo, old_sha, new_sha)


def pending_reinstall_notice() -> str | None:
    """The short banner shown until the install is reconciled, or None."""
    record = read_pending_reinstall()
    if record is None:
        return None
    raw_reasons = record.get("reasons")
    reasons = raw_reasons if isinstance(raw_reasons, list) else []
    detail = "; ".join(_REINSTALL_REASON_TEXT.get(r, str(r)) for r in reasons)
    old = str(record.get("from") or "")[:7]
    new = str(record.get("to") or "")[:7]
    moved = f" {old} -> {new}" if old and new else ""
    return (
        f"[!] ClawJournal updated{moved} ({detail}).\n"
        f"    Finish with: clawjournal selfupdate --reinstall"
    )


# ---------- reinstall ---------------------------------------------------------


def _frontend_is_built(repo: Path) -> bool:
    return (repo / "clawjournal" / "web" / "frontend" / "dist" / "index.html").exists()


def _frontend_build_revision_path(repo: Path) -> Path:
    return (
        repo
        / "clawjournal"
        / "web"
        / "frontend"
        / "dist"
        / FRONTEND_BUILD_REVISION_FILENAME
    )


def record_frontend_build(repo: Path, revision: str | None = None) -> bool:
    """Stamp a successfully completed workbench build with its source revision.

    Source mtimes can show that a build is stale when a file changes, but they
    cannot show that a pulled commit deleted an input. Installers call this
    only after npm completes successfully, so pending frontend work is cleared
    only when the built output is known to cover the requested checkout.
    """
    built_revision = revision or _rev_parse(repo, "HEAD")
    if not built_revision:
        return False
    path = _frontend_build_revision_path(repo)
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(f"{built_revision}\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        _debug(f"could not record frontend build revision: {exc}")
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


def _frontend_build_covers(repo: Path, revision: str) -> bool:
    """Return whether the last successful build includes ``revision``."""
    try:
        built_revision = _frontend_build_revision_path(repo).read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return False
    if not built_revision:
        return False
    if built_revision == revision:
        return True
    # A later successful build also reconciles an older pending frontend
    # revision. Unknown or pruned revisions fail closed and retain the notice.
    return _is_ancestor(repo, revision, built_revision) is True


def _frontend_stale(repo: Path) -> bool:
    """True when the built workbench is older than its sources.

    Mirrors the `find -newer` staleness check in scripts/install.sh. Only
    meaningful when a build exists at all — callers gate on
    ``_frontend_is_built`` first.
    """
    frontend = repo / "clawjournal" / "web" / "frontend"
    try:
        built = (frontend / "dist" / "index.html").stat().st_mtime
    except OSError:
        return True
    skip = {"dist", "node_modules"}
    for root, dirs, files in os.walk(frontend):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            try:
                if os.path.getmtime(os.path.join(root, name)) > built:
                    return True
            except OSError:
                continue
    return False


def _sharing_is_installed() -> bool:
    bin_dir = CONFIG_DIR / "bin"
    return any(
        (bin_dir / name).exists()
        for name in ("betterleaks", "betterleaks.exe", "trufflehog", "trufflehog.exe")
    )


def _managed_scanners_installed() -> bool:
    """True only when both managed share scanners are present."""
    bin_dir = CONFIG_DIR / "bin"
    betterleaks = any(
        (bin_dir / name).exists()
        for name in ("betterleaks", "betterleaks.exe")
    )
    trufflehog = any(
        (bin_dir / name).exists()
        for name in ("trufflehog", "trufflehog.exe")
    )
    return betterleaks and trufflehog


def _checkout_covers_revision(repo: Path, revision: str) -> bool:
    """Return whether the installed checkout contains ``revision``."""
    head = _rev_parse(repo, "HEAD") or ""
    if not head or not revision:
        return False
    if head == revision:
        return True
    return _is_ancestor(repo, revision, head) is True


def _installer_command(
    repo: Path,
    *,
    with_frontend: bool = False,
    with_sharing: bool = False,
) -> list[str] | None:
    """Build the installer invocation that matches this machine's setup.

    Optional flags come from an explicit request, an existing installation,
    or a pending record from an earlier failed attempt. Rerunning without
    ``--with-frontend`` on a machine that has a built workbench would leave
    exactly the stale UI this mechanism exists to prevent.
    """
    if os.name == "nt":
        script = repo / "scripts" / "install.ps1"
        if not script.exists():
            return None
        cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)]
        frontend_flag, sharing_flag = "-WithFrontend", "-WithSharing"
    else:
        script = repo / "scripts" / "install.sh"
        if not script.exists():
            return None
        # Invoke via `sh` so a checkout without the exec bit still works.
        cmd = ["sh", str(script)]
        frontend_flag, sharing_flag = "--with-frontend", "--with-sharing"
    pending = read_pending_reinstall() or {}
    raw_reasons = pending.get("reasons")
    reasons = set(raw_reasons) if isinstance(raw_reasons, list) else set()
    # A failed build can remove dist/index.html. Preserve the intent recorded
    # before the attempt so the retry still asks the installer to rebuild it.
    if with_frontend or _frontend_is_built(repo) or "frontend" in reasons:
        cmd.append(frontend_flag)
    # Likewise, a failed scanner reinstall may leave only one managed binary.
    if with_sharing or _sharing_is_installed() or "scanners" in reasons:
        cmd.append(sharing_flag)
    return cmd


def finalize_install(
    repo: Path | None = None,
    *,
    frontend_requested: bool = False,
    scanners_installed: bool = False,
    clear_unknown: bool = False,
) -> dict[str, object]:
    """Retire only pending reasons that an installer verifiably reconciled.

    Both platform installers call this after first recording any checkout range
    they synchronized and then completing pip. Frontend failures are
    intentionally non-fatal in those scripts, so a requested workbench is
    cleared only when the built index exists and is current. Dependency,
    scanner, and unknown reasons are retired only when this checkout contains
    the pending target revision, so installing an older branch cannot erase
    work that still belongs to main.
    """
    target = repo or _package_repo_root()
    if target is None:
        return {"status": "not-a-checkout", "remaining": []}

    record = read_pending_reinstall() or {}
    raw_reasons = record.get("reasons")
    remaining = set(raw_reasons) if isinstance(raw_reasons, list) else set()
    current_revision = _rev_parse(target, "HEAD") or ""
    pending_revision = str(record.get("to") or "")
    required_pending_revision = pending_revision or current_revision
    checkout_current = (
        not remaining
        or _checkout_covers_revision(target, required_pending_revision)
    )
    if checkout_current:
        remaining.discard("deps")

    frontend_current = _frontend_is_built(target) and not _frontend_stale(target)
    if frontend_requested:
        required_revision = (
            required_pending_revision
            if "frontend" in remaining
            else current_revision
        )
        frontend_current = (
            frontend_current
            and bool(required_revision)
            and _frontend_build_covers(target, required_revision)
        )
        if frontend_current:
            remaining.discard("frontend")
        else:
            remaining.add("frontend")
    elif _frontend_is_built(target) and not frontend_current:
        # A direct installer may have self-synced frontend sources without
        # being asked to rebuild the already-installed workbench.
        remaining.add("frontend")

    scanners_current = _managed_scanners_installed()
    if scanners_installed:
        scanner_revision_current = "scanners" not in remaining or checkout_current
        if scanners_current and scanner_revision_current:
            remaining.discard("scanners")
        else:
            remaining.add("scanners")

    if clear_unknown and (
        checkout_current
        and (not frontend_requested or frontend_current)
        and (not scanners_installed or scanners_current)
    ):
        remaining.discard("unknown")

    if remaining and not pending_revision and current_revision:
        # A first explicit optional-install failure has no update range yet.
        # Pin its fallback record to the checkout that was actually attempted
        # so a later successful build/install can prove it reconciled the same
        # revision. The required_pending_revision fallback above also repairs
        # records written by older versions without this target.
        record = {
            **record,
            "from": record.get("from") or current_revision,
            "to": current_revision,
        }
    _replace_pending_reinstall(record, sorted(remaining))
    return {
        "status": "finalized" if not remaining else "finalized-partial",
        "remaining": sorted(remaining),
        "checkout_current": checkout_current,
        "frontend_current": frontend_current,
        "scanners_current": scanners_current,
    }


def reinstall_needed(repo: Path | None = None) -> bool:
    """True when the installed tool may not match the checkout.

    Lets `selfupdate --reinstall` be safely run unconditionally (the setup
    prompt tells participants' agents to do exactly that): when the pull
    found nothing and nothing is pending or stale, the minutes-long
    installer run is skipped entirely.
    """
    target = repo or _package_repo_root()
    if target is None:
        return False
    if read_pending_reinstall() is not None:
        return True
    return _frontend_is_built(target) and _frontend_stale(target)


def _reinstall_lock_path() -> Path:
    return CONFIG_DIR / REINSTALL_LOCK_FILENAME


def _claim_reinstall_lock() -> bool:
    """Claim a crash-safe OS lock for the installer.

    Advisory locks are released by the kernel when a process exits, so there
    is no stale-file deletion window in which two contenders can both win.
    The lock file itself intentionally persists between runs.
    """
    global _reinstall_lock_fd

    lock = _reinstall_lock_path()
    with _reinstall_lock_guard:
        if _reinstall_lock_fd is not None:
            return False
        fd = _acquire_advisory_lock(lock)
        if fd is None:
            return False

        _reinstall_lock_fd = fd
        return True


def _release_reinstall_lock() -> None:
    global _reinstall_lock_fd

    with _reinstall_lock_guard:
        fd = _reinstall_lock_fd
        _reinstall_lock_fd = None
    if fd is not None:
        _release_advisory_lock(fd)


def reinstall_in_progress() -> bool:
    """Return whether any process currently owns the install critical section."""
    with _reinstall_lock_guard:
        if _reinstall_lock_fd is not None:
            return True
        # Probe the same kernel lock used by automatic and direct installers. A
        # successful temporary claim proves the section is idle; release it
        # immediately without publishing it as this process's owned lock.
        fd = _acquire_advisory_lock(_reinstall_lock_path())
        if fd is None:
            return True
        _release_advisory_lock(fd)
        return False


def _terminate_installer_tree(
    process: subprocess.Popen[bytes],
    *,
    grace_seconds: float = REINSTALL_TERMINATE_GRACE_SECONDS,
) -> None:
    """Terminate every installer descendant and reap the group leader.

    The installer launches pip, npm, and scanner installers.  Killing only
    its shell on timeout would release our advisory lock while those children
    could still be writing the same environment.  POSIX installers therefore
    run in a fresh session and Windows installers in a new process group; this
    helper tears down that entire tree before ``reinstall()`` can release the
    lock.
    """
    if os.name == "posix":
        def session_members(session_id: int) -> list[tuple[int, str]] | None:
            """Return process metadata for the installer's POSIX session."""
            try:
                result = subprocess.run(
                    ["ps", "-Ao", "pid=,sid=,stat="],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                    check=False,
                    text=True,
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            if result.returncode != 0:
                return None
            members = []
            for line in result.stdout.splitlines():
                fields = line.split(None, 2)
                if len(fields) != 3:
                    continue
                try:
                    pid, sid = int(fields[0]), int(fields[1])
                except ValueError:
                    continue
                if sid == session_id:
                    members.append((pid, fields[2]))
            return members

        def session_alive(session_id: int, process_group: int) -> bool:
            # poll() reaps the leader so it cannot keep the session looking
            # active after every descendant has exited.
            process.poll()
            members = session_members(session_id)
            if members is not None:
                # Zombies have already released file descriptors and locks.
                return any(not state.startswith("Z") for _, state in members)
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                return False
            return True

        def signal_session(
            session_id: int,
            process_group: int,
            signum: signal.Signals,
        ) -> None:
            # Shells may place background jobs in their own process groups.
            # Signal the original group first, then every remaining member of
            # the fresh installer session so none escape cleanup.
            try:
                os.killpg(process_group, signum)
            except ProcessLookupError:
                pass
            for pid, state in session_members(session_id) or []:
                if state.startswith("Z"):
                    continue
                try:
                    os.kill(pid, signum)
                except ProcessLookupError:
                    pass

        def wait_for_session(
            session_id: int,
            process_group: int,
            seconds: float,
            *,
            repeat_signal: signal.Signals | None = None,
        ) -> bool:
            deadline = time.monotonic() + max(0.0, seconds)
            while session_alive(session_id, process_group):
                if time.monotonic() >= deadline:
                    return False
                if repeat_signal is not None:
                    # Catch a child forked between the previous session
                    # snapshot and delivery of the terminating signal.
                    signal_session(session_id, process_group, repeat_signal)
                time.sleep(0.05)
            return True

        try:
            process_group = os.getpgid(process.pid)
            session_id = os.getsid(process.pid)
        except ProcessLookupError:
            process.wait()
            return

        signal_session(session_id, process_group, signal.SIGTERM)
        if not wait_for_session(session_id, process_group, grace_seconds):
            signal_session(session_id, process_group, signal.SIGKILL)
            # Signals are asynchronous. Do not let the install lock go until
            # every non-zombie session member has dropped files and locks.
            wait_for_session(
                session_id,
                process_group,
                max(1.0, grace_seconds),
                repeat_signal=signal.SIGKILL,
            )
    else:
        # CREATE_NEW_PROCESS_GROUP gives taskkill a bounded tree rooted at the
        # PowerShell installer. /T includes pip/npm/scanner descendants.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(1.0, grace_seconds),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()

    try:
        process.wait(timeout=max(1.0, grace_seconds))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def reinstall(
    repo: Path | None = None,
    *,
    capture: bool = False,
    timeout: float = REINSTALL_TIMEOUT_SECONDS,
    with_frontend: bool = False,
    with_sharing: bool = False,
    _lock_held: bool = False,
) -> dict[str, object]:
    """Rerun the project's installer against the current checkout.

    Runs in the foreground for `selfupdate --reinstall` and in the detached
    background child right after an auto-applied update. Clears the
    pending-reinstall record only for what verifiably matches the checkout
    afterwards.
    """
    target = repo or _package_repo_root()
    if target is None:
        return {"status": "not-a-checkout", "repo": None}

    cmd = _installer_command(
        target,
        with_frontend=with_frontend,
        with_sharing=with_sharing,
    )
    if cmd is None:
        return {"status": "installer-missing", "repo": str(target)}

    info: dict[str, object] = {"repo": str(target), "command": " ".join(cmd)}
    frontend_requested = any(
        arg in {"--with-frontend", "-WithFrontend"} for arg in cmd
    )
    scanners_installed = any(
        arg in {"--with-sharing", "-WithSharing"} for arg in cmd
    )

    if not _lock_held and not _claim_reinstall_lock():
        info["status"] = "reinstall-in-progress"
        return info
    try:
        child_env = {**os.environ, OPT_OUT_ENV: "1"}
        # The platform installer uses the same advisory lock when launched
        # directly.  This parent already owns it, so mark the child to avoid a
        # recursive acquisition deadlock.
        child_env[INSTALL_LOCK_HELD_ENV] = "1"
        # Bootstrap through the interpreter that is running ClawJournal before
        # either installer probes PATH. This is required for absolute desktop
        # shortcuts and conda/custom environments whose Python is not exported.
        child_env[ACTIVE_PYTHON_ENV] = sys.executable
        base_prefix = getattr(sys, "base_prefix", sys.prefix)
        if sys.prefix != base_prefix or hasattr(sys, "real_prefix"):
            # Console entry points run under the environment in their shebang.
            # Preserve its root for compatibility with the managed-venv path;
            # ACTIVE_PYTHON supplies the exact executable used to bootstrap it.
            child_env["CLAWJOURNAL_VENV"] = sys.prefix
        else:
            # Editable installs are also supported directly in a user, conda
            # base, or system interpreter. Reinstall through that exact Python
            # instead of silently creating/updating ~/.clawjournal-venv.
            child_env.pop("CLAWJOURNAL_VENV", None)
        try:
            process_kwargs: dict[str, object] = {}
            if os.name == "posix":
                process_kwargs["start_new_session"] = True
            else:
                process_kwargs["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP
            process = subprocess.Popen(
                cmd,
                cwd=str(target),
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
                stdin=subprocess.DEVNULL,
                # The installer shells out to the CLI it is installing;
                # without this the child would fire its own auto-update
                # mid-install.
                env=child_env,
                **process_kwargs,
            )
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                _terminate_installer_tree(process)
                # Drain captured pipes after the whole process tree is gone.
                process.communicate()
                info["status"] = "installer-failed"
                info["stderr"] = str(exc)
                return info
        except OSError as exc:
            info["status"] = "installer-failed"
            info["stderr"] = str(exc)
            return info

        info["returncode"] = process.returncode
        if capture and stderr:
            info["stderr"] = stderr.decode("utf-8", "replace").strip()
        if process.returncode != 0:
            info["status"] = "installer-failed"
            return info

        # The installer reports success even when an optional workbench build
        # was skipped or failed. Finalize only the components that can be
        # verified, and keep every unresolved reason visible.
        finalized = finalize_install(
            target,
            frontend_requested=frontend_requested,
            scanners_installed=scanners_installed,
            clear_unknown=True,
        )
        remaining = finalized["remaining"]
        if remaining:
            info["status"] = "reinstalled-partial"
            info["remaining"] = remaining
            if "frontend" in remaining and shutil.which("npm") is None:
                info["hint"] = (
                    "Node.js (npm) was not found, so the workbench was not "
                    "rebuilt. Install Node.js, then run "
                    "`clawjournal selfupdate --reinstall` again.")
            elif "frontend" in remaining:
                info["hint"] = (
                    "The workbench build did not complete; run "
                    "`scripts/install.sh --with-frontend` to see the build "
                    "error.")
            elif "scanners" in remaining:
                info["hint"] = (
                    "The managed secret scanners are still incomplete; run "
                    "`scripts/install.sh --with-sharing` to see the error.")
            else:
                info["hint"] = (
                    "The update could not be fully verified; run the project "
                    "installer directly to inspect the remaining issue.")
            return info

        info["status"] = "reinstalled"
        return info
    finally:
        if not _lock_held:
            _release_reinstall_lock()


def _apply_fast_forward(
    repo: Path, upstream: str, *, force: bool
) -> subprocess.CompletedProcess[bytes] | None:
    """Apply a known-safe fast-forward.

    ``force`` only controls whether local uncommitted changes are discarded.
    Callers must already have proven that HEAD is an ancestor of upstream, so
    neither path can discard local commits.
    """
    if force:
        return _git(repo, "reset", "--hard", "--quiet", upstream,
                    timeout=APPLY_TIMEOUT_SECONDS, capture=True)
    return _git(repo, "merge", "--ff-only", "--quiet", upstream,
                timeout=APPLY_TIMEOUT_SECONDS, capture=True)


def _run_background_update(repo: Path) -> int:
    """Fetch and apply a silent fast-forward update if it is still safe.

    Wrapped in a top-level ``BaseException`` handler because the child's
    stdio is DEVNULL: an uncaught exception would crash silently, which
    is OK for correctness but means we lose the ability to record a
    silent failure status. Returning 1 keeps behavior consistent across
    expected and unexpected errors.
    """
    try:
        fetch = _git(repo, "fetch", "--quiet", DEFAULT_REMOTE, DEFAULT_BRANCH,
                     timeout=FETCH_TIMEOUT_SECONDS)
        if fetch is None or fetch.returncode != 0:
            return 1

        # Re-check safety in the child after the fetch. The parent may have
        # spawned us while the user continued working in the checkout.
        if _current_branch(repo) != DEFAULT_BRANCH:
            return 0

        is_dirty, _ = _git_status_ok(repo)
        if is_dirty is None or is_dirty:
            return 0

        upstream_ref = f"{DEFAULT_REMOTE}/{DEFAULT_BRANCH}"
        head_sha = _rev_parse(repo, "HEAD")
        upstream_sha = _rev_parse(repo, upstream_ref)
        if not head_sha or not upstream_sha:
            return 1

        if _history_relation(repo, head_sha, upstream_sha) != "behind":
            return 0

        # Coordinate the checkout transition and the install as one critical
        # section.  Otherwise a direct installer could finish against the old
        # revision while this child moves HEAD, then clear reasons it never
        # actually reconciled.
        if not _claim_reinstall_lock():
            return 0
        try:
            # A direct installer may have completed a sync while this child
            # waited for the lock, so prove the relation again before moving
            # anything.
            head_sha = _rev_parse(repo, "HEAD")
            upstream_sha = _rev_parse(repo, upstream_ref)
            if not head_sha or not upstream_sha:
                return 1
            if _history_relation(repo, head_sha, upstream_sha) != "behind":
                return 0

            update = _apply_fast_forward(repo, upstream_ref, force=False)
            if update is None:
                return 1
            if update.returncode == 0:
                # Note what the pulled commits imply for the *installed* tool,
                # then finish the job while we're already in the background:
                # rerun the installer so the participant's next invocation has
                # the new dependencies, workbench build, and scanner pins
                # without doing anything. If the reinstall can't complete, the
                # pending record survives and the foreground CLI shows the
                # one-line fix-it notice instead.
                reasons = _record_reinstall_needs(repo, head_sha, upstream_sha)
                if reasons:
                    reinstall(repo, capture=True, _lock_held=True)
            return update.returncode
        finally:
            _release_reinstall_lock()
    except BaseException as exc:  # noqa: BLE001 - silent child, see docstring
        _debug(f"background update crashed: {exc}")
        return 1


def _spawn_background_update(repo: Path) -> bool:
    """Spawn a detached child that runs a safe fast-forward, return immediately.

    Implementation: spawn `python -c "<inline script>"` rather than
    `sh -c` / `cmd /c`. The inline script uses ``subprocess.run`` with
    list-form argv, so repo paths with shell metacharacters
    (spaces, ``&``, ``|``, quotes, etc.) are safe — there's no shell
    in the loop at all.

    The child inherits no stdio. We use ``setsid`` (POSIX) /
    ``DETACHED_PROCESS`` (Windows) so it survives the parent CLI
    exiting. Returns False on spawn failure so the caller can decide
    whether to release the lock or leave it for the next throttle
    window.
    """
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "cwd": str(repo),
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS

    inline = (
        "import sys;"
        "from pathlib import Path;"
        "from clawjournal.selfupdate import _run_background_update;"
        "sys.exit(_run_background_update(Path(sys.argv[1])))"
    )

    try:
        # Bind the Popen object so it isn't garbage-collected before the
        # detached child is reaped — CPython would otherwise emit a
        # ResourceWarning under certain pytest configurations. The
        # detached session/process group means we never need to wait()
        # on it; the binding just suppresses the warning.
        _proc = subprocess.Popen(  # noqa: F841 - see comment above
            [sys.executable, "-c", inline, str(repo)], **kwargs
        )
        _debug(f"spawned background update (pid {_proc.pid})")
        return True
    except OSError as exc:
        _debug(f"could not spawn background update: {exc}")
        return False


def maybe_self_update(*, now: float | None = None) -> str:
    """Possibly fire a silent background update. Returns a short status.

    Status strings exist for tests and the manual `selfupdate --check`
    path; they are never surfaced to normal users.
    """
    if _truthy(os.environ.get(OPT_OUT_ENV)):
        return "opt-out"

    repo = _package_repo_root()
    if repo is None:
        return "not-a-checkout"

    # Pre-scan install: ~/.clawjournal/ doesn't exist yet. Don't
    # bootstrap it just to drop a throttle stamp — the user hasn't
    # run anything that needs updating against, and doing so makes
    # `clawjournal events doctor` think this is no longer a fresh
    # install. Wait until the user has actually used the tool.
    if not CONFIG_DIR.exists():
        return "no-config-dir"

    when = time.time() if now is None else now
    if _throttle_fresh(when):
        return "throttled"

    # Check repo state BEFORE writing the stamp, so a transient
    # `git status` failure doesn't burn a throttle slot.
    branch = _current_branch(repo)
    if branch != DEFAULT_BRANCH:
        return f"branch-{branch or 'unknown'}"

    is_dirty, err = _git_status_ok(repo)
    if is_dirty is None:
        # Transient git failure (index lock, AV scan, git missing) —
        # don't burn the throttle window; try again next invocation.
        return f"skip-{err}"
    if is_dirty:
        return "dirty"

    # Atomically claim the throttle slot. If two CLIs race here, only
    # one wins and spawns the background update; the other returns
    # "throttled" and exits cleanly.
    if not _claim_throttle_slot(when):
        return "throttled"

    # A racer can pass the pre-lock freshness check, wait while another
    # thread holds the lock, and then acquire it after that winner releases
    # it. Re-check under the lock so only one contender can spawn for a
    # freshly written stamp.
    post_claim_when = time.time() if now is None else now
    if _throttle_fresh(post_claim_when):
        _release_lock()
        return "throttled"

    _write_stamp(when)
    spawned = _spawn_background_update(repo)
    # Release the lock — the stamp's fresh mtime is what gates the next
    # invocation; the lock is only for the brief spawn window.
    _release_lock()
    return "spawned" if spawned else "spawn-failed"


def selfupdate_sync(
    repo: Path | None = None,
    *,
    check_only: bool = False,
    force: bool = False,
    _lock_held: bool = False,
) -> dict[str, object]:
    """Synchronous variant for the `clawjournal selfupdate` subcommand.

    Returns a small dict describing what happened. Surfaces errors as
    fields (no exceptions) so the CLI can render them deterministically.

    `--force` only overrides the dirty-tree guard (the user explicitly
    asked to discard local changes). It does NOT override the branch,
    local-commit, or diverged-history guards.
    """
    target = repo or _package_repo_root()
    if target is None:
        return {"status": "not-a-checkout", "repo": None}

    info: dict[str, object] = {"repo": str(target)}
    branch = _current_branch(target)
    info["branch"] = branch

    # Branch guard is non-negotiable even with --force: we update
    # `main` only. Checking out another branch and resetting it to
    # `origin/main` would silently discard the user's commits.
    if branch != DEFAULT_BRANCH:
        info["status"] = f"branch-{branch or 'unknown'}"
        return info

    if not force:
        is_dirty, err = _git_status_ok(target)
        if is_dirty is None:
            info["status"] = f"skip-{err}"
            return info
        if is_dirty:
            info["status"] = "dirty"
            return info

    fetch = _git(target, "fetch", "--quiet", DEFAULT_REMOTE, DEFAULT_BRANCH,
                 timeout=FETCH_TIMEOUT_SECONDS, capture=True)
    if fetch is None or fetch.returncode != 0:
        info["status"] = "fetch-failed"
        if fetch is not None:
            info["stderr"] = fetch.stderr.decode("utf-8", "replace").strip()
        return info

    upstream_ref = f"{DEFAULT_REMOTE}/{DEFAULT_BRANCH}"
    head_sha = _rev_parse(target, "HEAD") or ""
    upstream_sha = _rev_parse(target, upstream_ref) or ""
    info["head"] = head_sha
    info["upstream"] = upstream_sha

    if not head_sha or not upstream_sha:
        info["status"] = "rev-parse-failed"
        return info

    relation = _history_relation(target, head_sha, upstream_sha)
    info["relation"] = relation

    if relation == "up-to-date":
        info["status"] = "up-to-date"
        _write_stamp(time.time())
        return info

    if relation in {"ahead", "diverged", "ancestry-failed"}:
        info["status"] = relation
        return info

    if check_only:
        info["status"] = "behind"
        return info

    owns_lock = False
    if not _lock_held:
        if not _claim_reinstall_lock():
            info["status"] = "reinstall-in-progress"
            return info
        owns_lock = True
    try:
        # Fetch and preflight happen outside the lock for ordinary syncs. A
        # direct installer or background updater could finish between that
        # preflight and our claim, so repeat every mutable check while owning
        # the checkout/install critical section.
        branch = _current_branch(target)
        info["branch"] = branch
        if branch != DEFAULT_BRANCH:
            info["status"] = f"branch-{branch or 'unknown'}"
            return info
        if not force:
            is_dirty, err = _git_status_ok(target)
            if is_dirty is None:
                info["status"] = f"skip-{err}"
                return info
            if is_dirty:
                info["status"] = "dirty"
                return info

        head_sha = _rev_parse(target, "HEAD") or ""
        upstream_sha = _rev_parse(target, upstream_ref) or ""
        info["head"] = head_sha
        info["upstream"] = upstream_sha
        if not head_sha or not upstream_sha:
            info["status"] = "rev-parse-failed"
            return info

        relation = _history_relation(target, head_sha, upstream_sha)
        info["relation"] = relation
        if relation == "up-to-date":
            info["status"] = "up-to-date"
            _write_stamp(time.time())
            return info
        if relation in {"ahead", "diverged", "ancestry-failed"}:
            info["status"] = relation
            return info

        update = _apply_fast_forward(target, upstream_ref, force=force)
        if update is None or update.returncode != 0:
            info["status"] = "update-failed"
            if update is not None:
                info["stderr"] = update.stderr.decode("utf-8", "replace").strip()
            return info

        info["status"] = "updated"
        info["reinstall"] = _record_reinstall_needs(target, head_sha, upstream_sha)
        _write_stamp(time.time())
        return info
    finally:
        if owns_lock:
            _release_reinstall_lock()


def selfupdate_and_reinstall(
    repo: Path | None = None,
    *,
    force: bool = False,
    capture: bool = False,
    with_frontend: bool = False,
    with_sharing: bool = False,
) -> dict[str, object]:
    """Synchronize and reinstall under one checkout/install critical section."""
    target = repo or _package_repo_root()
    if target is None:
        result = selfupdate_sync(repo=target, force=force)
        result["reinstall_result"] = {
            "status": "skipped-update-blocked",
            "update_status": result.get("status"),
        }
        return result
    if not _claim_reinstall_lock():
        return {
            "repo": str(target),
            "status": "reinstall-in-progress",
            "reinstall_result": {"status": "reinstall-in-progress"},
        }
    try:
        result = selfupdate_sync(repo=target, force=force, _lock_held=True)
        if result.get("status") in {"updated", "up-to-date"}:
            result["reinstall_result"] = reinstall(
                repo=target,
                capture=capture,
                with_frontend=with_frontend,
                with_sharing=with_sharing,
                _lock_held=True,
            )
        else:
            result["reinstall_result"] = {
                "status": "skipped-update-blocked",
                "update_status": result.get("status"),
            }
        return result
    finally:
        _release_reinstall_lock()
