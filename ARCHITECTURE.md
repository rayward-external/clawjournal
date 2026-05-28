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

See [PRIVACY.md](PRIVACY.md) for the full redaction and upload model.
