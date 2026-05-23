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
#   ./scripts/install.sh --help
#
# Environment:
#   CLAWJOURNAL_VENV  Path to the venv (default: ~/.clawjournal-venv)
#   CLAWJOURNAL_REPO  Where to clone the repo if running outside one
#                     (default: ~/clawjournal). Only used when piped via curl.

set -eu

# Capture stderr from venv creation in a per-run temp file. mktemp avoids the
# /tmp/<predictable-name> race (multi-user systems, symlink attacks).
ERR_LOG="$(mktemp 2>/dev/null || echo "/tmp/clawjournal-venv.$$.err")"
trap 'rm -f "$ERR_LOG"' EXIT INT TERM

WITH_FRONTEND=0
while [ $# -gt 0 ]; do
  case "$1" in
    --with-frontend) WITH_FRONTEND=1 ;;
    -h|--help)
      sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2 ;;
  esac
  shift
done

# Resolve the repo root. If the script is run from a checkout, REPO_DIR is the
# parent of scripts/. If piped via `curl | sh`, $0 is "sh" and we clone fresh.
SCRIPT_PATH="${0:-}"
REPO_DIR=""
if [ -n "$SCRIPT_PATH" ] && [ -f "$SCRIPT_PATH" ]; then
  REPO_DIR="$(cd "$(dirname "$SCRIPT_PATH")/.." 2>/dev/null && pwd)" || REPO_DIR=""
fi
if [ -z "$REPO_DIR" ] || [ ! -f "$REPO_DIR/pyproject.toml" ]; then
  TARGET="${CLAWJOURNAL_REPO:-$HOME/clawjournal}"
  if [ -d "$TARGET/.git" ]; then
    echo "-> Updating existing checkout at $TARGET"
    git -C "$TARGET" pull --ff-only --quiet || true
  else
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

echo "[ok] Python: $("$PYTHON" --version 2>&1) ($(command -v "$PYTHON"))"

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
    if ! ( cd "$REPO_DIR/clawjournal/web/frontend" && npm install --silent && npm run build --silent ); then
      echo "[!] Frontend build failed. The CLI is installed; re-run with --with-frontend after fixing the build." >&2
    fi
  fi
fi

# 5) Report.
echo
INSTALLED_VERSION="$("$VENV_PY" -c 'import clawjournal; print(clawjournal.__version__)' 2>/dev/null || echo "?")"
echo "[ok] ClawJournal $INSTALLED_VERSION installed."

cat <<EOF

Run:    $VENV_BIN/clawjournal scan
        $VENV_BIN/clawjournal serve

Or add the venv to your PATH:
        export PATH="$VENV_BIN:\$PATH"
EOF

# 6) Soft hints for optional runtime deps.
if [ ! -f "$REPO_DIR/clawjournal/web/frontend/dist/index.html" ]; then
  cat <<EOF

[i] Browser workbench not built. To enable 'clawjournal serve':
    ./scripts/install.sh --with-frontend       (requires Node.js)
EOF
fi

if ! command -v trufflehog >/dev/null 2>&1; then
  cat <<EOF

[i] TruffleHog is required when sharing exports. Install it before 'bundle-export':
    macOS:    brew install trufflehog
    Linux:    curl -sSfL https://raw.githubusercontent.com/trufflesecurity/trufflehog/main/scripts/install.sh | sh -s -- -b /usr/local/bin
    Windows:  https://github.com/trufflesecurity/trufflehog/releases
EOF
fi
