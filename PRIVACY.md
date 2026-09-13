# Privacy and sharing

ClawJournal is designed to be usable without uploading anything.

## What stays local

- `clawjournal scan`, `serve`, `inbox`, `search`, `score`, `export`, and `bundle-export` run locally.
- The browser workbench is local. If you install from source, `clawjournal serve` opens your own machine at `localhost:8384`.
- **Report a problem** starts as an editable draft in browser memory. Its loopback diagnostic request uses a fixed allowlist and excludes transcripts, logs, paths, raw URLs, identifiers, and raw error text. It never takes a screenshot automatically. When private support explicitly advertises optional screenshot intake, you may press **Capture masked app view**: only the visible ClawJournal app viewport is rendered, the reporter is excluded, unmarked dynamic text and form/media content are masked by default, and the exact final PNG, dimensions, byte count, and SHA-256 are shown before consent. The browser sends only reviewed bytes to the loopback daemon; it never contacts the support server or receives the report management secret. Copy/download and opening a blank GitHub issue do not upload the draft. Private submission requires consent bound to the exact Markdown hash, optional PNG hash, support terms version, and retention version. The daemon durably recovers text receipt and optional PNG upload as separate steps, and **Delete** removes the whole remote report.
- A private problem report is not a research share and does not run the bundle redaction/TruffleHog pipeline. Remove credentials, confidential text, health information, and third-party content from the visible Markdown before sending it.
- `bundle-export` writes files to disk. It does not contact a server.
- If you never use the workbench Submit step, never choose **Send privately** for a problem report, never explicitly enable Automatic uploads, and never configure `CLAWJOURNAL_INGEST_URL` or run `bundle-share`, nothing is uploaded.
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

An email, Telegram token, or internal hostname can touch ordinary text without
a space. The scanner uses delimiters and format clues to find candidates, then
checks their replacement length. It does not discard an oversized candidate as
safe. If the boundary is unclear, sharing stops for that trace before replacing
the text. The original content stays local. Add a separator or an explicit
custom redaction, then preview again. During automatic candidate selection,
such traces are deferred so other eligible traces can fill the five slots;
if all candidates are deferred, the runner backs off and retries.

The email checks use a 64-byte local-part and 254-byte mailbox budget. Hostnames
use 63 bytes per label and 253 bytes overall. These are conservative UTF-8
replacement budgets, not complete address validators. The Telegram budget is
128 characters; it is not a claim about the maximum possible token length.
Email, Telegram-token and internal-domain regex searches use complete-candidate
adapters. Required markers locate possible matches; the unchanged regexes
validate them against the original field. They do not repeatedly retry each
suffix of the same long run. No window copies, worker processes or interpreter
launches are needed for these deterministic searches. Private keys retain
complete-text handling. Code context, finding decisions and known-credential
propagation use field/session scope. Overlapping replacements cover the union
of detected sensitive spans. Weak truncated email fragments stay local to their
occurrence unless their finding is explicitly accepted; accepted findings
propagate to matching bare identifiers. Manual and automatic
sharing use this same scanning path. Packaging and all scan gates finish
before upload starts; faster scanning does not change network transfer time.

A pure-Python literal prefilter checks required markers before the built-in
secret and PII regex passes. Only exact reviewed regexes can be skipped; new
or changed rules still run. Small fields use the original rules. Tests generate
positive inputs from the live regexes to check that a necessary marker cannot
reject a valid match. The prefilter retains no input between calls and requires
no native extension. Neither pyahocorasick nor RE2 is required.

Code evidence is collected from the original field and its offsets move with
known replacements. It is not reparsed for each email or hostname, and edits
cannot create new code exemptions. Host-boundary evidence is rechecked when
other replacements can expose a new hostname. Hints are reused within each
PII scan. Code analysis is optional and has size and complexity limits. A
parse error, NUL byte or exhausted hint budget cancels the affected code
exemptions; it never aborts secret detection, badge computation or indexing.
The detector still scans the original text. NUL bytes are not removed or
shifted in the source. Quoted data cannot authorize embedded Markdown fences.
Fence lines are paired in one pass, then checked by Python's tokenizer. An
ambiguous outer string disables fenced-code exemptions for that field. Code
hints have a 65,536-character ceiling plus token budgets. Larger fields still
receive full secret detection, but do not receive Python-code exemptions;
code-shaped text can therefore be redacted as it was before these exemptions
were added. Scanner threads do not change the process's warning filters or
hold a parser mutex. Suspicious string escapes disable optional hints before
tokenization. No trace text is executed.

