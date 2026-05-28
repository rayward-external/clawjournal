# ClawJournal

Review and curate your coding agent conversation traces — 100% locally. ClawJournal scans session logs from Claude Code, Claude Desktop, Codex, Gemini CLI, OpenCode, OpenClaw, Kimi CLI, and Cline, automatically anonymizes secrets and personal information, and gives you a browser workbench to review everything before it ever leaves your machine.

## Install in one step (no coding required)

If you have an AI coding assistant — **Claude Code**, **Codex**, **Cursor**, **OpenCode**, **Hermes**, **Gemini CLI**, or similar — you can install ClawJournal without writing any code or running any commands yourself.

**1.** Open your AI assistant.

**2.** Paste this message:

> *Install ClawJournal from https://github.com/rayward-external/clawjournal. Read its README and follow the install instructions for my operating system. Install any missing prerequisites. Verify it works at the end.*

**3.** The AI does the rest. It'll figure out your operating system (Mac, Windows, Linux), install any background tools it needs (git, Python, Node.js), run the install script, and tell you when it's done.

### What to expect

- **Permission prompts — lots of them.** Your AI will ask permission to run several commands. Expect 10–25 prompts before install finishes — more if your computer is fresh, fewer if it already has dev tools. **Click "Allow" each time.** This is normal. The tools the AI installs (git for fetching code, Python for running ClawJournal, Node.js for the browser workbench) are widely-used software your computer probably has parts of already.
- **A separate password prompt on Mac.** macOS may ask for *your computer password* (the one you use to log in) when installing certain tools. This is your operating system asking, not the AI. Type your password and hit Enter — installing software almost always requires this.
- **Silent waiting periods.** Some downloads and compiles take 30–90 seconds with no visible progress. **The AI isn't frozen — it's working.** Wait for it to come back. Total install time is usually 2–10 minutes depending on your network and what's already installed.
- **A success message at the end:** `[ok] ClawJournal 0.1.15 installed.` (the version number may differ).

### Open the workbench

After install, your AI will give you a web address that looks like `http://localhost:8384`. **That's a URL — copy it into the address bar of your web browser** (Chrome, Safari, Firefox, Edge — any of them) and press Enter. The ClawJournal workbench opens locally on your own computer; nothing is uploaded.

### If something doesn't work

- **General fix:** tell your AI *"it didn't work, please fix it."* Most install hiccups (a missing tool, a stale package) are routine.
- **If the AI says it can't do something:** reply *"try a different approach"* or *"are you sure? please try another way."* Installation problems usually have multiple solutions; the agent can find one.
- **If a permission prompt scares you:** ask your AI *"what does this command do, and is it safe?"* before clicking Allow.

### What to do next

Ask your AI: *"Open ClawJournal and help me set it up to review my coding-agent conversations."* It'll walk you through configure, scan, and review in your browser.

Your first scan may take a minute or two if you have lots of past sessions, and the workbench may show an empty list briefly while it indexes — that's normal. The page refreshes automatically as conversations are added.

---

<details>
<summary><b>For AI agents reading this README directly</b> (humans following the 3 steps above can skip this)</summary>

Detect the user's OS, follow the matching block in [Quickstart](#quickstart) below, install all missing prereqs (git, Python 3.10+, and Node.js — the install includes the browser workbench by default), then verify with `clawjournal status`. The install script handles Python detection, venv creation, and dependency install. You have everything you need; proceed.

</details>

<details>
<summary><b>For developers who want to run the commands by hand</b></summary>

