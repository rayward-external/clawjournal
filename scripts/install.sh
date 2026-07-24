#!/usr/bin/env sh
# Install ClawJournal in editable mode into a venv.
#
# Works on macOS, Linux, WSL, and Git Bash on Windows. Idempotent: re-running
# upgrades the existing install. For native Windows PowerShell, use
# scripts/install.ps1 instead.
#
# Usage:
#   ./scripts/install.sh                # CLI install (recommended for first run)
#   ./scripts/install.sh --with-frontend  # also build the browser workbench
#   ./scripts/install.sh --desktop-shortcut # build workbench + add desktop launcher
#   ./scripts/install.sh --with-sharing   # also install the managed secret scanners
#   ./scripts/install.sh --desktop-shortcut --with-sharing
#   ./scripts/install.sh --help
#
# Environment:
#   CLAWJOURNAL_VENV  Path to the venv (default: ~/.clawjournal-venv)
#   CLAWJOURNAL_REPO  Where to clone the repo if running outside one
#                     (default: ~/clawjournal). Only used when piped via curl.

set -eu

WITH_FRONTEND=0
DESKTOP_SHORTCUT=0
WITH_SHARING=0
while [ $# -gt 0 ]; do
  case "$1" in
    --with-frontend) WITH_FRONTEND=1 ;;
    --desktop-shortcut) DESKTOP_SHORTCUT=1; WITH_FRONTEND=1 ;;
    --with-sharing) WITH_SHARING=1 ;;
    -h|--help)
      sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2 ;;
  esac
  shift
done