Local indexing, card rendering and local session export do not apply the sharing
preflight. They retain full masking of recognized candidates, including long
URL credentials; no leading credential bytes are kept just to limit a local
email replacement. Sharing checks the original input with the strict boundary
policy. Explicit URL userinfo is a credential with a complete URL boundary, so
it is masked without imposing the email local-part limit. An explicit blocked
domain or custom redaction can likewise remove an oversized value. Ambiguous
email-like code without specific array evidence uses normal email redaction,
rather than stopping the whole share.

Pure detection returns its findings without applying replacement limits, so
findings can be saved and unchanged sessions need not be scanned every tick.
A blocked review request returns a structured error instead of closing the
connection; an explicit custom redaction can resolve the candidate before
retrying the preview.

Automatic sharing persists content-boundary deferrals in that review queue,
so later status reports exclude them and normal traces can proceed. A
boundary failure during final PII application keeps its trace identity and
does not rewrite the bundle; the automatic runner can park that trace and
retry the remainder. An absent provider finding cannot block unrelated text.
External scanner infrastructure failures remain retryable and do not change
content holds. Betterleaks and TruffleHog remain the existing share gates;
Gitleaks is not installed.

A dotted call alone, such as `api01.internal()`, is not proof of ordinary
code. A method exemption requires an explicit import, a preceding local
object binding or a function parameter. Literals, comments and arguments still scan. Code hints
cannot bypass replacement budgets: an oversized candidate remains a review
case whether parsing succeeds or fails. A large JSON value without such an
ambiguous candidate is still shareable.

An ASCII email next to unspaced Chinese, Japanese, Korean or similar prose
uses the script transition as a boundary. The surrounding prose stays intact.
Accented local parts, Unicode domains, quoted mailboxes and explicitly
angle-delimited mixed-script mailboxes remain supported. An unquoted
mixed-script local part joined directly to prose is inherently ambiguous;
use an explicit mailbox delimiter to retain its full intended span. These
heuristics do not claim perfect word separation in every script.

Fine-grained `github_pat_` credentials are recognized independently of email
syntax, including overlong token-like values. Their complete detected value
is masked locally rather than retaining a token prefix before the email
replacement window. Strict sharing still checks the original input.

Common encoded separators are checked against their original text offsets.
Named Telegram assignments and database/SSH host contexts add coverage when
the usual colon or private domain suffix is absent.

Partial emails such as `abc@` only hide the local part at that occurrence;
this also applies when `@` uses a supported separator escape. The ordinary
word `abc` elsewhere is retained. A known hostname does not remove the same
letters inside an unrelated longer word. Secret assignments retain the
variable name, separator and quotes while hiding the value, including copies
of that value in other fields. Existing review decisions still use the original
finding hash.
If a value was identified as a password or secret, that evidence takes
precedence over automatic email/hostname code exemptions in other fields.

Bounded Python syntax checks can distinguish a method call such as
`obj.local()` from a hostname. Only the method's name is protected; strings,
comments and arguments still scan. An email-shaped matrix expression is
protected only with preceding NumPy/PyTorch imports, standard aliases and
recognized array member names. Arbitrary imported aliases do not exempt addresses. A bare assignment such as
`result = numpy.array@torch.tensor` is ambiguous and stops sharing instead of
being deleted or silently treated as safe. This is a narrow syntax check, not
a general code classifier; other languages and incomplete snippets can still
produce false positives. External secret-scan gates remain mandatory.
Configuration lookups such as `DB_HOST=config["db_host"]` and named Telegram
property references retain their code identifiers; quoted values and call
arguments still scan. A candidate ending inside a lookup or call is not
treated as a complete host/token value. Independent credential and hostname
rules still scan the expression and its arguments.

These rules cannot infer every boundary: an unlabelled token fragment, an
ordinary-looking private hostname, or code that has the same spelling as an
email can remain ambiguous. A short word attached to an address may be
redacted with it. Private-key detection keeps its previous coverage; this
change does not add support for arbitrary keys without BEGIN/END markers.

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

Manual Share saves the input used for each redaction preview in the local index.
When you include that preview, packaging uses its saved content version even if
the conversation later grows. Later content stays local for a future share; it
does not inherit this inclusion. Only successful previews are saved. There is
no time-based approval expiry. Unlinked previews normally retain the latest
revision per trace and at most 100 rows. An active CLI selection protects its
own previews and can exceed that row count; if abandoned, excess rows are
pruned by the next preview or explicit cache cleanup. A total payload budget
of 128 MiB applies throughout, including linked previews. Old unused previews can be evicted; affected tabs
must refresh. Inputs linked to pending shares are preserved. If those inputs
fill the cache, new previews stop with a clear cache-full message.

