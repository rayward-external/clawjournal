# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development commands

```bash
python -m pip install -e ".[dev]"      # install with dev deps (pytest)
pytest                                  # full suite
pytest tests/workbench/ -v              # a single directory
pytest tests/test_cli.py::test_name     # a single test
```

Python 3.10+ is required (CI matrix: 3.10–3.13, `.github/workflows/test.yml`).

Frontend (only when touching the workbench UI — the PyPI wheel ships the pre-built assets):

```bash
cd clawjournal/web/frontend && npm install && npm run build
```

The CI `smoke` job verifies that the built wheel contains `clawjournal/web/frontend/dist/index.html`. Touching the frontend without rebuilding will silently regress the wheel.

CLI entry point: `clawjournal = clawjournal.cli:main` (see `pyproject.toml`). README.md has the full user-facing command reference.

## Architecture

ClawJournal is a local-first tool that scans coding-agent session logs, indexes them into SQLite, runs a findings (secrets + PII) pipeline, and lets the user triage, score, redact, and export/share. Everything defaults to local; sharing is a separate opt-in step.

### Package layout (`clawjournal/`)

- `cli.py` — single-file argparse CLI (~4k lines) dispatching all subcommands.
- `config.py` — `~/.clawjournal/config.json` read/write, TypedDict schema, migrations. `CONFIG_DIR` (`~/.clawjournal/`) is the well-known install root used by the rest of the code.
- `paths.py` — atomic bootstrap of `~/.clawjournal/hash_salt` (salts findings' `entity_hash`) and `~/.clawjournal/api_token` (bearer token for the loopback daemon API). Write-then-link pattern prevents races between CLI and daemon on a fresh install.
- `parsing/` — per-agent source discovery + normalization. `parser.py` defines each agent's on-disk location (`CLAUDE_DIR`, `CODEX_DIR`, `GEMINI_DIR`, `OPENCODE_DIR`, `OPENCLAW_DIR`, `KIMI_DIR`, `CURSOR_DIR`, `COPILOT_DIR`, `AIDER_*`, `CUSTOM_DIR`, `LOCAL_AGENT_DIR` for Claude Desktop) and converts raw logs to a shared session shape. `segmenter.py` handles session boundary logic.
- `capture/` — incremental ingest adapter (JSONL cursors, change detection, discovery) used by the background scanner in the daemon.
- `redaction/` — three stages:
  - `anonymizer.py` anonymizes home-dir paths + usernames (always, including before anything is sent to an AI backend).
  - `secrets.py` regex secrets detection with entropy heuristics (`_has_mixed_char_types`, `_shannon_entropy`).
  - `pii.py` optional AI-assisted PII review (`review_session_pii*`), producing `findings` and applying them.
- `findings.py` — substrate for the scan-time findings pipeline (hashed entity references; plaintext is never persisted — salt lives in `paths.py`).
- `scoring/` — judge-backed 1–5 quality scoring. `backends.py` picks a backend (Claude CLI / `codex exec` / other), `scoring.py` orchestrates, `badges.py` computes outcome/value/risk badges used in the index, `depth.py`/`insights.py` support card generation.
- `workbench/`
  - `index.py` — SQLite + FTS5 schema + all queries. `SECURITY_SCHEMA_VERSION` is bumped with gated migrations — don't rewrite historical migrations, add a new one.
  - `daemon.py` — `clawjournal serve` HTTP API (loopback, bearer-token gated) + background scanner. Serves the Vite build under `web/frontend/dist/`.
  - `findings_pipeline.py` — runs findings on scan (`run_findings_pipeline`) and backfills older sessions (`drain_findings_backfill`).
  - `card.py`, `trace_note.py` — share-card rendering and per-session markdown trace notes synced with the DB.
- `export/` — `markdown.py` (human-readable) and `training_data.py` (JSONL bundle format used by `bundle-export`).
- `prompts/agents/*/*.md` — canonical runtime prompts shipped in the wheel (referenced from `pyproject.toml` `package-data`). `prompt_sync.py` keeps mirrors in sync.
- `auto_upload.py`, `auto_upload_client.py`, `auto_upload_credentials.py` — recurring authorization/candidate/runner state machine, exact hosted v1 client, and the private purpose-separated credential store.
- `agent_hooks.py` — Claude Code/Codex `SessionStart` hook installation and the bounded, fail-open due-check adapter. Hooks never package or upload inline.
- `web/frontend/` — Vite app; **excluded from the Python package** via `[tool.setuptools.packages.find]`. Only the built `dist/` is packaged.

### Key invariants

- **Hold-state gates upload.** Only sessions in `auto_redacted` or `released` can leave the machine. `pending_review` / active `embargoed` must be blocked by any new share path. `hold`, `release`, `embargo`, `hold-history` commands maintain this in the workbench DB.
- **Source + project confirmation required before export.** `config --source` and `config --confirm-projects` must both be set; CLI blocks export otherwise.
- **Redaction runs twice on the share path.** Regex redaction is always applied on export, independently of the scan-time findings pipeline. AI-PII review is an additional layer on top, done at share time — it is not a substitute for the deterministic layers.
- **Anonymization happens before any AI call.** Home-dir paths and usernames are stripped locally before scoring or AI-PII review sends anything to a backend.
- **Appending config flags.** `--exclude`, `--redact`, `--redact-usernames` append rather than overwrite; preserve this behavior in any config edits.
- **Mandatory TruffleHog post-redaction gate.** `clawjournal/redaction/trufflehog.py` is invoked from `export_share_to_disk` (and re-invoked from the daemon upload path after the PII rewrite). Any finding or missing binary blocks the share; manifest gains `blocked=true` + `block_reason` and `shares.status` is not advanced. TruffleHog is AGPL-3.0 — we invoke it as a subprocess only, never link in-process. Tests bypass via the autouse fixture in `tests/conftest.py` which sets `CLAWJOURNAL_SKIP_TRUFFLEHOG=1`.
- **Recurring sharing is a separate authorization.** It is default-off, future-only, capped at five, exact-scope, and unavailable without hosted protocol-v2 discovery plus a successful manual receipt. Protocol v2 enrollment sends explicit (source, project) scope entries and requires a separately accepted, versioned ownership certification; the server owns the scope hash and the client pins the read-back value. Manual Share behavior must not change.
- **Recovery reuses exact bytes.** Persist/fsync/hash the exact sealed ZIP, client submission ID, included revisions, and raw fingerprints before `submitting`. Ambiguous requests reconcile receipts first and may retry only that exact ledger entry.
- **Controls win before submitting.** Generation, profile, hold, revision, source, raw-input, terms, and provider checks repeat before AI/egress. Disable removes active authority first; recovery credentials can never upload.

### Skills + plugin wrapper

`skills/` is the single source of truth for the three user-facing skills (`clawjournal-setup`, `clawjournal`, `clawjournal-score`). `plugins/clawjournal/skills` is a symlink back to `skills/` so `npx skills add` and the Claude plugin distribution share the same content — don't duplicate skill content into the plugin directory.

### Findings UI placement

Security/redaction surfaces (findings, allowlist, hold-state controls) belong in the **share** workflow, not the per-session local review. Local review should apply silent defaults only.

### Docs

- `README.md` — user-facing; the canonical command list.
- `ARCHITECTURE.md` — short public overview.
- `PRIVACY.md` — redaction list and the two sharing paths.
- `docs/` — local-only planning material, not part of the published source tree.
