# Privacy and sharing

ClawJournal is designed to be usable without uploading anything.

## What stays local

- `clawjournal scan`, `serve`, `inbox`, `search`, `score`, `export`, and `bundle-export` run locally.
- The browser workbench is local. If you install from source, `clawjournal serve` opens your own machine at `localhost:8384`.
- `bundle-export` writes files to disk. It does not contact a server.
- If you never use the workbench Submit step, never explicitly enable Automatic uploads, and never configure `CLAWJOURNAL_INGEST_URL` or run `bundle-share`, nothing is uploaded.
- If you are explicitly enrolled in OpenRefinery Agent Failure Sharing, the optional agent hook only shows a local reminder and can open the existing Share workflow. The hook does not read transcripts, package bundles, or upload data by itself.
- The separate recurring-upload `SessionStart` hook is inert unless you explicitly accept the current recurring authorization and the local SQLite enrollment remains enabled. It only starts a detached local runner when a cycle is due; it never sends trace content from the hook process.

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

For manual publishing, review is still your responsibility. The separately authorized recurring path below does not show each bundle, so it uses stricter completion, coverage, findings, hold, revision, and exact-artifact gates instead.

## Mandatory post-redaction scan (Betterleaks + TruffleHog)

Every share export runs two independent secret scanners on the already-redacted `sessions.jsonl` before the export is considered complete, feeding a tiered policy that ClawJournal owns — no third-party scanner decides on its own whether a whole session survives:

