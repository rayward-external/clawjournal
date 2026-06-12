# ClawJournal

Review and curate your coding-agent session traces — 100% locally. ClawJournal scans session logs from Claude Code, Claude Desktop, Codex, Gemini CLI, OpenCode, OpenClaw, Kimi CLI, and Cline, anonymizes secrets and personal information, and gives you a browser workbench to review everything before it ever leaves your machine.

## Copy-paste prompts

Use one of these prompts in Claude Code, Codex, Cursor, or another coding assistant.

> [!TIP]
> **Ordinary workflow — browser workbench**
>
> ```text
> Install or update ClawJournal from https://github.com/rayward-external/clawjournal. Read its README and follow it for my operating system. Then help me share my recent coding-agent sessions using the browser workbench. If needed, configure source `all`, confirm projects, and scan first. Open the local workbench with `clawjournal serve`, guide me through Share -> Queue -> Redact -> Review -> Package -> Submit, and stop for my consent before upload. Do not use `clawjournal bundle-share`. If hosted upload is unavailable, save the ZIP and tell me where it is so I can upload it at https://data.rayward.ai/share.
> ```

> [!IMPORTANT]
> **CLI-only workflow — remote terminal or SSH**
>
> ```text
> Install or update ClawJournal from https://github.com/rayward-external/clawjournal. Read its README and follow it for my operating system. Then help me share my recent coding-agent sessions from this terminal. If needed, configure source `all`, confirm projects, and scan first. Then run `clawjournal share --interactive --weekly`; guide me through selecting sessions, reviewing redactions, and consenting. Do not use `clawjournal bundle-share`. If hosted upload is unavailable, save the ZIP and tell me where it is so I can upload it at https://data.rayward.ai/share.
> ```

## Install or update (no coding required)

The same prompt installs ClawJournal the first time and updates it later. Run it before you package or submit a bundle — an out-of-date copy is the #1 cause of submission errors.

Open any AI coding assistant — **Claude Code**, **Codex**, **Cursor**, **OpenCode**, **Gemini CLI**, or similar — and paste this:

> *Install or update ClawJournal from https://github.com/rayward-external/clawjournal. Read its README and follow it for my operating system. If it's already installed, pull the latest code from the public GitHub repo and rebuild the browser workbench. Install any missing prerequisites (git, Python 3.10+, Node.js). When you're done, run `clawjournal status` and tell me the version.*

The AI detects your OS, installs what it needs (git, Python, Node.js), runs the installer, and confirms it works.

**What to expect:**

- **Lots of permission prompts** — click "Allow" each time (expect 10–25). Mac may ask for your computer password; that's macOS, not the AI.
- **Quiet stretches are normal** — some downloads run 30–90 seconds with no visible progress. Total time is usually 2–10 minutes.
- **Success looks like** `[ok] ClawJournal 0.1.15 installed.` (version may differ).
- **If something doesn't work** — tell your AI "it didn't work, please fix it" or "try a different approach," and it will retry. A scary-looking permission prompt is normal; "Allow" is safe.

When it's done, your AI gives you a web address like `http://localhost:8384`. Copy it into your browser's address bar and press Enter — the workbench opens locally on your own computer; nothing is uploaded.

## Your data stays local

- `scan`, `serve`, `inbox`, `search`, `score`, `export`, and `bundle-export` all run on your own computer. The review UI opens on `localhost:8384` — no account, no cloud service.
- `scan` auto-runs a secrets + PII findings pipeline per session. Findings are stored as hashed references in your local SQLite DB — plaintext is never persisted.
- Uploading is a separate, opt-in flow. If you never use the workbench Submit step and never run `bundle-share` against a self-hosted ingest endpoint, nothing is sent anywhere.

## If you decide to share

Sharing is fully opt-in and separate from local review. On export, ClawJournal re-applies regex redaction (paths, usernames, emails, API keys, tokens, private keys, and your configured strings) on top of the scan-time findings — to both the session traces *and* the `manifest.json` metadata. The workbench Share flow can add optional AI-assisted PII review; home-dir paths and usernames are anonymized locally before anything is sent to an AI backend. A mandatory TruffleHog secrets gate then runs on the redacted output and blocks the share if it finds anything or the binary is missing (not required for local-only use).

See [PRIVACY.md](PRIVACY.md) for the full redaction list and the sharing paths.

## Quickstart

<details>
<summary><b>Show manual install (for AI agents and developers)</b></summary>

**Prerequisites** — `git` + Python 3.10+ are required; Node.js 18+ is required only for the browser workbench. Skip any line whose tool is already installed:

