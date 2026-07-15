# Privacy and sharing

ClawJournal is designed to be usable without uploading anything.

## What stays local

- `clawjournal scan`, `serve`, `inbox`, `search`, `score`, `export`, and `bundle-export` run locally.
- The browser workbench is local. If you install from source, `clawjournal serve` opens your own machine at `localhost:8384`.
- `bundle-export` writes files to disk. It does not contact a server.
- If you never use the workbench Submit step, and never configure `CLAWJOURNAL_INGEST_URL` or run `bundle-share`, nothing is uploaded.
- Weekly automatic sharing remains off unless you explicitly enroll after a hosted submission or in Settings/CLI. Once enrolled, due background runs can upload without another prompt until you pause or disable the service.
- If you are explicitly enrolled in OpenRefinery Agent Failure Sharing, the optional agent hook only shows a local reminder and can open the existing Share workflow. The hook does not read transcripts, package bundles, or upload data by itself.

## Automatic redaction

Local session views (the workbench UI at `localhost:8384`) show session content as it was recorded, including your own home-directory paths and username. Redaction runs at the points where data leaves your machine or goes into an LLM prompt:

- the Share **Redact** step (step 2) and any bundle/export command
- the AI scoring pipeline, before the judge is called

At those boundaries, ClawJournal redacts several classes of sensitive data:

| Type | Result |
|------|--------|
| Home-directory paths | Replaced with `[REDACTED_PATH]` |
| Usernames | Replaced with `[REDACTED_USERNAME]` |
| Email addresses | Replaced with `[REDACTED_EMAIL]` |
| API keys and tokens | Replaced with typed placeholders such as `[REDACTED_OPENAI_KEY]`, `[REDACTED_GITHUB_TOKEN]`, `[REDACTED_JWT]` |
| Database URLs and password-like assignments | Replaced with typed placeholders |
| Private keys | Replaced with `[REDACTED_PRIVATE_KEY]` |
| Public IP addresses | Replaced with `[REDACTED_IP]` |
| Suspicious high-entropy strings | Replaced with `[REDACTED_SECRET]` |
| Export timestamps | Coarsened to hour-level precision |

You can also add custom strings and extra usernames to redact through `clawjournal config`.

## AI-assisted PII review

Automatic secret redaction is useful, but it is not perfect. For higher confidence, run:

```bash
clawjournal export --pii-review --pii-apply
```

That second layer can catch identifying text such as:

- names
- usernames and user IDs
- org names
- private project names
- private URLs and domains
- phone numbers and addresses
- device names and location-like text

Review is still your responsibility before publishing anything.

## Mandatory post-redaction scan (TruffleHog)