- **[Betterleaks](https://github.com/betterleaks/betterleaks)** (MIT) is the primary detection layer: ~380 rules plus token-efficiency and entropy filters. It runs **local-only** — its live-validation feature is never enabled, so candidate secrets are never sent to provider APIs by the detection layer.
- **[TruffleHog](https://github.com/trufflesecurity/trufflehog)** (AGPL-3.0, subprocess-only) runs verified-only as the live-credential check.

Each finding is tiered, per finding — not per bundle:

- **block** — a TruffleHog-verified live credential. The trace cannot ship.
- **review** — private-key material and other unmistakable credential structure, findings that survived redaction rescans, or an allowlisted value that nonetheless verified live. A human decides; on the automatic path the trace moves to `pending_review`.
- **redact** — a recognizable-but-unverified token (the common case). The exact span is replaced with a `[REDACTED_*]` placeholder in the bundle, the file is rescanned until clean, and the share proceeds.
- **warn** — soft keyword rules, low-entropy matches, and values you explicitly ignored or allowlisted. Recorded in the manifest, never blocks.

Scanner failure still fails closed: a missing binary or scan error blocks the export with an install hint (`scanner-not-installed` / `scanner-error`). Outcomes land in `manifest.json` under `redaction_summary.secret_scan` (tier counts, gate redactions, convergence) with full reports in `secret-scan.json`; the legacy `redaction_summary.trufflehog` + `trufflehog.json` artifacts remain and show the verified-only view.

Install:

```bash
# macOS / Linux / Windows (x86-64 and ARM64) — pinned versions, sha256-verified
# against the official releases, installed to ~/.clawjournal/bin (preferred over PATH):
clawjournal betterleaks install
clawjournal trufflehog install

# Or install them yourself:
brew install betterleaks trufflehog          # macOS
# Linux / Windows: see each project's install docs
```

The managed copies are downloaded from each project's own GitHub release artifacts at your explicit request and are only ever invoked as subprocesses with a scrubbed environment. `clawjournal betterleaks status` / `clawjournal trufflehog status` show which binaries the gate will use.

For the upload path, the gate runs at least **twice at share time**: once inside `export_share_to_disk` on the merged `sessions.jsonl`, and again after the final PII pass rewrites the file. The final PII pass always runs deterministic rules. If you opt in to AI-assisted review for a bundle, it also reviews sessions in a small bounded worker pool. A manual share records any per-trace rules-only fallback; an automatic share fails closed unless every trace has full coverage from the exact accepted provider. The manifest records this under `redaction_summary.pii_review.coverage.full` and `.rules_only`. Betterleaks also participates as a deterministic findings engine at scan-ingest time, so a session's existing `findings` rows already carry its detections before any share step — the share-time gates are the final check, not the first. Your findings decisions feed the gate: a value you ignored or allowlisted classifies as warn instead of blocking (unless it verifies as live, which always needs review).

One detector is excluded at the TruffleHog layer: **`refiner`** (refiner.io user-feedback platform). Its pattern is "the word 'refiner' followed by a UUID", which false-positives on any project name containing that substring paired with the UUIDs present throughout Claude/Codex session JSON. Verification against refiner.io's own API correctly returns `unverified` for those matches, so they are never real leaks.

An escape hatch exists for CI and development: setting `CLAWJOURNAL_SKIP_BETTERLEAKS=1` / `CLAWJOURNAL_SKIP_TRUFFLEHOG=1` disables the gate. Any bypass is recorded in the manifest (`redaction_summary.secret_scan.bypassed=true`) so reviewers can tell scanned shares from bypassed ones, and the upload path refuses to ship a bypassed bundle. Do not use it for real shares.

## What a local bundle contains

`clawjournal bundle-export <bundle_id>` writes:

- `sessions.jsonl`
- `manifest.json`
- `secret-scan.json` (combined scan report)
- `trufflehog.json` (verified-only sub-report, legacy name)

Depending on how you export, bundle content can include user messages, assistant messages, tool calls, model metadata, token counts, and timestamps. Extended thinking can be excluded from regular exports with `--no-thinking`.

## Optional upload flow

Uploading is a separate path from local export.

- Hosted research submission uses the local workbench Submit step by default. The browser talks to the local daemon, the daemon sends the finalized zip to Rayward's hosted API, and the hosted service returns a receipt ID. Self-hosters can override the destination with `CLAWJOURNAL_SHARE_URL`; setting `CLAWJOURNAL_SHARE_URL=` disables hosted submission.
- Advanced self-hosted ingest upload is disabled unless `CLAWJOURNAL_INGEST_URL` is configured.
- The ingest and hosted-share URLs must use `https://`, except for `localhost` and `127.0.0.1` during local development.
- Self-hosted ingest upload uses `clawjournal bundle-share <bundle_id>`.
- You can inspect what would be packaged with `clawjournal share --preview --status approved`.

### Explicitly authorized recurring upload

After a successful hosted manual submission, you may separately authorize automatic sharing for an exact future source/project scope. V1 limits that scope to Claude Code and Codex, whose append-only inputs have strict parsing and content-bound mutation checks; other sources remain manual-share only. This is not a replay of manual-bundle consent: the workbench shows dedicated, versioned recurring authorization and retention text and requires fresh email verification. The server records the verified identity, accepted versions/time, an opaque scope hash, the fixed five-trace cap, and an authorization revision. Raw project names, custom redaction strings, allowlist values, usernames, and local session IDs are not sent as enrollment metadata.

The local client remains the privacy authority. It considers only post-enrollment completed revisions, uses stored scores without invoking a judge, runs strict source refreshes, rechecks holds/revisions/raw-file fingerprints before egress, and seals the exact ZIP for crash recovery. The hosted service hashes the received ZIP, enforces one-to-five sessions, rejects duplicate pseudonymous revision keys, and returns an idempotent receipt.

Recurring credentials are purpose-separated and stored outside `config.json` in a fail-loud current-user-only credential file. The active credential can submit; the recovery credential can only revoke and reconcile receipts. Pause or disable wins before the local `submitting` transition. After that boundary, one already-started request may finish and cannot be recalled. Disabling does not delete earlier hosted submissions.

Claude Code and Codex hooks trigger due checks only when you start an agent session; there is no cron or daemon timer, so a missed week becomes one capped catch-up cycle. **Run now** is an explicit extra cycle, still capped at five, and resets the next due date only after a successful or clean `nothing_new` result.

### Email verification

If you use the upload flow, ClawJournal requires:

```bash
clawjournal verify-email you@university.edu
clawjournal verify-email you@university.edu --code <CODE>
```

The academic email is used for verification and short-lived upload authorization. It is not included in the exported bundle itself, and the upload token stays in the local daemon rather than browser JavaScript.

## Practical guidance

- If you only want local review, stop at `scan`, `serve`, `export`, or `bundle-export`.
- If you want to distribute data yourself, use `bundle-export` and share the files however you choose.
- If you want hosted research upload, use the workbench Submit step so the current consent terms are shown before upload.
- If you want self-hosted network upload, configure ingest explicitly and treat that as a separate opt-in step.

For security reporting and threat-model scope, see [SECURITY.md](SECURITY.md).