After every share referencing an input receives a receipt, its raw snapshot
payload is cleared. Minimal links remain, so an old share cannot fall back to a
newer live trace. Index recovery preserves pending snapshot data and links.
`clawjournal review-cache --clear` removes unused previews;
`clawjournal review-cache --clear --all` also clears linked payloads and requires
fresh previews for pending shares. These operations remove database payloads;
they are not a guarantee of forensic removal from backups or filesystem copies.
For a completed share, local `bundle-export` copies the previously exported
JSONL only after its SHA-256 matches the recorded receipt hash. Later messages,
titles and scores do not enter that copy. Its manifest marks it as a local copy;
it is not a newly prepared upload package. Missing or changed archived files
cannot be reconstructed from current traces; use the previously downloaded ZIP.
A refreshed preview must be included again. Failed or timed-out previews remain
excluded while healthy traces can proceed. A boundary failure during optional
AI review is reported as a blocked trace, not as successful rules-only coverage.

For preparing or submitting a share, current holds, blocked status, source/project scope, exclusions, redaction rules,
consent, duplicate checks, and both secret-scan gates still apply. Missing or
damaged saved content requires a fresh preview. This manual review mechanism
does not grant or change recurring upload authority.

- Hosted research submission uses the local workbench Submit step by default. The browser talks to the local daemon, the daemon sends the finalized zip to Rayward's hosted API, and the hosted service returns a receipt ID. Self-hosters can override the destination with `CLAWJOURNAL_SHARE_URL`; setting `CLAWJOURNAL_SHARE_URL=` disables hosted submission.
- Advanced self-hosted ingest upload is disabled unless `CLAWJOURNAL_INGEST_URL` is configured.
- The ingest and hosted-share URLs must use `https://`, except for `localhost` and `127.0.0.1` during local development.
- Self-hosted ingest upload uses `clawjournal bundle-share <bundle_id>`.
- You can inspect what would be packaged with `clawjournal share --preview --status approved`.

### Explicitly authorized recurring upload

You may separately authorize automatic sharing for an exact future source/project scope on the final manual Submit screen or later from Settings. ClawJournal derives that scope automatically from every observed recurring-capable source and every currently eligible, non-excluded project; there is no recurring source picker. The exact source/project pairs remain visible in the authorization and must still be accepted. When a valid hosted challenge is available, the final screen selects the combined submit-and-enable choice by default; it displays the cadence and cap, links to the full versioned recurring authorization, retention, ownership certification, and exact scope, and lets you uncheck the choice before submitting. If recurring authority is already configured, the same row remains checked and locked so the manual share cannot silently alter that authority; Settings remains the place to review, pause, or revoke it. The local recurring mode remains off until the manual share receives a hosted receipt; only then does the same local request durably queue setup for the daemon. The receipt screen does not wait for the strict source refresh: setup continues after the browser closes and resumes when the daemon restarts, while the manual receipt remains valid if setup later needs review or fails. If the scope or hosted terms changed, automatic enrollment stops and the receipt page asks you to review them again. The recurring path is currently limited to Claude Code and Codex, whose append-only inputs have strict parsing and content-bound mutation checks; other sources remain manual-share only. Raw custom redaction strings, allowlist values, usernames, and local session IDs are not sent as enrollment metadata.

The local client remains the privacy authority. It considers only post-enrollment completed revisions, uses stored scores without invoking a judge, runs strict source refreshes, rechecks holds/revisions/raw-file fingerprints before egress, and seals the exact ZIP for crash recovery. Session-level exclusion works through the same controls as everywhere else: holding, embargoing, or blocking a session in the workbench keeps that individual trace out of automatic uploads — without removing its project from the enrolled scope — and the automatic-uploads status reports it under the exclusion counts. The hosted service hashes the received ZIP, enforces one-to-five sessions, rejects duplicate pseudonymous revision keys, and returns an idempotent receipt.

Continuously reused Claude Code and Codex conversations do not have to be closed merely to participate. Their append-only JSONL is parsed one record at a time and split only after a bounded limit has been reached and a complete assistant turn has ended. A sealed checkpoint can be considered independently while later turns continue appending; the active unfinished tail stays local and remains subject to the normal stability gate. ClawJournal binds each checkpoint to its exact raw byte range, so a later append is allowed but an edit, replacement, or truncation inside the sealed range blocks egress. The first checkpoint retains the original session identity, so a trace already shared manually follows the existing revision and fresh-approval rules instead of being duplicated under a new identity.

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
