# ClawJournal

Review and curate your coding agent conversation traces — 100% locally. ClawJournal scans session logs from Claude Code, Claude Desktop, Codex, Gemini CLI, OpenCode, OpenClaw, Kimi CLI, and Cline, automatically anonymizes secrets and personal information, and gives you a browser workbench to review everything before it ever leaves your machine.

## Your data stays local

Everything in the default workflow runs on your own computer:

- `scan`, `serve`, `inbox`, `search`, `score`, `export`, and `bundle-export` all run locally.
- The review UI opens on `localhost:8384` in your own browser — no account, no cloud service.
- `scan` auto-runs a secrets + PII findings pipeline per session. Findings are stored as hashed references in your local SQLite DB — plaintext is never persisted.
- `bundle-export` writes redacted files to your disk. It does not upload them.
- Uploading is a separate, opt-in flow. If you never configure an ingest endpoint and never run a share command, nothing is sent anywhere.

## If you decide to share

Sharing is fully opt-in and separate from local review. When you do choose to export or upload, ClawJournal re-applies regex redaction (paths, usernames, emails, API keys, tokens, private keys, and similar) on top of the scan-time findings, and the workbench Share flow adds an AI-assisted PII review on top of that.

The AI-assisted PII review uses the same backend as `score` — your current coding agent's automation CLI (e.g. `codex exec`, the Claude CLI). Home-dir paths and usernames are anonymized locally before anything is sent to the agent; if your agent routes to a cloud provider, that's where the PII review happens. Override with `--backend` to keep the call on a local model.

See [PRIVACY.md](PRIVACY.md) for the full redaction list and the two sharing paths (local file vs. self-configured upload).

---

## Quickstart

**Prerequisites** — `git` + Python 3.10+ are required; Node.js 18+ is required only for the browser workbench (`--with-frontend`). Skip any line whose tool is already installed:

```bash
# macOS:
brew install git python              # workbench (optional): brew install node

# Debian / Ubuntu (drop `sudo` if you're root in a container):
sudo apt update && sudo apt install -y git curl python3-full python3-venv
sudo apt install -y nodejs npm       # workbench (optional). 24.04+ ships Node 18; on older LTS use NodeSource: curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo bash - && sudo apt install -y nodejs

# Windows (PowerShell, native package manager):
winget install --id Git.Git -e
winget install --id Python.Python.3.12 -e
winget install --id OpenJS.NodeJS.LTS -e   # workbench (optional)
# winget doesn't refresh the current shell's PATH; close this PowerShell window and open a new one before continuing.
```

Then pick the block for your OS and run it. The install script handles Python detection, venv creation, and editable install. Run `./scripts/install.sh --help` for all options.

**macOS / Linux / WSL / Git Bash on Windows:**

```bash
git clone https://github.com/kai-rayward/clawjournal.git ~/clawjournal
cd ~/clawjournal
./scripts/install.sh --with-frontend       # or: sh scripts/install.sh --with-frontend  (if the +x bit is missing)
```

**Native Windows PowerShell** — use `pwsh` (PowerShell 7+) if available, otherwise `powershell` (legacy 5.1) works the same:

```powershell
git clone https://github.com/kai-rayward/clawjournal.git "$HOME\clawjournal"
Set-Location "$HOME\clawjournal"
pwsh -ExecutionPolicy Bypass -File .\scripts\install.ps1 -WithFrontend
# If `pwsh` is not installed: powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -WithFrontend
```

