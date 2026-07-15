# Architecture

## Recurring hosted sharing

Recurring sharing is capability-gated and remains unavailable by default. A successful manual hosted receipt is a prerequisite, but its bundle-specific credential is never reused. Enrollment exchanges a fresh verified identity for a server enrollment, authorization revision, and separate active/recovery credentials while snapshotting exact sources, projects, privacy profile, terms, and a future-only server timestamp.

Claude Code and Codex `SessionStart` hooks only perform a local due check and detach the one-shot runner. The runner owns an OS-released lock, strictly refreshes enrolled sources, and selects at most five stable revisions through the same candidate service used by status and preview. Packaging reuses `shares`/`share_sessions`, repeats release and revision gates before AI and egress, applies both TruffleHog gates, then persists and fsyncs the exact ZIP, SHA-256, submission ID, generation, and pseudonymous revision keys before networking. Retries reuse identical bytes and identity; ambiguous requests use the recovery credential for receipt lookup. Pause/disable increment generation so changes before the submitting boundary win.

ClawJournal is a local-first Python application for reviewing, scoring, redacting, and exporting coding-agent conversation traces.

## High-Level Layout

```text
clawjournal/
  cli.py
  config.py
  pricing.py
  prompt_sync.py
  export/
  parsing/
  prompts/
  redaction/
  scoring/
  web/frontend/
  workbench/
skills/
plugins/clawjournal/
tests/
```

## Main Components

- `clawjournal/parsing/`: discovers projects and parses session logs from supported tools.
- `clawjournal/redaction/`: anonymizes paths/usernames, redacts secrets, and runs optional AI-assisted PII review.
- `clawjournal/scoring/`: prepares sessions for judge-style scoring and stores structured score outputs.
- `clawjournal/workbench/`: local SQLite-backed workbench API and browser UI server.
- `clawjournal/export/`: renders export formats such as JSONL and Markdown.
- `clawjournal/prompts/`: canonical runtime prompt assets used by scoring and PII review.

## Data Flow

The core flow is:

1. discover sessions
2. parse them into a normalized internal shape
3. index and review locally
4. optionally score and redact
5. export or bundle-share the sanitized result

## Frontend

The browser workbench lives in `clawjournal/web/frontend` and is built with Vite.

- The built assets are served by `clawjournal/workbench/daemon.py`.
- The frontend build is not yet automated into Python packaging.
- Public source installs therefore require a one-time:

```bash
cd clawjournal/web/frontend
npm install
npm run build
```

## Skills And Plugin Wrapper

The repo keeps `skills/` as the single source of truth for user-facing skills.

- `npx skills add rayward-external/clawjournal` reads from the root `skills/` layout.
- Claude plugin distribution uses a thin wrapper under `plugins/clawjournal/`.
- `plugins/clawjournal/skills` is a symlink back to the root `skills/` directory so both channels share the same content.

## Sharing Model

Supported public path:

- `clawjournal bundle-export` writes a redacted bundle to disk.

Optional self-hosted path:

- Hosted research submission happens from the local workbench Submit step after email verification and consent.
- `clawjournal bundle-share` can upload to a self-hosted ingest backend only when `CLAWJOURNAL_INGEST_URL` is explicitly configured.

The default configuration is local-first and does not require any hosted backend.

### Trace revision contract

`session_id` is the stable identity of a source trace, including traces that continue growing after an upload. Each normalized message snapshot also carries:

- `revision_hash`: `sha256:<hex>` over the canonical normalized `messages` array.
- `replaces_revision_hash`: the last successfully uploaded revision for that `session_id`, or `null` for its first upload.

Both fields appear on the exported JSONL row and its manifest session entry. A receiver must upsert by `session_id`, treat an identical `revision_hash` as idempotent, replace only when `replaces_revision_hash` matches its current revision, and reject a stale predecessor instead of overwriting newer content.

Schema-v6 migration gives an unreadable pre-v6 blob an opaque `legacy:<hex>` baseline and assigns the same value to its historical successful share. Receivers must compare this value exactly; they should not attempt to derive or validate legacy content from it.

See [PRIVACY.md](PRIVACY.md) for the full redaction and upload model.
