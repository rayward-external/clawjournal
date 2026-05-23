---
name: clawjournal-setup
description: Install ClawJournal, scan coding agent sessions, and launch the review workbench. Use when user wants to set up clawjournal, review their traces, get started with trace curation, or says "setup clawjournal". Triggers on "setup clawjournal", "install clawjournal", "review my traces", or first-time clawjournal requests.
---

# ClawJournal Setup

Interactive setup wizard. Walk the user through each step. Only pause when user input is required. Fix problems yourself when possible.

**Principle:** When something is broken or missing, fix it. Don't tell the user to go fix it themselves unless it genuinely requires their action. If a dependency is missing, install it. If a command fails, diagnose and repair.

## 0. Preflight

Check if `clawjournal` is already installed:
- `command -v clawjournal && clawjournal status`
- `test -x ~/.clawjournal-venv/bin/clawjournal && ~/.clawjournal-venv/bin/clawjournal status`

**If found and the user is just resuming:** skip to Step 2. If only `~/.clawjournal-venv/bin/clawjournal` exists, use that full path for commands below.
**If found but the user wants the latest:** re-run Step 1's installer (idempotent — it'll fast-forward the checkout and reinstall in place), then continue to Step 2.
**If not found:** Continue to Step 1.

## 1. Install

Use the bundled installer scripts. They detect a Python 3.10+ interpreter, create an isolated venv at `~/.clawjournal-venv`, and `pip install -e` the repo. Idempotent — safe to re-run.

First, ask: "Do you want the browser workbench? (Recommended — it's the primary review surface. Otherwise the CLI works alone.)" Use the answer to pick whether to pass the frontend flag below.

**macOS / Linux / WSL / Git Bash on Windows:**

```bash
if [ -d ~/clawjournal/.git ]; then
  git -C ~/clawjournal pull --ff-only
else
  git clone https://github.com/rayward-external/clawjournal.git ~/clawjournal
fi

# Pick ONE based on the user's answer above:
~/clawjournal/scripts/install.sh                    # CLI only
~/clawjournal/scripts/install.sh --with-frontend    # also build the browser workbench (needs Node.js)
```

**Native Windows PowerShell** (no WSL / Git Bash):

```powershell
if (Test-Path "$HOME\clawjournal\.git") {
  git -C "$HOME\clawjournal" pull --ff-only
} else {
  git clone https://github.com/rayward-external/clawjournal.git "$HOME\clawjournal"
}

# Pick ONE based on the user's answer above:
powershell -ExecutionPolicy Bypass -File "$HOME\clawjournal\scripts\install.ps1"                # CLI only
powershell -ExecutionPolicy Bypass -File "$HOME\clawjournal\scripts\install.ps1" -WithFrontend  # also build the browser workbench (needs Node.js)
```

The script's exit code and printed output tell you exactly what to do next. Common failure modes the script surfaces directly:

- **Python 3.10+ not found** — install via the platform hint the script prints, then re-run. macOS users may also need `xcode-select --install` to get a working `python3`.
- **`python3 -m venv` fails on Debian/Ubuntu** — run `sudo apt install -y python3-venv python3-full`, then re-run the script.
- **`node` / `npm` missing** when `--with-frontend` was requested — the script skips the frontend with a warning. Install Node.js (macOS: `brew install node`, Linux: `sudo apt-get install -y nodejs npm`, Windows: nodejs.org) and re-run with the flag.

Verify (POSIX):

```bash
~/.clawjournal-venv/bin/clawjournal status
```

Verify (PowerShell):

```powershell
& "$HOME\.clawjournal-venv\Scripts\clawjournal.exe" status
```

## 2. Scan Sessions

Discover all local coding agent sessions:

```bash
~/.clawjournal-venv/bin/clawjournal scan
```

This indexes sessions from Claude Code, Codex, Gemini CLI, OpenCode, OpenClaw, Kimi CLI, and Cline — whatever is present on the machine.

Show the user a summary: "Found N sessions across M sources."

If zero sessions found:
- Check if the user has any supported coding agents installed
- Common issue: sessions live in non-default paths — ask the user

## 3. Score & Auto-Triage (optional but recommended)

Ask: "Would you like me to auto-score your sessions? This uses AI to rate quality 1-5 and auto-approve high-quality traces."

If yes:

```bash
~/.clawjournal-venv/bin/clawjournal score --batch --auto-triage
```

Show summary: "N auto-approved (quality 4-5), M auto-blocked (1-2), K need review."

If no: skip to Step 4.

## 4. Launch Workbench

Ask: "How would you like to review your traces?"

**Option A — Browser UI (recommended for local machines):**

Check whether the frontend is already built:

```bash
test -f ~/clawjournal/clawjournal/web/frontend/dist/index.html && echo built || echo missing
```

If missing, re-run the installer with the frontend flag (POSIX):

```bash
~/clawjournal/scripts/install.sh --with-frontend
```

PowerShell equivalent:

```powershell
powershell -ExecutionPolicy Bypass -File "$HOME\clawjournal\scripts\install.ps1" -WithFrontend
```

Then start the workbench:

```bash
~/.clawjournal-venv/bin/clawjournal serve
```

Tell the user: "Your workbench is open at localhost:8384. Everything is 100% local. Use the Inbox to triage traces, Search to find sessions, and Bundles to assemble exports."

**Option B — Terminal review (for remote VMs or headless environments):**

```bash
~/.clawjournal-venv/bin/clawjournal inbox --json --limit 15
```

Parse the JSON and present traces as a numbered list. Then guide triage interactively.

**For remote VMs:** `clawjournal serve --remote` prints the SSH tunnel command.

## 5. Done

Show summary:
- ClawJournal version installed
- Number of sessions indexed
- Number scored/triaged (if applicable)
- How to access the workbench

Tell the user:
- "You can review and share traces anytime with `/clawjournal`"
- "Score sessions with `/clawjournal-score`"
- "Everything stays 100% local until you explicitly choose to share"

## Troubleshooting

**clawjournal command not found after install:** Use `~/.clawjournal-venv/bin/clawjournal` directly, or add the venv bin directory to your shell PATH.

**No sessions found:** Make sure you've used a supported coding agent (Claude Code, Codex, Gemini CLI, etc.) on this machine. Sessions are stored in agent-specific directories under your home folder.

**Permission errors on scan:** ClawJournal reads session files from `~/.claude/`, `~/.codex/`, etc. Ensure these directories are readable.

**Browser UI shows a placeholder page:** The frontend has not been built yet. Re-run the installer with the frontend flag: `~/clawjournal/scripts/install.sh --with-frontend` (or `.\scripts\install.ps1 -WithFrontend` on PowerShell). Requires Node.js.

**venv issues on Linux:** If you see `externally-managed-environment`, make sure you're installing into the venv: `python3 -m venv ~/.clawjournal-venv && ~/.clawjournal-venv/bin/python -m pip install -e ~/clawjournal`.
