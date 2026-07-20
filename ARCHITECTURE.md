# Architecture

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
- `clawjournal/auto_upload.py`: SQLite-owned recurring authorization state, candidate selection, cadence, exact-artifact sealing, crash recovery, and the fail-closed runner.
- `clawjournal/auto_upload_client.py` and `auto_upload_credentials.py`: the typed hosted v1 protocol and purpose-separated private credential boundary.
- `clawjournal/agent_hooks.py`: semantics-preserving Claude Code/Codex `SessionStart` configuration plus the bounded due-check adapter.

## Data Flow

The core flow is:

1. discover sessions
2. parse them into a normalized internal shape
3. index and review locally
4. optionally score and redact
5. export or bundle-share the sanitized result

When recurring sharing is explicitly enabled, `SessionStart` only performs a bounded due check and detaches the runner. The runner strictly refreshes the enrolled sources, selects at most five future eligible revisions, packages through the same redaction path, seals and hashes the exact ZIP, persists its recovery ledger, repeats mutable gates, and then crosses the atomic `submitting` boundary. An ambiguous request is reconciled by receipt lookup and can retry only the identical ZIP and client submission ID.

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

Optional hosted recurring path:

- It remains unavailable unless hosted discovery advertises protocol v1 and the participant has a successful manual receipt.
- The local SQLite singleton owns mode, generation, accepted exact scope/profile, cadence, health, and run overlay. Private files own active/recovery credentials; `config.json` never does.
- The hosted service owns versioned recurring authorization, credential hashes, exact-byte idempotency, cross-enrollment duplicate-revision rejection, storage, and receipts.
- Public discovery can close or go dark without stranding recovery: fixed, origin-pinned receipt and revocation routes remain recovery-only. No content egress occurs while the capability is unavailable.

The default configuration is local-first and does not require any hosted backend.

### Trace revision contract

`session_id` is the stable identity of a source trace, including traces that continue growing after an upload. Each normalized message snapshot also carries:

- `revision_hash`: `sha256:<hex>` over the canonical normalized `messages` array.
- `replaces_revision_hash`: the last successfully uploaded revision for that `session_id`, or `null` for its first upload.

Both fields appear on the exported JSONL row and its manifest session entry. A receiver must upsert by `session_id`, treat an identical `revision_hash` as idempotent, replace only when `replaces_revision_hash` matches its current revision, and reject a stale predecessor instead of overwriting newer content.

Schema-v6 migration gives an unreadable pre-v6 blob an opaque `legacy:<hex>` baseline and assigns the same value to its historical successful share. Receivers must compare this value exactly; they should not attempt to derive or validate legacy content from it.

See [PRIVACY.md](PRIVACY.md) for the full redaction and upload model.