The script prints `[ok] ClawJournal <version> installed.` on success. The default above includes `--with-frontend` / `-WithFrontend` which builds the browser workbench at `localhost:8384`. **If Node.js is not installed, the script warns and continues with a CLI-only install** — `clawjournal serve` will then 404; install Node and re-run the script with the flag to fix. Drop the flag entirely if you only need `scan`, `inbox`, `search`, and `bundle-export` (these don't need the workbench).

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

The CLI lives at `~/.clawjournal-venv/bin/clawjournal` (POSIX) or `$HOME\.clawjournal-venv\Scripts\clawjournal.exe` (Windows). The install is idempotent — re-run the script any time to upgrade against the latest `git pull`.

**To call it as plain `clawjournal` instead of the full path** (current shell session; add to your shell profile to persist):

```bash
export PATH="$HOME/.clawjournal-venv/bin:$PATH"          # POSIX (bash/zsh)
```

```powershell
$env:Path = "$HOME\.clawjournal-venv\Scripts;" + $env:Path   # PowerShell
```

> **Already inside a coding agent and want it to drive ClawJournal for you?** `npx skills add kai-rayward/clawjournal` adds three skills (Claude Code, Codex, Cursor, …); then say *"setup clawjournal"* — the wizard runs the same script above. Optional convenience, not a separate install path. See [Stage 1: Install](#1-install).
>
> **Can't clone? Behind a firewall?** `pipx install clawjournal` works as a fallback, but the PyPI wheel currently lags the source by many releases. See [Stage 1: Install](#1-install).

---

## End-to-end flow

After install, six stages take you from indexing local sessions to optionally sharing a redacted bundle. Each stage shows the shell command (the canonical path) and the equivalent skill prompt (if you've done `npx skills add`).

> **Heads up on `PATH`:** the shell snippets below use bare `clawjournal`. If you haven't added the venv bin to `PATH`, prefix every command with `~/.clawjournal-venv/bin/` (POSIX) or `$HOME\.clawjournal-venv\Scripts\` (Windows).

```
 Install ──► Configure ──► Scan ──► Triage ──► Score ──► Package & Share
    1            2           3          4          5              6
```

**Optional skills layer** — `npx skills add kai-rayward/clawjournal` installs three skills into Claude Code / Codex / Cursor / Gemini CLI / OpenCode and similar agents:

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
npx skills add kai-rayward/clawjournal
```

Then say *"setup clawjournal"* inside the agent. The `clawjournal-setup` wizard runs `scripts/install.sh` (or `install.ps1`) under the hood and walks through scan + workbench launch. The end state is identical to the Quickstart shell install.

**PyPI install (fallback, lags GitHub source):**

```bash
pipx install clawjournal        # or: pip install clawjournal
```

The PyPI wheel ships the pre-built workbench (no Node.js needed) but is currently many versions behind the source — features documented in this README may be missing. Use this only when installing from source isn't an option. `pip show clawjournal` reports the wheel's version.

**TruffleHog** is **not** required to install or use ClawJournal locally. It is required only for [Stage 6 Package & Share](#6-package--share) — every `bundle-export` and `share` runs an independent secrets scan on the redacted output, and exports are blocked if TruffleHog is missing or finds anything. Install it before your first share; you can defer it.

```bash
brew install trufflehog                                    # macOS
curl -sSfL https://raw.githubusercontent.com/trufflesecurity/trufflehog/main/scripts/install.sh | sh -s -- -b /usr/local/bin   # Linux
# Windows: download a release binary from https://github.com/trufflesecurity/trufflehog/releases
```

See [PRIVACY.md](PRIVACY.md) for the full gate semantics.

### 2. Configure

Tell ClawJournal which agents' sessions to scan and what to exclude or redact.

**Shell:**

```bash
clawjournal config --source all                   # claude | codex | gemini | opencode | openclaw | kimi | custom | all
clawjournal list                                  # see discovered projects
clawjournal config --exclude "project1,project2"  # optional: exclude projects
clawjournal config --redact "string1,string2"     # optional: custom redactions (appends)
clawjournal config --redact-usernames "handle1"   # optional: anonymize usernames (appends)
clawjournal config --confirm-projects             # lock in project selection
```

`--exclude`, `--redact`, and `--redact-usernames` all append; they never overwrite. Safe to call repeatedly.

**Skill prompt** (if you've done `npx skills add`):

> *"setup clawjournal"* — first-time run with defaults (all sources, no exclusions). The skill runs install + scan; if ClawJournal is already installed it skips to scan.
>
> *"Configure clawjournal to scan only claude and codex, exclude the `scratch` project, and always redact the string `acme-internal`."* — narrow scope after the fact. Subsequent scans pick up new settings automatically.

### 3. Scan

Reads your local session files into a SQLite DB and runs a per-session findings pipeline (secrets engine + PII engine). Findings are stored as hashed references — plaintext is never persisted.

**Shell:**

```bash
clawjournal scan
```

The workbench daemon (`clawjournal serve`) also scans continuously in the background.

**Skill prompt:** *"scan my sessions again"* (a first scan also runs as part of `setup clawjournal`).

### 4. Triage

Approve sessions worth keeping, block the rest. Happens in the workbench (Sessions page) or the CLI.

**Shell:**

```bash
clawjournal serve                                    # workbench UI — the primary review surface
# or directly in the terminal:
clawjournal inbox --json --limit 20                  # list sessions
clawjournal search "refactor auth" --json            # full-text search
clawjournal approve <session_id> --reason "clean"    # approve
clawjournal block <session_id> --reason "private"    # block
clawjournal shortlist <session_id>                   # mark for deeper review
```

**Skill prompt:** *"Open clawjournal and help me triage the unreviewed sessions."*

Optional hold-state controls — useful when you want to quarantine a session without blocking it (CLI only):

```bash
clawjournal hold <id> --reason "pending legal review"
clawjournal release <id>
clawjournal embargo <id> --until 2026-06-01
clawjournal hold-history <id>
```

### 5. Score

AI-assisted quality scoring on a 1–5 scale (1 = noise, 5 = excellent). Home-dir paths and usernames are anonymized before anything is sent to the judge.

**Shell:**

```bash
clawjournal score --batch --auto-triage              # batch-score; auto-blocks noise (score 1) sessions
clawjournal score-view <id>                          # show score details
clawjournal set-score <id> --quality 4               # manual override
```

**Skill prompt:** *"Score my unscored sessions."* (runs through the `clawjournal-score` skill, uses your current agent's automation CLI.)

`--auto-triage` moves sessions with quality score 1 to `blocked`. Sessions scored 2–5 stay visible for you to decide.

By default scoring uses the current agent's automation CLI (e.g. `codex exec` inside Codex, the Claude CLI inside Claude Code). Use `--backend` to override. For Codex specifically, `codex exec` reuses saved CLI authentication by default; for automation the recommended explicit credential is `CODEX_API_KEY`.

### 6. Package & Share

Bundle approved sessions into a redacted export on disk. Uploading that bundle is a separate, opt-in step.

**Shell:**

```bash
clawjournal bundle-create --status approved          # bundle all approved sessions
clawjournal bundle-list
clawjournal bundle-view <bundle_id>                  # inspect before exporting
clawjournal bundle-export <bundle_id>                # write sessions.jsonl + manifest.json to disk
```

**Workbench:** open `clawjournal serve` and walk the Share page: **Queue → Redact → Review → Package → Done**. The Redact step layers AI-assisted PII detection on top of the scan-time findings.

**Skill prompts:**
> *"Package my approved sessions and export them locally."*
>
> *(optional)* *"Then share the bundle through the ingest service."*

Optional upload:

```bash
clawjournal verify-email you@university.edu          # one-time email verification
clawjournal share --preview --status approved        # dry-run
clawjournal bundle-share <bundle_id>                 # upload through the configured ingest service
```

Upload is gated on hold-state: only sessions in `auto_redacted` or `released` can leave the machine.

---

## Build the browser workbench

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
git clone https://github.com/kai-rayward/clawjournal.git
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
| `clawjournal score --batch --auto-triage` | AI-score sessions; auto-block noise (score 1) |
| `clawjournal bundle-create --status approved` | Bundle approved sessions |
| `clawjournal bundle-export <bundle_id>` | Export bundle to disk as `sessions.jsonl` + `manifest.json` |

### Triage & review

| Command | Description |
|---------|-------------|
| `clawjournal inbox --json --limit 20` | List sessions as JSON |
| `clawjournal search <query> --json` | Full-text search |
| `clawjournal approve <id> [id ...]` | Approve sessions |
| `clawjournal block <id> [id ...]` | Block sessions |
| `clawjournal shortlist <id> [id ...]` | Shortlist sessions |
| `clawjournal score --batch --limit 20` | AI-score up to 20 sessions |
| `clawjournal score-view <id>` | View score details |
| `clawjournal set-score <id> --quality <1-5>` | Manually set a quality score |

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
| `clawjournal bundle-export <bundle_id>` | Export bundle to disk |
| `clawjournal bundle-share <bundle_id>` | Upload via configured ingest service |

### Quick share

| Command | Description |
|---------|-------------|
| `clawjournal recent` | Show recent sessions (auto-scans if stale) |
| `clawjournal recent --source openclaw --since today` | Filter by source and time |
| `clawjournal card <id>` | Generate a share card for a session |
| `clawjournal card <id> --depth workflow` | Workflow-only card (safe for public channels) |
| `clawjournal card <id> --depth full` | Full card with redacted content |

### Optional upload

| Command | Description |
|---------|-------------|
| `clawjournal verify-email you@university.edu` | Verify a `.edu` email for upload authorization |
| `clawjournal share --preview --status approved` | Preview what would be shared without uploading |
| `clawjournal share --status approved` | Create a bundle and upload through the ingest service |

### Configuration

| Command | Description |
|---------|-------------|
| `clawjournal config --exclude "a,b"` | Add excluded projects (appends) |
| `clawjournal config --redact "str1,str2"` | Add strings to always redact (appends) |
| `clawjournal config --redact-usernames "u1,u2"` | Add usernames to anonymize (appends) |
| `clawjournal list` | List all projects with exclusion status |
| `clawjournal status` | Show current stage and next steps (JSON) |
| `clawjournal update-skill <agent>` | Install/update the clawjournal skill for an agent |
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
- **`scan` already redacts.** Secrets and PII findings are computed and stored as hashed references at scan time. For additional LLM-PII review, use the workbench Share page. The legacy `--pii-review` / `--pii-apply` CLI path still works for sanitizing already-exported files.
- **Hold-state gates uploads.** Sessions in `pending_review` or active `embargoed` cannot be shared; `auto_redacted` (default) and `released` can.
- **Large exports take time** — 500+ sessions may take 1–3 minutes.
- **Virtual environment recommended** — modern Linux (and some macOS setups) block system-wide pip installs. Use a venv to avoid issues.

</details>

## Acknowledgments

ClawJournal builds on early work from [dataclaw](https://github.com/peteromallet/dataclaw) by [@peteromallet](https://github.com/peteromallet).

## License

Apache-2.0