Every share export runs an independent secrets scanner — [TruffleHog](https://github.com/trufflesecurity/trufflehog) — on the already-redacted `sessions.jsonl` before the export is considered complete. TruffleHog is a separate project with its own ~800 credential detectors and optional live-verification step. It acts as a backstop: if our layered redaction misses something, TruffleHog's independent detectors should catch it.

The scan is mandatory. Outcomes:

- **clean** — export proceeds, summary embedded in `manifest.json` under `redaction_summary.trufflehog`, full report written to `trufflehog.json`.
- **any finding** (verified, unverified, or unknown) — export is blocked. The share's status is **not** advanced; the directory is preserved with `manifest.blocked=true` and masked examples so you can debug.
- **binary not installed** — export is blocked with an install hint.

Install:

```bash
# macOS / Linux / Windows (x86-64 and ARM64) — pinned version, sha256-verified
# against the official release, installed to ~/.clawjournal/bin (preferred over PATH):
clawjournal trufflehog install

# Or install it yourself:
brew install trufflehog          # macOS
# Linux / Windows: https://github.com/trufflesecurity/trufflehog#floppy_disk-installation
```

The managed copy is downloaded from TruffleHog's own GitHub release artifacts at your explicit request and is only ever invoked as a subprocess. `clawjournal trufflehog status` shows which binary the gate will use.

For the upload path, the scan runs at least **twice at share time**: once inside `export_share_to_disk` on the merged `sessions.jsonl`, and again after the final PII pass rewrites the file. Either scan finding something aborts the upload. The final PII pass always runs deterministic rules. If you opt in to AI-assisted review for a bundle, it also reviews sessions in a small bounded worker pool and falls back to deterministic PII rules when an AI backend errors or times out; the manifest records `redaction_summary.pii_review.ai_enabled` plus `redaction_summary.coverage.full` vs. `rules_only`. TruffleHog also participates as a deterministic findings engine at scan-ingest time, so a session's existing `findings` rows already carry its detections before any share step — the share-time gates are the final check, not the first.

One detector is excluded at the TruffleHog layer: **`refiner`** (refiner.io user-feedback platform). Its pattern is "the word 'refiner' followed by a UUID", which false-positives on any project name containing that substring paired with the UUIDs present throughout Claude/Codex session JSON. Verification against refiner.io's own API correctly returns `unverified` for those matches, so they are never real leaks. Every other TruffleHog detector remains active and blocking.

An escape hatch exists for CI and development: setting `CLAWJOURNAL_SKIP_TRUFFLEHOG=1` disables the gate. This is recorded in the manifest (`redaction_summary.trufflehog.bypassed=true`) so reviewers can tell scanned shares from bypassed ones. Do not use it for real shares.

## What a local bundle contains

`clawjournal bundle-export <bundle_id>` writes:

- `sessions.jsonl`
- `manifest.json`
- `trufflehog.json` (scan report)

Depending on how you export, bundle content can include user messages, assistant messages, tool calls, model metadata, token counts, and timestamps. Extended thinking can be excluded from regular exports with `--no-thinking`.

## Optional upload flow

Uploading is a separate path from local export.

- Hosted research submission uses the local workbench Submit step by default. The browser talks to the local daemon, the daemon sends the finalized zip to Rayward's hosted API, and the hosted service returns a receipt ID. Self-hosters can override the destination with `CLAWJOURNAL_SHARE_URL`; setting `CLAWJOURNAL_SHARE_URL=` disables hosted submission.
- Advanced self-hosted ingest upload is disabled unless `CLAWJOURNAL_INGEST_URL` is configured.
- The ingest and hosted-share URLs must use `https://`, except for `localhost` and `127.0.0.1` during local development.
- Self-hosted ingest upload uses `clawjournal bundle-share <bundle_id>`.
- You can inspect what would be packaged with `clawjournal share --preview --status approved`.

### Email verification

If you use the upload flow, ClawJournal requires:

```bash
clawjournal verify-email you@university.edu
clawjournal verify-email you@university.edu --code <CODE>
```

The academic email is used for verification and short-lived upload authorization. It is not included in the exported bundle itself, and the upload token stays in the local daemon rather than browser JavaScript.

### Automatic weekly sharing

Automatic sharing is a separate, persistent opt-in available only after one successful hosted manual share. Enrollment records separately versioned recurring consent, the exact included source/project snapshot, cadence, server enrollment identity, authorization revision, run state, and receipts. The server returns distinct opaque active and recovery credentials. The active credential can upload; the recovery credential can only revoke enrollment and look up receipts. Neither is exposed to browser JavaScript or embedded in agent hook commands.

The first automatic checkpoint is the server-accepted enrollment time. Each due cycle selects at most five future eligible revisions from the exact snapshot. Stored scores only order candidates; no synchronous scoring occurs. Append-only traces must be unchanged for 24 hours unless their source provides a tested close marker. Local anonymization and deterministic redaction remain independent of optional AI-PII review, and both mandatory TruffleHog scans still gate upload. Settings and `clawjournal auto-upload preview` show the next capped set; `pause` retains enrollment, while `disable` immediately removes hooks and active upload authority and keeps recovery authority only while revocation or receipt reconciliation remains pending.

## Practical guidance

- If you only want local review, stop at `scan`, `serve`, `export`, or `bundle-export`.
- If you want to distribute data yourself, use `bundle-export` and share the files however you choose.
- If you want hosted research upload, use the workbench Submit step so the current consent terms are shown before upload.
- If you want self-hosted network upload, configure ingest explicitly and treat that as a separate opt-in step.

For security reporting and threat-model scope, see [SECURITY.md](SECURITY.md).