The [Quickstart](#quickstart) section below has the explicit per-OS shell commands the agent would execute. Same end result as the AI-driven path.

</details>

## Your data stays local

Everything in the default workflow runs on your own computer:

- `scan`, `serve`, `inbox`, `search`, `score`, `export`, and `bundle-export` all run locally.
- The review UI opens on `localhost:8384` in your own browser — no account, no cloud service.
- `scan` auto-runs a secrets + PII findings pipeline per session. Findings are stored as hashed references in your local SQLite DB — plaintext is never persisted.
- `bundle-export` writes redacted files to your disk. It does not upload them.
- Uploading is a separate, opt-in flow. If you never use the workbench Submit step and never run `bundle-share` against a configured self-hosted ingest endpoint, nothing is sent anywhere.

## If you decide to share

Sharing is fully opt-in and separate from local review. When you do choose to export or upload, ClawJournal re-applies regex redaction (paths, usernames, emails, API keys, tokens, private keys, and similar) on top of the scan-time findings. The workbench Share flow can also add AI-assisted PII review when you opt in for a bundle.

The optional AI-assisted PII review uses the same backend as `score` — your current coding agent's automation CLI (e.g. `codex exec`, the Claude CLI). Home-dir paths and usernames are anonymized locally before anything is sent to the agent; if your agent routes to a cloud provider, that's where the PII review happens.

See [PRIVACY.md](PRIVACY.md) for the full redaction list and the sharing paths (local file, hosted research submission, or self-configured ingest upload).

---

## Quickstart

> Non-coders following the **Install in one step** section above don't need to read this. The shell commands here are for AI agents and developers who run installs by hand. Click the section below to expand it.

<details>
<summary><b>Show shell commands (for AI agents and developers)</b></summary>

**Prerequisites** — `git` + Python 3.10+ are required; Node.js 18+ is required only for the browser workbench (`--with-frontend`). Skip any line whose tool is already installed:

```bash
# macOS (install Homebrew first if you don't have it; NONINTERACTIVE=1 skips the RETURN prompt
# that would otherwise hang an autonomous install. Second line puts brew on PATH for the current shell):
NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv)"
brew install git python              # workbench (optional): brew install node

# Debian / Ubuntu (drop `sudo` if you're root in a container):
sudo apt update && sudo apt install -y git curl python3-full python3-venv

# Workbench (optional). Ubuntu 24.04+ apt is fine; on 22.04 or older, distro Node
# is too old for the Vite build — install the LTS via NodeSource first:
curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo bash -
sudo apt install -y nodejs

# Windows (PowerShell, native package manager). The flags suppress interactive prompts that block autonomous installs:
winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements --scope user
winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements --scope user
winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements              # workbench (optional). The Node MSI doesn't support --scope user; this needs admin or will prompt for elevation.

# Refresh PATH in the current PowerShell session (winget doesn't do this for you):
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
```

Then pick the block for your OS and run it. The install script handles Python detection, venv creation, and editable install. Run `./scripts/install.sh --help` for all options.

**macOS / Linux / WSL / Git Bash on Windows:**

```bash
git clone https://github.com/rayward-external/clawjournal.git ~/clawjournal
cd ~/clawjournal
./scripts/install.sh --with-frontend       # or: sh scripts/install.sh --with-frontend  (if the +x bit is missing)
```

**Native Windows PowerShell** — use `pwsh` (PowerShell 7+) if available, otherwise `powershell` (legacy 5.1) works the same:

```powershell
git clone https://github.com/rayward-external/clawjournal.git "$HOME\clawjournal"
Set-Location "$HOME\clawjournal"
pwsh -ExecutionPolicy Bypass -File .\scripts\install.ps1 -WithFrontend
# If `pwsh` is not installed: powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -WithFrontend
```

The script prints `[ok] ClawJournal <version> installed.` on success. The default above includes `--with-frontend` / `-WithFrontend` which builds the browser workbench at `localhost:8384`. **If Node.js is not installed, the script warns and continues with a CLI-only install** — `clawjournal serve` will then 404; install Node and re-run the script with the flag to fix. Drop the flag entirely if you only need `scan`, `inbox`, `search`, and `bundle-export` (these don't need the workbench).

**Stays current automatically.** Each invocation runs a silent, throttled background fast-forward from `rayward-external/clawjournal` (once per hour, capped on time, network-failure-safe). New code is picked up by the editable install on the next run — no manual upgrades. Skipped on dirty trees, local-only commits, diverged histories, or non-`main` branches. Opt out with `CLAWJOURNAL_NO_AUTO_UPDATE=1`, or run a synchronous update anytime with `clawjournal selfupdate` (`--check` to peek, `--force` to discard local changes on `main` only).

**Verify** — you should see a JSON response with `"stage"` and `"stage_number"`:

```bash
~/.clawjournal-venv/bin/clawjournal status                              # POSIX
```

```powershell
& "$HOME\.clawjournal-venv\Scripts\clawjournal.exe" status              # PowerShell
```

```json
{ "stage": "configure", "stage_number": 2, "total_stages": 4, ... }
```

The CLI lives at `~/.clawjournal-venv/bin/clawjournal` (POSIX) or `$HOME\.clawjournal-venv\Scripts\clawjournal.exe` (Windows). The install is idempotent — re-run the script any time to fast-forward to the latest source.

**To call it as plain `clawjournal` instead of the full path** (current shell session; add to your shell profile to persist):

```bash
export PATH="$HOME/.clawjournal-venv/bin:$PATH"          # POSIX (bash/zsh)
```

```powershell
$env:Path = "$HOME\.clawjournal-venv\Scripts;" + $env:Path   # PowerShell
```

> **Already inside a coding agent and want it to drive ClawJournal for you?** `npx skills add rayward-external/clawjournal` adds three skills (Claude Code, Codex, Cursor, …); then say *"setup clawjournal"* — the wizard runs the same script above. Optional convenience, not a separate install path. See [Stage 1: Install](#1-install).
>
> **Can't clone? Behind a firewall?** `pipx install clawjournal` works as a fallback, but the PyPI wheel currently lags the source by many releases. See [Stage 1: Install](#1-install).

</details>

---

## End-to-end flow

After install, six stages take you from indexing local sessions to (optionally) sharing a redacted bundle. **Non-coders: each stage starts with the natural-language prompt to give your AI assistant.** The shell commands the agent runs are tucked into expandable "Show shell commands" sections — you can ignore them.

> *Heads up for developers running commands by hand: the shell snippets use bare `clawjournal`. If you haven't added the venv bin to `PATH`, prefix every command with `~/.clawjournal-venv/bin/` (POSIX) or `$HOME\.clawjournal-venv\Scripts\` (Windows).*

```
 Install ──► Configure ──► Scan ──► Triage ──► Score ──► Package & Share
    1            2           3          4          5              6
```

**Optional skills layer** — `npx skills add rayward-external/clawjournal` installs three skills into Claude Code / Codex / Cursor / Gemini CLI / OpenCode and similar agents:

| Skill | Covers stages |
|-------|---------------|
| **clawjournal-setup** | 1 Install · 3 Scan · workbench launch |
| **clawjournal** | 4 Triage · 6 Package & Share |
| **clawjournal-score** | 5 Score |

With skills installed, prompts like *"triage my new sessions"*, *"score everything unscored"*, or *"package my approved sessions for export"* route to the right skill. Skills are a convenience for agent-driven workflows — the shell commands below work the same without them.

### 1. Install

The canonical install is the shell script in [Quickstart](#quickstart) above — that's what you want unless your environment blocks it. If you used Quickstart, skip to Stage 2. Two fallbacks:

**Skills install (guided wizard inside your coding agent):**

```bash
npx skills add rayward-external/clawjournal
```

Then say *"setup clawjournal"* inside the agent. The `clawjournal-setup` wizard runs `scripts/install.sh` (or `install.ps1`) under the hood and walks through scan + workbench launch. The end state is identical to the Quickstart shell install.

**PyPI install (fallback, lags GitHub source):**

```bash
pipx install clawjournal        # or: pip install clawjournal
```

The PyPI wheel ships the pre-built workbench (no Node.js needed) but is currently many versions behind the source — features documented in this README may be missing. Use this only when installing from source isn't an option. `pip show clawjournal` reports the wheel's version.

**TruffleHog** is **not** required to install or use ClawJournal locally. It is only needed for [Stage 6 Package & Share](#6-package--share) — every `bundle-export` and `share` runs an independent secrets scan on the redacted output, and exports are blocked if TruffleHog is missing or finds anything. Your AI can install it for you when you reach Stage 6, or you can defer it entirely if you only plan to use ClawJournal locally.

<details>
<summary><b>Show TruffleHog install commands (your AI handles this for you)</b></summary>

```bash
brew install trufflehog                                    # macOS
curl -sSfL https://raw.githubusercontent.com/trufflesecurity/trufflehog/main/scripts/install.sh | sh -s -- -b /usr/local/bin   # Linux
# Windows: download a release binary from https://github.com/trufflesecurity/trufflehog/releases
```

</details>

See [PRIVACY.md](PRIVACY.md) for the full gate semantics.

### 2. Configure

Tell ClawJournal which agents' sessions to scan and what to exclude or redact.

**Just say to your AI:**

> *"Configure clawjournal — scan all sources with defaults, no exclusions."*
>
> Or, to narrow scope: *"Configure clawjournal to scan only claude and codex, exclude the `scratch` project, and always redact the string `acme-internal`."*

The agent translates this into the right CLI calls. Subsequent scans pick up new settings automatically.

<details>
<summary><b>Show shell commands (what the agent runs)</b></summary>

```bash
clawjournal config --source all                   # claude | codex | gemini | opencode | openclaw | kimi | custom | all
clawjournal list                                  # see discovered projects
clawjournal config --exclude "project1,project2"  # optional: exclude projects
clawjournal config --redact "string1,string2"     # optional: custom redactions (appends)
clawjournal config --redact-usernames "handle1"   # optional: anonymize usernames (appends)
clawjournal config --confirm-projects             # lock in project selection
```

`--exclude`, `--redact`, and `--redact-usernames` all append; they never overwrite. Safe to call repeatedly.

</details>

### 3. Scan

Reads your local session files into a private database on your computer. As it reads, ClawJournal automatically detects secrets and personal information so you can review them before sharing anything. Plaintext is never saved — only safe references.

**Just say to your AI:** *"scan my coding-agent sessions"* (a first scan also runs automatically as part of *"setup clawjournal"*).

<details>
<summary><b>Show shell command</b></summary>

```bash
clawjournal scan
```

The workbench daemon (`clawjournal serve`) also scans continuously in the background.

</details>

### 4. Triage

Mark which conversations you want to keep ("approve") and which to discard ("block"). The browser workbench at `localhost:8384` is the easiest place to do this — you'll see each conversation with a summary, and click Approve or Block.

**Just say to your AI:** *"Open clawjournal and help me triage my unreviewed sessions."*

<details>
<summary><b>Show shell commands</b></summary>

```bash
clawjournal serve                                    # workbench UI — the primary review surface
# or directly in the terminal:
clawjournal inbox --json --limit 20                  # list sessions
clawjournal search "refactor auth" --json            # full-text search
clawjournal approve <session_id> --reason "clean"    # approve
clawjournal block <session_id> --reason "private"    # block
clawjournal shortlist <session_id>                   # mark for deeper review
```

</details>

Sometimes you want to set a conversation aside without blocking it permanently — for example, "this might be useful but I need to clear it with legal first." That's called a "hold." Just say to your AI: *"Put this session on hold pending legal review."* The agent will mark it. You can release it later, or set an embargo (auto-release at a specific date).

<details>
<summary><b>Show hold-state shell commands (CLI only — non-coders can ignore)</b></summary>

```bash
clawjournal hold <id> --reason "pending legal review"
clawjournal release <id>
clawjournal embargo <id> --until 2026-06-01
clawjournal hold-history <id>
```

</details>

### 5. Score

AI-assisted scoring now records two labels in one pass: a legacy productivity score (`ai_quality_score`) and the primary failure-value score (`ai_failure_value_score`). Failure value prioritizes traces where a frontier coding agent made, recovered from, or failed to recover from a meaningful mistake on real work. Personal info in your conversations is removed before anything is sent to the AI judge — your home folder paths and usernames are anonymized first.

**Just say to your AI:** *"Score my unscored ClawJournal sessions and auto-block the noise."*

The agent batches the scoring over the `failure-corpus` source scope (`claude`, `codex`, `opencode`, `openclaw`). Productivity-1 sessions get auto-blocked when `--auto-triage` is set only if failure value is 1-2; failure value 3+ stays visible for review and sharing.

<details>
<summary><b>Show shell commands</b></summary>

```bash
clawjournal score --batch --source failure-corpus --auto-triage  # score failure-value scope; auto-block low-value productivity-1 noise
clawjournal score --batch --source failure-corpus --window 7d     # restrict to traces from the last N days
clawjournal score-view <id>                          # show score details
clawjournal set-score <id> --failure-value 4 --failure-evidence "User corrected a fabricated API call"
clawjournal set-score <id> --quality 4               # legacy productivity override
```

By default scoring uses the current agent's automation CLI (e.g. `codex exec` inside Codex, the Claude CLI inside Claude Code). Supported backends are `claude`, `codex`, `hermes`, and `openclaw`; use `--backend` to override. For Codex specifically, `codex exec` reuses saved CLI authentication by default; for automation the recommended explicit credential is `CODEX_API_KEY`.

The browser workbench also keeps share-ready traces warm: on app load it offers to score the latest unscored `failure-corpus` traces in the background. It scores only after you confirm a backend once; the choice is persisted to `~/.clawjournal/config.json`. If you never open the workbench, confirm a backend headlessly with `clawjournal config --scorer-backend <claude|codex|hermes|openclaw>` (use `--scorer-backend none` to clear it).

</details>

### 6. Package & Share

Package the conversations you approved into a redacted file on your computer. Uploading anywhere is a separate, opt-in step — by default the file just sits on your disk.

**Just say to your AI:** *"Package my approved ClawJournal sessions and export them to a file on my computer."*

The agent walks you through the Share page in the browser workbench: **Queue → Redact → Review → Package → Submit → Done**. The Redact step always uses deterministic and policy rules; you can opt in to AI review to catch contextual personal info the automatic scan missed. When enabled, upload-time AI review runs a small parallel worker pool by default; set `CLAWJOURNAL_UPLOAD_PII_WORKERS=1` to serialize it or `CLAWJOURNAL_UPLOAD_PII_TIMEOUT_SECONDS=90` to allow longer AI review per trace.

To actually upload after packaging (optional), use the workbench **Submit** step. It verifies email, shows consent, sends the finalized zip to the hosted service, and stores the returned receipt locally. Self-hosters can override the destination with `CLAWJOURNAL_SHARE_URL`; setting `CLAWJOURNAL_SHARE_URL=` disables hosted submission and leaves download-only packaging.

> *(optional)* *"Submit this bundle to ClawJournal Research."*

Uploads are gated: only conversations you approved and confirmed for sharing leave your machine.

<details>
<summary><b>Show shell commands</b></summary>

```bash
clawjournal bundle-create --status approved          # bundle all approved sessions
clawjournal bundle-list
clawjournal bundle-view <bundle_id>                  # inspect before exporting
clawjournal bundle-export <bundle_id> --zip          # write an uploadable zip plus export folder

# Optional hosted research submission:
# use the workbench Share tab's Submit step

# Advanced self-hosted ingest upload:
clawjournal share --preview --status approved        # dry-run
clawjournal bundle-share <bundle_id>                 # self-hosted ingest upload
```

Upload is gated on hold-state: only sessions in `auto_redacted` or `released` can leave the machine.

</details>

---

## Build the browser workbench

If you followed the **Install in one step** section at the top of this README, the workbench is already built — you don't need this section.

This section is only for developers who installed without the workbench (no `--with-frontend` flag) and want to add it later, or who want to do the frontend build by hand.

<details>
<summary><b>Show frontend-build shell commands</b></summary>

`clawjournal serve` opens a local Vite app from `clawjournal/web/frontend/dist/`. The PyPI wheel ships this `dist/` pre-built; a source install needs a one-time build.

The simplest way is to re-run the installer with the frontend flag:

```bash
~/clawjournal/scripts/install.sh --with-frontend                                            # POSIX
pwsh -ExecutionPolicy Bypass -File "$HOME\clawjournal\scripts\install.ps1" -WithFrontend    # Windows (substitute `powershell` if pwsh isn't installed)
```

Or do it manually:

```bash
cd ~/clawjournal/clawjournal/web/frontend
npm install
npm run build
```

Either path requires Node.js. Skip the build entirely if you're only using the CLI (`scan`, `inbox`, `search`, `bundle-export`, …).

</details>

<details>
<summary><b>Python not installed?</b></summary>

ClawJournal requires Python 3.10+.

| Platform | Install command |
|----------|----------------|
| **macOS** | `brew install python` |
| **Windows** | Download from [python.org/downloads](https://python.org/downloads) — check "Add to PATH" |
| **Linux** | `sudo apt install python3-full` (includes venv support) |

</details>

<details>
<summary><b>Node.js (only for the frontend build)</b></summary>

| Platform | Install command |
|----------|----------------|
| **macOS** | `brew install node` |
| **Windows** | Download from [nodejs.org](https://nodejs.org) |
| **Linux** | `sudo apt install nodejs npm` |

</details>

<details>
<summary><b>Developing ClawJournal itself</b></summary>

```bash
git clone https://github.com/rayward-external/clawjournal.git
cd clawjournal
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
pytest
```

On native Windows (no WSL / Git Bash), replace `source .venv/bin/activate` with `.venv\Scripts\Activate.ps1`.

If you see `externally-managed-environment` on Linux/macOS, make sure the venv is activated before running `python -m pip` ([PEP 668](https://peps.python.org/pep-0668/)).

</details>

## Supported agents

ClawJournal can parse session data from: Claude Code, Claude Desktop, Codex, Gemini CLI, OpenCode, OpenClaw, Kimi CLI, and Cline.

## Project docs

- [PRIVACY.md](PRIVACY.md) — what stays local, what gets redacted, and how optional sharing works
- [ARCHITECTURE.md](ARCHITECTURE.md) — public architecture overview
- [CONTRIBUTING.md](CONTRIBUTING.md) — contribution guidelines
- [SECURITY.md](SECURITY.md) — security reporting and threat-model scope

---

## Command reference

<details>
<summary><b>All commands</b></summary>

### Essential

| Command | Description |
|---------|-------------|
| `clawjournal scan` | Index local sessions + run findings pipeline |
| `clawjournal serve` | Open workbench UI at localhost:8384 |
| `clawjournal config --source all` | Select source scope (required) |
| `clawjournal config --confirm-projects` | Confirm project selection (required before export) |
| `clawjournal config --scorer-backend codex` | Confirm the AI scoring backend used for background workbench scoring (`none` clears it) |
| `clawjournal score --batch --source failure-corpus --auto-triage` | AI-score failure-value scope; auto-block low-value productivity-1 noise |
| `clawjournal bundle-create --status approved` | Bundle approved sessions |
| `clawjournal bundle-export <bundle_id> --zip` | Export bundle to disk and write an uploadable zip |

### Triage & review

| Command | Description |
|---------|-------------|
| `clawjournal inbox --json --limit 20` | List sessions as JSON |
| `clawjournal search <query> --json` | Full-text search |
| `clawjournal approve <id> [id ...]` | Approve sessions |
| `clawjournal block <id> [id ...]` | Block sessions |
| `clawjournal shortlist <id> [id ...]` | Shortlist sessions |
| `clawjournal score --batch --source failure-corpus --limit 20` | AI-score up to 20 in-scope sessions |
| `clawjournal score --batch --source failure-corpus --window 7d` | AI-score only in-scope sessions from the last N days |
| `clawjournal score-view <id>` | View score details |
| `clawjournal set-score <id> --failure-value <1-5>` | Manually set the failure-value score; 4-5 requires `--failure-evidence` |
| `clawjournal set-score <id> --quality <1-5>` | Manually set the legacy productivity score |

### Hold-state gate

| Command | Description |
|---------|-------------|
| `clawjournal hold <id>` | Move session to `pending_review` (blocks upload) |
| `clawjournal release <id>` | Release a held session for share |
| `clawjournal embargo <id> --until <ISO>` | Time-lock a session (auto-releases on expiry) |
| `clawjournal hold-history <id>` | Show the full hold-state timeline |

### Findings & allowlist

| Command | Description |
|---------|-------------|
| `clawjournal findings <id>` | List findings (hashed entities) for a session |
| `clawjournal findings <id> --accept <ref>` | Accept a finding (will be redacted at export) |
| `clawjournal findings <id> --ignore <ref>` | Ignore a finding |
| `clawjournal findings <id> --accept-all` / `--ignore-all` | Bulk decision on open findings |
| `clawjournal allowlist list` | Show global allowlist |
| `clawjournal allowlist add ...` | Allowlist an entity (hashed locally) |
| `clawjournal allowlist remove <id>` | Remove an allowlist entry |

### Bundles

| Command | Description |
|---------|-------------|
| `clawjournal bundle-create --status approved` | Create bundle from all approved sessions |
| `clawjournal bundle-list` | List bundles |
| `clawjournal bundle-view <bundle_id>` | View bundle details |
| `clawjournal bundle-export <bundle_id> --zip` | Export bundle and write an uploadable zip |
| `clawjournal bundle-share <bundle_id>` | Advanced self-hosted ingest upload |

### Quick share

| Command | Description |
|---------|-------------|
| `clawjournal recent` | Show recent sessions (auto-scans if stale) |
| `clawjournal recent --source openclaw --since today` | Filter by source and time |
| `clawjournal card <id>` | Generate a share card for a session |
| `clawjournal card <id> --depth workflow` | Workflow-only card (safe for public channels) |
| `clawjournal card <id> --depth full` | Full card with redacted content |

### Advanced self-hosted ingest upload

| Command | Description |
|---------|-------------|
| `clawjournal verify-email you@university.edu` | Verify an academic email for hosted workbench submission |
| `clawjournal share --preview --status approved` | Preview what would be packaged |
| `clawjournal share --status approved` | Package locally and print the workbench Submit URL |
| `clawjournal share --status approved --ai-pii-review` | Package with optional AI-assisted PII review |
| `clawjournal bundle-share <bundle_id>` | Upload through an explicitly configured self-hosted ingest service |

### Configuration

| Command | Description |
|---------|-------------|
| `clawjournal config --exclude "a,b"` | Add excluded projects (appends) |
| `clawjournal config --redact "str1,str2"` | Add strings to always redact (appends) |
| `clawjournal config --redact-usernames "u1,u2"` | Add usernames to anonymize (appends) |
| `clawjournal config --ai-pii-review` / `--no-ai-pii-review` | Set the default AI-assisted PII review on/off for the share flow |
| `clawjournal list` | List all projects with exclusion status |
| `clawjournal status` | Show current stage and next steps (JSON) |
| `clawjournal update-skill <agent>` | Install/update the clawjournal skill for an agent |
| `clawjournal selfupdate` | Fast-forward to latest from `rayward-external/clawjournal` (sync) |
| `clawjournal selfupdate --check` | Report whether updates are available without applying |
| `clawjournal serve --remote` | Print SSH tunnel command for remote VM access |

### Export & sanitize (advanced)

| Command | Description |
|---------|-------------|
| `clawjournal export` | Export to local JSONL |
| `clawjournal export --no-thinking` | Exclude extended thinking blocks |
| `clawjournal export --pii-review --pii-apply` | Legacy LLM-PII path — export + AI-PII review + sanitize |
| `clawjournal pii-review --file <file> --output <findings.json>` | Legacy — run PII detection on an exported file |
| `clawjournal pii-apply --file <file> --findings <findings.json> --output <sanitized.jsonl>` | Legacy — apply PII redactions to an exported file |
| `clawjournal pii-rubric` | Show PII entity types and detection rules |

**Legacy note:** `pii-review` and `pii-apply` remain for AI-based PII review of already-exported files, but deterministic secrets/PII detection has moved to the `findings` + `bundle-export` flow above. Prefer the new path.

</details>

<details>
<summary><b>What gets exported & data schema</b></summary>

| Data | Included | Notes |
|------|----------|-------|
| User messages | Yes | Full text (including voice transcripts) |
| Assistant responses | Yes | Full text output |
| Extended thinking | Yes | Claude's reasoning (opt out with `--no-thinking`) |
| Tool calls | Yes | Tool name + inputs + outputs |
| Token usage | Yes | Input/output tokens per session |
| Model & metadata | Yes | Model name, git branch, timestamps |

Each line in the exported JSONL is one session:

```json
{
  "session_id": "abc-123",
  "project": "my-project",
  "model": "claude-opus-4-6",
  "git_branch": "main",
  "start_time": "2025-06-15T10:00:00+00:00",
  "end_time": "2025-06-15T10:00:00+00:00",
  "messages": [
    {"role": "user", "content": "Fix the login bug", "timestamp": "..."},
    {
      "role": "assistant",
      "content": "I'll investigate the login flow.",
      "thinking": "The user wants me to look at...",
      "tool_uses": [
          {
            "tool": "bash",
            "input": {"command": "grep -r 'login' src/"},
            "output": {"text": "src/auth.py:42: def login(user, password):"},
            "status": "success"
          }
        ],
      "timestamp": "..."
    }
  ],
  "stats": {
    "user_messages": 5, "assistant_messages": 8,
    "tool_uses": 20, "input_tokens": 50000, "output_tokens": 3000
  }
}
```

</details>

<details>
<summary><b>Gotchas</b></summary>

- **`--exclude`, `--redact`, `--redact-usernames` APPEND** — they never overwrite. Safe to call repeatedly.
- **Source and project confirmation are required** — the CLI blocks export until both are set.
- **`scan` already redacts.** Secrets and PII findings are computed and stored as hashed references at scan time. For additional LLM-PII review, opt in on the workbench Share page. The legacy `--pii-review` / `--pii-apply` CLI path still works for sanitizing already-exported files.
- **Hold-state gates uploads.** Sessions in `pending_review` or active `embargoed` cannot be shared; `auto_redacted` (default) and `released` can.
- **Large exports take time** — 500+ sessions may take 1–3 minutes.
- **Virtual environment recommended** — modern Linux (and some macOS setups) block system-wide pip installs. Use a venv to avoid issues.

</details>

## Acknowledgments

ClawJournal builds on early work from [dataclaw](https://github.com/peteromallet/dataclaw) by [@peteromallet](https://github.com/peteromallet).

## License

Apache-2.0