# Bring an existing checkout up to the latest published version before
# installing from it. Safe by construction: fast-forward only, and only on a
# clean `main` — anything else is left untouched with an explanation, and the
# install proceeds from the code that is already there.
SYNC_FROM=""
SYNC_TO=""
sync_checkout() {
  sc_repo="$1"
  [ -d "$sc_repo/.git" ] || return 0
  if [ -n "${CLAWJOURNAL_NO_AUTO_UPDATE:-}" ]; then
    # Set by `clawjournal selfupdate --reinstall`, which already synced.
    return 0
  fi
  sc_branch="$(git -C "$sc_repo" symbolic-ref --short -q HEAD 2>/dev/null || echo '?')"
  if [ "$sc_branch" != "main" ]; then
    echo "[i] Not updating the checkout: it is on branch '$sc_branch', not 'main'. Installing the current code."
    return 0
  fi
  if [ -n "$(git -C "$sc_repo" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
    echo "[i] Not updating the checkout: it has local changes (they are preserved). Installing the current code."
    return 0
  fi
  sc_before="$(git -C "$sc_repo" rev-parse HEAD 2>/dev/null || echo '')"
  if ! git -C "$sc_repo" fetch --quiet origin main 2>/dev/null; then
    echo "[i] Could not fetch the latest version (offline). Installing the current code."
    return 0
  fi
  sc_upstream="$(git -C "$sc_repo" rev-parse FETCH_HEAD 2>/dev/null || echo '')"
  if [ -z "$sc_before" ] || [ -z "$sc_upstream" ]; then
    echo "[i] Could not compare the checkout with the latest published version. Installing the current code."
    return 0
  fi
  if [ "$sc_before" = "$sc_upstream" ]; then
    SYNC_FROM="$sc_before"
    SYNC_TO="$sc_upstream"
    echo "[ok] Checkout is on the latest published version."
    return 0
  fi
  if git -C "$sc_repo" merge-base --is-ancestor "$sc_before" "$sc_upstream" 2>/dev/null; then
    if git -C "$sc_repo" merge --ff-only --quiet "$sc_upstream" 2>/dev/null; then
      SYNC_FROM="$sc_before"
      SYNC_TO="$sc_upstream"
      echo "[ok] Checkout is on the latest published version."
      return 0
    fi
    echo "[i] Could not apply the latest published version. Installing the current code."
    return 0
  fi
  if git -C "$sc_repo" merge-base --is-ancestor "$sc_upstream" "$sc_before" 2>/dev/null; then
    echo "[x] Not installing: this main checkout has unpublished local commits. Move them to a branch, then retry." >&2
  else
    echo "[x] Not installing: this main checkout has diverged from the published version. Reconcile it, then retry." >&2
  fi
  return 1
}

# Resolve the repo root. If the script is run from a checkout, REPO_DIR is the
# parent of scripts/. If piped via `curl | sh`, $0 is "sh" and we clone fresh.
SCRIPT_PATH="${0:-}"
REPO_DIR=""
if [ -n "$SCRIPT_PATH" ] && [ -f "$SCRIPT_PATH" ]; then
  REPO_DIR="$(cd "$(dirname "$SCRIPT_PATH")/.." 2>/dev/null && pwd)" || REPO_DIR=""
fi
if [ -z "$REPO_DIR" ] || [ ! -f "$REPO_DIR/pyproject.toml" ]; then
  TARGET="${CLAWJOURNAL_REPO:-$HOME/clawjournal}"
  if [ ! -d "$TARGET/.git" ]; then
    echo "-> Cloning ClawJournal to $TARGET"
    git clone --quiet https://github.com/rayward-external/clawjournal.git "$TARGET"
  fi
  REPO_DIR="$TARGET"
fi

VENV_DIR="${CLAWJOURNAL_VENV:-$HOME/.clawjournal-venv}"

# 1) Locate a Python 3.10+ interpreter.
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
      PYTHON="$candidate"
      break
    fi
  fi
done

if [ -z "$PYTHON" ]; then
  cat >&2 <<EOF
[x] Python 3.10+ not found on PATH.

  macOS:    brew install python
  Debian/Ubuntu:  sudo apt install -y python3 python3-venv python3-full
  Fedora/RHEL:    sudo dnf install -y python3 python3-virtualenv
  Windows:  https://python.org/downloads  (check "Add Python to PATH")
EOF
  exit 1
fi

# Direct installer invocations join the same advisory lock as automatic
# reinstalls. The Python parent marks recursive installer children so they do
# not try to acquire their already-held lock again.
if [ "${CLAWJOURNAL_INSTALL_LOCK_HELD:-}" != "1" ]; then
  set --
  if [ "$DESKTOP_SHORTCUT" -eq 1 ]; then
    set -- "$@" --desktop-shortcut
  elif [ "$WITH_FRONTEND" -eq 1 ]; then
    set -- "$@" --with-frontend
  fi
  if [ "$WITH_SHARING" -eq 1 ]; then
    set -- "$@" --with-sharing
  fi
  exec "$PYTHON" "$REPO_DIR/scripts/install_lock.py" -- \
    sh "$REPO_DIR/scripts/install.sh" "$@"
fi

echo "[ok] Python: $("$PYTHON" --version 2>&1) ($(command -v "$PYTHON"))"

# Capture stderr from venv creation in a per-run temp file. mktemp avoids the
# /tmp/<predictable-name> race (multi-user systems, symlink attacks).
ERR_LOG="$(mktemp 2>/dev/null || echo "/tmp/clawjournal-venv.$$.err")"
trap 'rm -f "$ERR_LOG"' EXIT INT TERM

if ! sync_checkout "$REPO_DIR"; then
  exit 1
fi

# 2) Create or reuse the venv. On Debian-likes, "python3 -m venv" fails when
#    python3-venv isn't installed — surface that clearly.
if [ ! -x "$VENV_DIR/bin/python" ] && [ ! -x "$VENV_DIR/Scripts/python.exe" ]; then
  echo "-> Creating venv at $VENV_DIR"
  if ! "$PYTHON" -m venv "$VENV_DIR" 2>"$ERR_LOG"; then
    cat "$ERR_LOG" >&2
    cat >&2 <<EOF

Hint: on Debian/Ubuntu the venv module is in a separate package:
  sudo apt install -y python3-venv python3-full
EOF
    exit 1
  fi
fi

if [ -x "$VENV_DIR/bin/python" ]; then
  VENV_PY="$VENV_DIR/bin/python"
  VENV_BIN="$VENV_DIR/bin"
else
  VENV_PY="$VENV_DIR/Scripts/python.exe"
  VENV_BIN="$VENV_DIR/Scripts"
fi

# Record anything the direct checkout sync changed before installation begins.
# If pip or an optional install later fails, the pending notice must survive.
if [ -n "$SYNC_FROM" ] && [ -n "$SYNC_TO" ]; then
  "$VENV_PY" -c 'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from clawjournal.selfupdate import record_install_sync; record_install_sync(Path(sys.argv[1]), sys.argv[2], sys.argv[3])' \
    "$REPO_DIR" "$SYNC_FROM" "$SYNC_TO" >/dev/null 2>&1 || true
fi

# 3) Install ClawJournal in editable mode.
echo "-> Installing ClawJournal (editable) from $REPO_DIR"
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet -e "$REPO_DIR"

# 4) Optional: build the browser workbench. Failures here are non-fatal — the
#    CLI install already succeeded; only the opt-in frontend is missing.
if [ "$WITH_FRONTEND" -eq 1 ]; then
  if ! command -v npm >/dev/null 2>&1; then
    cat >&2 <<EOF
[!] --with-frontend requested but npm not found. Skipping the workbench build.
  Install Node.js (https://nodejs.org), then re-run with --with-frontend.
EOF
  else
    echo "-> Building browser workbench"
    if ( cd "$REPO_DIR/clawjournal/web/frontend" && npm install --silent && npm run build --silent ); then
      # A revision stamp is required in addition to mtimes: deleted frontend
      # inputs leave no newer file behind for a staleness check to discover.
      "$VENV_PY" -c 'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from clawjournal.selfupdate import record_frontend_build; record_frontend_build(Path(sys.argv[1]))' \
        "$REPO_DIR" >/dev/null 2>&1 || true
    else
      echo "[!] Frontend build failed. The CLI is installed; re-run with --with-frontend after fixing the build." >&2
    fi
  fi
fi

# 5) Optional sharing dependencies. These are pinned, checksum-verified, and
# installed under ~/.clawjournal/bin without root access.
if [ "$WITH_SHARING" -eq 1 ]; then
  echo "-> Installing managed secret scanners"
  CLAWJOURNAL_NO_AUTO_UPDATE=1 "$VENV_BIN/clawjournal" betterleaks install
  CLAWJOURNAL_NO_AUTO_UPDATE=1 "$VENV_BIN/clawjournal" trufflehog install
fi

# 6) Optional desktop launcher. It uses the just-installed venv executable so
#    the shortcut remains independent of the user's PATH.
if [ "$DESKTOP_SHORTCUT" -eq 1 ]; then
  echo "-> Installing desktop shortcut"
  "$VENV_BIN/clawjournal" desktop install
fi

# 7) Retire only the pending reasons this run actually reconciled. Frontend
#    failures are non-fatal above, so the CLI verifies the built assets before
#    clearing that reason; unrequested optional components remain pending.
set -- --finalize-install
if [ "$WITH_FRONTEND" -eq 1 ]; then
  set -- "$@" --frontend-requested
fi
if [ "$WITH_SHARING" -eq 1 ]; then
  set -- "$@" --scanners-installed
fi
CLAWJOURNAL_NO_AUTO_UPDATE=1 "$VENV_BIN/clawjournal" selfupdate "$@" >/dev/null 2>&1 || true

echo
INSTALLED_VERSION="$("$VENV_PY" -c 'import clawjournal; print(clawjournal.__version__)' 2>/dev/null || echo "?")"
echo "[ok] ClawJournal $INSTALLED_VERSION installed."

cat <<EOF

Run:    $VENV_BIN/clawjournal scan
        $VENV_BIN/clawjournal serve

Or add the venv to your PATH:
        export PATH="$VENV_BIN:\$PATH"
EOF

# 8) Soft hints for optional runtime deps.
FE_DIST_HTML="$REPO_DIR/clawjournal/web/frontend/dist/index.html"
FE_SRC_DIR="$REPO_DIR/clawjournal/web/frontend/src"
if [ ! -f "$FE_DIST_HTML" ]; then
  cat <<EOF

[i] Browser workbench not built. To enable 'clawjournal serve':
    ./scripts/install.sh --with-frontend       (requires Node.js)
EOF
elif [ -d "$FE_SRC_DIR" ] && [ -n "$(find "$FE_SRC_DIR" -type f -newer "$FE_DIST_HTML" 2>/dev/null | head -n 1)" ]; then
  # Source is newer than the built assets — a sync without a rebuild leaves
  # 'clawjournal serve' showing a stale workbench (e.g. an empty Share queue).
  cat <<EOF

[i] The browser workbench build looks out of date (its source is newer than
    the built assets). 'clawjournal serve' may show an old UI until you rebuild:
    ./scripts/install.sh --with-frontend       (requires Node.js)
EOF
fi

if [ "$WITH_SHARING" -eq 0 ] && ! CLAWJOURNAL_NO_AUTO_UPDATE=1 "$VENV_BIN/clawjournal" betterleaks status --json >/dev/null 2>&1; then
  cat <<EOF

[i] Betterleaks is required when sharing exports. Install it with:
    $VENV_BIN/clawjournal betterleaks install      (pinned version, sha256-verified, no root needed)
    Or re-run: ./scripts/install.sh --with-sharing
EOF
fi

if [ "$WITH_SHARING" -eq 0 ] && ! CLAWJOURNAL_NO_AUTO_UPDATE=1 "$VENV_BIN/clawjournal" trufflehog status --json >/dev/null 2>&1; then
  cat <<EOF

[i] TruffleHog is required when sharing exports. Install it before 'bundle-export':
    $VENV_BIN/clawjournal trufflehog install      (pinned version, sha256-verified, no root needed)
    Or re-run: ./scripts/install.sh --with-sharing
EOF
fi