```bash
# macOS (install Homebrew first if needed; NONINTERACTIVE=1 skips the hanging RETURN prompt):
NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv)"
brew install git python              # workbench (optional): brew install node

# Debian / Ubuntu (drop `sudo` if root in a container):
sudo apt update && sudo apt install -y git curl python3-full python3-venv
# Workbench (optional). On Ubuntu 22.04 or older, distro Node is too old for Vite — use NodeSource:
curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo bash - && sudo apt install -y nodejs

# Windows (PowerShell). Flags suppress interactive prompts:
winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements --scope user
winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements --scope user
winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements   # workbench (optional)
# Refresh PATH in the current PowerShell session:
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
```

**Install — macOS / Linux / WSL / Git Bash:**

```bash
git clone https://github.com/rayward-external/clawjournal.git ~/clawjournal
cd ~/clawjournal
./scripts/install.sh --with-frontend       # or: sh scripts/install.sh --with-frontend
```

**Install — native Windows PowerShell** (use `powershell` if `pwsh` isn't installed):

```powershell
git clone https://github.com/rayward-external/clawjournal.git "$HOME\clawjournal"
Set-Location "$HOME\clawjournal"
pwsh -ExecutionPolicy Bypass -File .\scripts\install.ps1 -WithFrontend
```

`--with-frontend` / `-WithFrontend` builds the workbench at `localhost:8384`. Without Node.js the script warns and continues CLI-only (`serve` will 404 until you install Node and re-run). The script prints `[ok] ClawJournal <version> installed.` on success.

**Verify** — the CLI lives at `~/.clawjournal-venv/bin/clawjournal` (POSIX) or `$HOME\.clawjournal-venv\Scripts\clawjournal.exe` (Windows); `status` prints JSON with `"stage"` and `"stage_number"`:

```bash
~/.clawjournal-venv/bin/clawjournal status                       # POSIX
& "$HOME\.clawjournal-venv\Scripts\clawjournal.exe" status       # PowerShell
```

To call it as plain `clawjournal`, put the venv bin on `PATH` (add to your shell profile to persist):

```bash
export PATH="$HOME/.clawjournal-venv/bin:$PATH"                  # POSIX
$env:Path = "$HOME\.clawjournal-venv\Scripts;" + $env:Path       # PowerShell
```

**Staying current** — installs auto-update via a throttled background fast-forward from `rayward-external/clawjournal` (skipped on dirty trees, diverged histories, or non-`main` branches; opt out with `CLAWJOURNAL_NO_AUTO_UPDATE=1`). Run a synchronous update anytime with `clawjournal selfupdate`. A `pipx install clawjournal` fallback exists for firewalled environments, but the PyPI wheel lags the GitHub source by many releases.

</details>

## End-to-end flow

Six stages take you from indexing local sessions to (optionally) sharing a redacted bundle. Each stage starts with the natural-language prompt for your AI; shell commands are tucked behind expandable sections.

```
 Install ──► Configure ──► Scan ──► Triage ──► Score ──► Package & Share
    1            2           3          4          5              6
```

Optionally, `npx skills add rayward-external/clawjournal` installs three skills so prompts route automatically: **clawjournal-setup** → install/scan, **clawjournal** → triage/share, **clawjournal-score** → score. The commands below work the same without them. If you run a bare `clawjournal ...` snippet without the venv on `PATH`, prefix it with `~/.clawjournal-venv/bin/` (POSIX) or `$HOME\.clawjournal-venv\Scripts\` (Windows).

### 1. Install

Use [Quickstart](#quickstart) above (or the install prompt at the top). TruffleHog is **not** required for local use — it's only needed at Stage 6, where every export runs an independent secrets scan and blocks if TruffleHog is missing or finds anything. Your AI installs it when you reach Stage 6.

<details>
<summary><b>Show TruffleHog install commands</b></summary>

```bash
brew install trufflehog                                    # macOS
curl -sSfL https://raw.githubusercontent.com/trufflesecurity/trufflehog/main/scripts/install.sh | sh -s -- -b /usr/local/bin   # Linux
# Windows: download a release binary from https://github.com/trufflesecurity/trufflehog/releases
```

</details>

### 2. Configure

Tell ClawJournal which agents to scan and what to exclude or redact.

> *"Configure clawjournal — scan all sources with defaults, no exclusions."*
> Or: *"Configure clawjournal to scan only claude and codex, exclude the `scratch` project, and always redact `acme-internal`."*

<details>
<summary><b>Show shell commands</b></summary>

```bash
clawjournal config --source all                   # claude | codex | gemini | opencode | openclaw | kimi | custom | all
clawjournal list                                  # see discovered projects
clawjournal config --exclude "project1,project2"  # optional (appends)
clawjournal config --redact "string1,string2"     # optional custom redactions (appends)
clawjournal config --redact-usernames "handle1"   # optional username anonymization (appends)
clawjournal config --confirm-projects             # lock in project selection (required before export)
```

`--exclude`, `--redact`, and `--redact-usernames` always append; they never overwrite. Source + project confirmation are both required before export.

</details>

### 3. Scan

Reads your local session files into a private database, detecting secrets and PII as it goes. Plaintext is never saved — only hashed references.

> *"scan my coding-agent sessions"* (a first scan runs automatically as part of *"setup clawjournal"*).

<details>
<summary><b>Show shell command</b></summary>

```bash
clawjournal scan
```

The workbench daemon (`clawjournal serve`) also scans continuously in the background.

</details>

### 4. Triage

Approve the conversations you want to keep and block the rest. The workbench at `localhost:8384` is the easiest place. You can also put a session on **hold** (set aside pending review) or **embargo** it (auto-release at a date).

> *"Open clawjournal and help me triage my unreviewed sessions."*

<details>
<summary><b>Show shell commands</b></summary>

```bash
clawjournal serve                                    # workbench UI — the primary review surface
clawjournal inbox --json --limit 20                  # list sessions
clawjournal search "refactor auth" --json            # full-text search
clawjournal approve <id> --reason "clean"            # approve
clawjournal block <id> --reason "private"            # block
clawjournal shortlist <id>                           # mark for deeper review
clawjournal hold <id> --reason "pending legal"       # set aside (blocks share)
clawjournal release <id>                             # release a held session
clawjournal embargo <id> --until 2026-06-01          # time-lock
clawjournal hold-history <id>                        # full hold timeline
```

</details>

### 5. Score

AI-assisted scoring records two labels in one pass: a legacy productivity score (`ai_quality_score`) and the primary failure-value score (`ai_failure_value_score`), which prioritizes traces where a frontier agent made, recovered from, or failed to recover from a meaningful mistake. Home-folder paths and usernames are anonymized before anything is sent to the judge.

> *"Score my unscored ClawJournal sessions and auto-block the noise."*

<details>
<summary><b>Show shell commands</b></summary>

```bash
clawjournal score --batch --source failure-corpus --auto-triage  # score scope; auto-block productivity-1 sessions only when failure value is 1-2 (failure value 3+ stays visible)
clawjournal score --batch --source failure-corpus --window 7d     # restrict to last N days
clawjournal score-view <id>                          # show score details
clawjournal set-score <id> --failure-value 4 --failure-evidence "User corrected a fabricated API call"
clawjournal set-score <id> --quality 4               # legacy productivity override
```

Scoring uses the current agent's automation CLI by default (`codex exec` in Codex, the Claude CLI in Claude Code); backends are `claude`, `codex`, `hermes`, `openclaw` (override with `--backend`). Claude Code-backed AI features default to `claude-sonnet-4-6`, Codex-backed AI features default to `gpt-5.4-mini`, and the other backends use their own agent defaults unless you pass `--model`. For Codex specifically, run `codex login` or set `CODEX_API_KEY` for headless scoring. The workbench can also auto-score share-ready traces in the background, but only after you confirm a backend once; confirm it headlessly with `clawjournal config --scorer-backend <backend>` (`none` clears it).

</details>

### 6. Package & Share

Packaging is **100% local** — it writes a redacted ZIP to your computer. Uploading is a **separate, opt-in step**. Only sessions you explicitly add and confirm are ever uploaded; `pending_review` and active `embargoed` sessions are blocked, while `auto_redacted` (default) and `released` are allowed. Redaction (your strings, paths, usernames, secrets) is applied to everything in the bundle — traces *and* `manifest.json`.

> *"Package my approved ClawJournal sessions and export them to a file on my computer."*

**Submit through the workbench (recommended).** Run `clawjournal serve`, click **Share**, and walk **Queue → Redact → Review → Package**. Redaction always runs on your machine first. ClawJournal then lands you on **one** of two final steps automatically:

| You land on… | What it means | What to do |
|--------------|---------------|------------|
| **Submit** | Hosted submissions are open | Verify your academic email (a code is emailed; confirm it on a second step), review consent, click **Submit to ClawJournal Research**. The finalized ZIP uploads straight from your computer; a receipt is saved locally. |
| **Done** | Hosted submissions are closed | Click **Download zip**, then upload that file at **[data.rayward.ai/share](https://data.rayward.ai/share)** when submissions reopen. |

Seeing **Done** instead of **Submit** is normal — submissions just aren't open right now.

**Sharing from a remote machine or SSH session.** If the browser workbench is inconvenient, use the terminal wizard:

```bash
clawjournal share --interactive --weekly
# or: clawshare --weekly
```

It lists shareable traces, prioritizes AI-scored high-failure-value sessions, shows the redacted preview, asks for consent, then uploads when hosted submission is available or saves a ZIP for manual upload. Useful filters: `--all`, `--source codex`, `--source claude`, `--search "text"`, and `--ai-pii-review` for the optional AI PII pass.

Or paste this into Claude Code, Codex, or another AI coding assistant on the remote machine:

> *Install or update ClawJournal from https://github.com/rayward-external/clawjournal. Read its README and follow it for my operating system. Then help me share my recent coding-agent sessions from this terminal. If needed, configure source `all`, confirm projects, and scan first. Then run `clawjournal share --interactive --weekly`; guide me through selecting sessions, reviewing redactions, and consenting. Do not use `clawjournal bundle-share`. If hosted upload is unavailable, save the ZIP and tell me where it is so I can upload it at https://data.rayward.ai/share.*

> ⚠️ **`clawjournal bundle-share` is NOT the Rayward path.** It only uploads to a **self-hosted** ingest server you configure via `CLAWJOURNAL_INGEST_URL`; without it, it reports *"Hosted sharing is not configured."* Rayward / STEM Data Program participants should ignore it and use the workbench above.

<details>
<summary><b>Show shell commands</b></summary>

```bash
# Recommended — browser workbench:
clawjournal serve
# open http://localhost:8384/share → Queue → Redact → Review → Package → Submit/Done

# Alternative — package via CLI, then upload the zip in a browser:
clawjournal bundle-create --status approved          # bundle all approved sessions
clawjournal bundle-list
clawjournal bundle-view <bundle_id>                  # inspect before exporting
clawjournal bundle-export <bundle_id> --zip          # writes an export folder + an uploadable zip
# upload the printed zip_path at https://data.rayward.ai/share when submissions are open

# Self-hosted ingest ONLY (not the Rayward path; requires CLAWJOURNAL_INGEST_URL):
clawjournal share --preview --status approved        # dry-run
clawjournal bundle-share <bundle_id>
```

**Empty Share queue, or `error: unrecognized arguments: --zip`?** These usually mean the workbench/CLI is stale: re-run `./scripts/install.sh --with-frontend` to rebuild the **frontend** (a CLI-only update won't fix the queue). Then release any holds/embargoes and check `clawjournal config --exclude`.

**Opt-in AI PII review** (workbench Redact step) adds an extra AI pass on top of the always-on deterministic redaction. Tune with `CLAWJOURNAL_UPLOAD_PII_WORKERS=1` (serialize) or `CLAWJOURNAL_UPLOAD_PII_TIMEOUT_SECONDS=90` (longer per trace).

</details>

## Command reference

<details>
<summary><b>All commands</b></summary>

### Essential

| Command | Description |
|---------|-------------|
| `clawjournal scan` | Index local sessions + run findings pipeline |
| `clawjournal serve` | Open workbench UI at localhost:8384 (`--remote` prints an SSH tunnel) |
| `clawjournal config --source all` | Select source scope (required) |
| `clawjournal config --confirm-projects` | Confirm project selection (required before export) |
| `clawjournal config --scorer-backend codex` | Confirm background scoring backend (`none` clears) |
| `clawjournal score --batch --source failure-corpus --auto-triage` | AI-score failure-value scope; auto-block productivity-1 noise only when failure value is 1-2 (failure value 3+ stays visible) |
| `clawjournal bundle-create --status approved` | Bundle approved sessions |
| `clawjournal bundle-export <bundle_id> --zip` | Package approved sessions into a redacted ZIP on disk |

### Triage & review

| Command | Description |
|---------|-------------|
| `clawjournal inbox --json --limit 20` | List sessions as JSON |
| `clawjournal search <query> --json` | Full-text search |
| `clawjournal approve <id>` / `block <id>` / `shortlist <id>` | Triage decisions (accept one or more ids) |
| `clawjournal recent [--source <s> --since today]` | Recent sessions (auto-scans if stale) |

### Score

| Command | Description |
|---------|-------------|
| `clawjournal score --batch --source failure-corpus [--auto-triage] [--window 7d] [--limit 20]` | AI-score the failure-value scope (`--auto-triage` blocks productivity-1 sessions only when failure value is 1-2; failure value 3+ stays visible) |
| `clawjournal score-view <id>` | View score details |
| `clawjournal set-score <id> --failure-value <1-5>` | Set failure-value score (4–5 requires `--failure-evidence`) |
| `clawjournal set-score <id> --quality <1-5>` | Set the legacy productivity score |

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
| `clawjournal findings <id> --accept <ref>` / `--ignore <ref>` | Decide a finding (`--accept-all` / `--ignore-all` for bulk) |
| `clawjournal allowlist list` / `add ...` / `remove <id>` | Manage the global allowlist (entities hashed locally) |

### Bundles & share

| Command | Description |
|---------|-------------|
| `clawjournal bundle-list` / `bundle-view <id>` | List bundles / view details |
| `clawjournal bundle-export <bundle_id> --zip` | Export a redacted ZIP for manual upload at `https://data.rayward.ai/share` |
| `clawjournal bundle-share <bundle_id>` | Self-hosted ingest ONLY; requires `CLAWJOURNAL_INGEST_URL` (not the Rayward path) |
| `clawjournal verify-email you@university.edu` | Request a code for the hosted submission flow (a code is emailed; confirm with `clawjournal verify-email you@university.edu --code <CODE>`) |
| `clawjournal share --preview --status approved` | Preview what would be packaged |
| `clawjournal share --status approved [--ai-pii-review]` | Package locally + print the Share URL; hosted upload happens in the browser |
| `clawjournal share --interactive --weekly` / `clawshare --weekly` | Terminal Share wizard for remote/SSH sessions; review redactions, consent, upload or save ZIP |
| `clawjournal card <id> [--depth workflow\|full]` | Generate a share card (`workflow` is safe for public channels) |

### Configuration & maintenance

| Command | Description |
|---------|-------------|
| `clawjournal config --exclude "a,b"` / `--redact "s1,s2"` / `--redact-usernames "u1,u2"` | Add excluded projects / strings to redact / usernames to anonymize (all append) |
| `clawjournal config --ai-pii-review` / `--no-ai-pii-review` | Default AI-assisted PII review for the share flow |
| `clawjournal config --benchmark-tab` / `--no-benchmark-tab` | Show/hide the workbench Benchmark tab |
| `clawjournal config --scoring-warmup` / `--no-scoring-warmup` | Enable or decline the background AI auto-scorer |
| `clawjournal list` / `clawjournal status` | List projects with exclusion status / show current stage (JSON) |
| `clawjournal update-skill <agent>` | Install/update the clawjournal skill for an agent |
| `clawjournal selfupdate [--check] [--force]` | Fast-forward to latest from `rayward-external/clawjournal` |

### Export & sanitize (advanced)

| Command | Description |
|---------|-------------|
| `clawjournal export [--no-thinking]` | Export to local JSONL (`--no-thinking` drops thinking blocks) |
| `clawjournal export --pii-review --pii-apply` | Legacy LLM-PII path — export + AI-PII review + sanitize |
| `clawjournal pii-review` / `pii-apply` / `pii-rubric` | Legacy PII detect/apply on exported files; entity types & rules |

Each line of the exported JSONL is one session: `session_id`, `project`, `model`, `git_branch`, timestamps, a `messages` array (`role`, `content`, `thinking`, `tool_uses`), and a `stats` object (message/tool counts, input/output tokens).

**Legacy note:** `pii-review` / `pii-apply` remain for already-exported files, but deterministic detection has moved to the `findings` + `bundle-export` flow. Prefer the new path.

</details>

## Supported agents

Claude Code, Claude Desktop, Codex, Gemini CLI, OpenCode, OpenClaw, Kimi CLI, and Cline.

## Project docs

- [PRIVACY.md](PRIVACY.md) — what stays local, what gets redacted, and how optional sharing works
- [ARCHITECTURE.md](ARCHITECTURE.md) — public architecture overview
- [CONTRIBUTING.md](CONTRIBUTING.md) — contribution guidelines
- [SECURITY.md](SECURITY.md) — security reporting and threat-model scope

## Acknowledgments

ClawJournal builds on early work from [dataclaw](https://github.com/peteromallet/dataclaw) by [@peteromallet](https://github.com/peteromallet).

## License

Apache-2.0
