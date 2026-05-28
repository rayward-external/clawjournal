# Simplified upload: direct "Submit to Research" from the Share tab

## Context

Today, contributing a redacted bundle to the hosted research collection is a two-app
chore: the workbench builds a zip, the user **downloads** it, then opens
`data.rayward.ai/share` in a browser and **re-uploads** it there (email verify +
consent happen on that page). We want PhD students to submit **without the
download/re-upload round-trip**, while still **agreeing to consent + data-use terms**
in the UI.

Key facts established during exploration:
- The hosted service `rayward-internal/clawjournal-share` already exposes the full
  API the browser page uses: `POST /api/verify-email`, `POST /api/verify-email/confirm`,
  `GET /api/consent`, `POST /api/submissions`. **GCS is entirely server-side** — the
  client only ever gets back a `receipt_id`. No bucket is ever exposed to the client.
- The workbench daemon already builds a zip the hosted validator accepts
  (`sessions.jsonl` + `manifest.json` + `trufflehog.json` + `trufflehog.post-pii.json`)
  in `_handle_download_share` (`clawjournal/workbench/daemon.py:2320-2329`), via the full
  redaction + TruffleHog finalize pipeline (`_prepare_share_export_for_upload`). Verified
  against `clawjournal-share@main` `validation.py`: the validator is deliberately
  backward-compatible with current bundle-export output — `schema_version` and `bundle_hash`
  are optional (a manifest omitting them is accepted). The only hard requirements are the
  three members `manifest.json` + `sessions.jsonl` + `trufflehog.post-pii.json` and a
  *finalized* manifest whose `redaction_summary` carries `pii_review.finding_count` (int)
  and `trufflehog_post_pii` with `findings==0`, `bypassed==false`, `binary_missing==false`,
  no `scan_error` — exactly what `finalize_share_export_for_upload` writes. (Re-confirm
  against the deployed service, since this was read from `main`, not prod.)
- The client's *existing* automated upload path (`upload_share` → `{INGEST}/upload`,
  loose files, `gs://` URIs) is **wire-incompatible** with the hosted Rayward server.
  The hosted path must be replaced, not reused. If self-hosted ingest is still kept, split
  it into a clearly named legacy/self-hosted function so it cannot be mistaken for hosted
  research submission.

**Chosen design:** the local daemon builds the zip in memory and POSTs it to
`https://data.rayward.ai/api/submissions` with consent fields captured in a new
in-workbench step. No zip download, no GCS exposure, upload token stays local.
Architecture mirrors the existing UI→daemon→hosted pattern (browser talks only to the
local daemon over bearer auth; the daemon talks to `data.rayward.ai`, so no CORS and the
token never enters the browser).

## Hosted API surface & validation contract (must match exactly)

- `GET /.well-known/clawjournal-share.json` → current hosted capabilities:
  `submissions_open`, `preferred_upload_flow`, `cli_ingest_supported`,
  `share_page_url`, `submit_page_url`, `maximum_bundle_size`,
  `accepted_manifest_schema_versions`, `supported_institution_email_policy`,
  `contact_email`, `deletion_withdrawal_instructions`, `cache_seconds`.
- `POST /api/verify-email` `{email}` → `{verification_id, expires_at, dev_code?}`
- `POST /api/verify-email/confirm` `{verification_id, code}` → `{upload_token, upload_token_expires_at}`
  (**no `verified` field** — treat HTTP 200 + token as success)
- `GET /api/consent` → `{consent_text, retention_text, consent_version, retention_policy_version, support_contact, ...}`
- `POST /api/submissions` (multipart) fields: `upload_token`, `consent_version`,
  `retention_policy_version`, `accept_terms`(bool), `ownership_certification`(bool),
  file `bundle`(zip) → receipt `{receipt_id, status, ...}`. Server rejects unless **both**
  `accept_terms` and `ownership_certification` are true and the versions match its current ones.
- Hosted bundle validation (`validation.py:validate_bundle_zip`) requires: a real zip
  ≤ `max_bundle_bytes` (default 52,428,800 = 50 MB) and within the uncompressed limit;
  members `manifest.json` + `sessions.jsonl` + `trufflehog.post-pii.json` (at zip root or one
  folder deep); non-empty, valid-JSONL `sessions.jsonl`; and a finalized manifest (the
  `redaction_summary.pii_review` + all-clear `trufflehog_post_pii` fields listed under
  Context). `bundle_hash` and `schema_version` are
  **optional** — if present they are enforced (`bundle_hash` must equal
  `sha256(sessions.jsonl bytes)`; `schema_version` must be in `accepted_schema_versions`,
  default `1.0.0`), but the current local manifest omits both and is accepted as-is.
  `session_count`, if present, must equal the JSONL row count (the local manifest sets it).

## Approach

### 1. Client networking layer — `clawjournal/workbench/daemon.py`

Unify everything on **one destination + the real contract**, replacing the GCS/ingest path.

- **Add `_hosted_api_base()`**: derive the API origin from `CLAWJOURNAL_SHARE_URL`
  (default `https://data.rayward.ai/share` → origin `https://data.rayward.ai`). One env
  drives both the page link and the API. Keep the HTTPS/localhost validation already in
  `_validated_hosted_share_url()`.
- **Add `_fetch_hosted_share_capabilities()`**: GET `{api}/.well-known/clawjournal-share.json`
  and cache according to `cache_seconds` for daemon lifetime. Use this for
  `submissions_open`, `maximum_bundle_size`, accepted manifest schemas, support contact,
  and email-domain policy instead of duplicating hosted constants in multiple places.
- **Rewrite `request_email_verification(email)`**: POST `{api}/api/verify-email` `{email}`;
  persist `pending_verification_id` (+ normalized email, expiry) to config; return the
  response. Surface `dev_code` only for local/dev deployments where the hosted API returns
  it; never invent or log verification codes.
- **Rewrite `confirm_email_verification(...)`**: keep a CLI-compatible wrapper signature
  (`email, code`) or add a wrapper around a lower-level `confirm_pending_email_verification(code)`.
  The confirm call must read `pending_verification_id` from config and POST
  `{api}/api/verify-email/confirm` `{verification_id, code}`. If the CLI passes an email,
  require it to match the pending email. On HTTP 200 + token, store `verified_email`,
  `verified_email_token`, `verified_email_token_expires_at`; clear the pending id. Drop the
  legacy `result.get("verified")` check.
- **Replace `_validate_ingest_url` in hosted paths**: `_ensure_verified_email_credentials()`
  should become `_ensure_hosted_upload_token()` or stop calling `_validate_ingest_url`.
  Hosted submission is governed by `CLAWJOURNAL_SHARE_URL`, not `CLAWJOURNAL_INGEST_URL`.
- **Add `fetch_hosted_consent()`**: GET `{api}/api/consent`, return the dict.
- **Add `submit_share_to_hosted(conn, share_id, *, accept_terms, ownership_certification,
  consent_version, retention_policy_version, settings, force=False)`**
  replacing `upload_share`'s transport:
  - Reuse the hosted token gate, `release_gate_blockers`, and
    `_prepare_share_export_for_upload(reuse_finalized=True)` (which wraps
    `export_share_to_disk` + `finalize_share_export_for_upload`). With `reuse_finalized`,
    Submit reuses the artifact sealed during Package (`_load_finalized_share_export`) rather
    than running the final AI-PII pass a second time.
  - If the share already has `hosted_receipt_id`/`shared_at`, return a 409-style
    "already submitted" response with the existing receipt. Hosted submit should ignore or
    reject `force`; replacing a hosted submission needs a future explicit supersede flow with
    consent and receipt handling, not an accidental double-submit.
  - Build the zip from the 4 finalized files (factor the `_handle_download_share` zip block
    into a small `_build_share_zip(export_dir) -> bytes` helper and reuse it in both places).
  - Check the final zip byte size before network upload and return a friendly error if it
    exceeds `maximum_bundle_size` from hosted capabilities (currently 52,428,800 bytes),
    not the legacy `sessions.jsonl`-only 500 MB check.
  - Use the **exact consent/retention versions displayed to the user**. The UI sends those
    versions to the daemon; the daemon forwards those versions in `_build_multipart_body`
    with `bundle` (zip), `upload_token`, and the two consent booleans. Do not silently
    replace them with freshly fetched versions at submit time; if the hosted service rejects
    stale versions, surface "terms changed, refresh and review again."
  - POST `{api}/api/submissions`; on success mark `shares.status='shared'` + `shared_at`,
    persist the hosted `receipt_id`, clear the upload token (single-use), return
    `{ok, receipt_id, status, ...}`.
- **Split hosted from self-hosted ingest**: remove GCS bucket/prefix fallback and `gs://`
  construction from the hosted path. Do not keep a generic `upload_share` name that can mean
  both hosted research submission and self-hosted ingest. Either delete the
  `CLAWJOURNAL_INGEST_URL` path if it is truly unused, or rename it to
  `upload_share_to_self_hosted_ingest` and keep it behind explicit self-hosted commands/docs.
  `_missing_ingest_url_error` should no longer appear in hosted workbench submission.
- Keep legacy DB columns such as `gcs_uri` nullable for migration/backward compatibility,
  but stop writing new hosted submissions to them and continue suppressing them from API
  responses.
- **Add receipt persistence**: add a **new gated migration** in
  `clawjournal/workbench/index.py` (bump `SECURITY_SCHEMA_VERSION`; never rewrite a historical
  migration) adding `hosted_receipt_id TEXT` to `shares` (and optionally `hosted_status TEXT` /
  `hosted_submission_url TEXT` if returned). Include the receipt in share detail/list responses
  so a page reload still shows the submission confirmation.
- **Hosted manifest compatibility**: no new manifest fields are required — the finalized
  manifest already satisfies the validator (confirmed above). Do **not** add `schema_version`
  or `bundle_hash` to make it pass; at most set `schema_version="1.0.0"` for forward-compat.
  Add focused tests asserting the submitted zip carries the three required members and that
  its manifest `redaction_summary` has the post-PII fields the hosted validator checks
  (`trufflehog_post_pii.findings==0`, not `bypassed`/`binary_missing`, no `scan_error`) —
  ideally by running the zip bytes through a copy of the hosted validation rules in the mock.
- **Hosted error mapping**: map 400/401/403/409/413/429 errors from the hosted API into
  user-facing local-daemon errors without leaking infrastructure URLs. Clear the cached token
  on token-invalid responses. Treat schema/hash/size errors as actionable re-export messages.

### 2. New daemon HTTP endpoints (bearer-authed, same-origin) — `daemon.py`

Add routes + handlers alongside the existing share routes:
- `GET  /api/share/consent`        → `_handle_share_consent` → `fetch_hosted_consent()`
- `POST /api/share/verify-email`   ← `{email}` → `_handle_share_verify_email` (stores pending id; returns `{ok, dev_code?}`)
- `POST /api/share/verify-confirm` ← `{code}`  → `_handle_share_verify_confirm`
  (stores token locally; returns `{verified:true, verified_email, expires_at}` and **never**
  returns `upload_token` to the browser)
- `GET  /api/share/upload-status`  → `_handle_share_upload_status` (returns
  `{verified_email, token_valid, expires_at, pending_email?}` so the UI knows whether to
  show the verify sub-flow)
- **Repurpose** `POST /api/shares/{id}/upload` (`_handle_upload_share`) → call
  `submit_share_to_hosted` with `{accept_terms, ownership_certification, consent_version,
  retention_policy_version}` from the body (keep the 10s `_SHARE_COOLDOWN_SECONDS`
  rate-limit guard).
- Keep legacy aliases (`/api/shares/{id}/share` and `/api/bundles/{id}/share`) routed to
  the same consent-aware handler or return a clear 400 that tells old clients to use the new
  consent body. They must not call the old loose-file ingest path.
- Disable or rewrite `/api/quick-share`: the current combined create+upload endpoint cannot
  submit to hosted research because it has no consent text/version input. It should create
  and package only, then return the new `share_id` and `next_step: "submit"`.
- Update `_handle_share_destination` to include hosted capabilities:
  `daemon_upload_supported`, `submissions_open`, `maximum_bundle_size`,
  `accepted_manifest_schema_versions`, `support_contact`, and a clear disabled `message`.

### 3. Frontend — new "Submit" step

`clawjournal/web/frontend/src/views/Share.tsx` (step machine: `StepKey` at :134,
`STEPS` at :136, render switch ~:1158-1274, `DoneStep` at :2597) and `api.ts` (:196-278).

- Add `'submit'` to `StepKey`/`STEPS`: `queue → redact → review → package → submit → done`.
  Update `completedKeysForStep`, `parseStep`, `onStepClick`, URL sync, and the package
  fallback `useEffect` that currently forces `package → done`. Packaging still seals the
  bundle (`api.shares.seal`); transition to `submit` instead of straight to `done` only when
  `daemon_upload_supported && submissions_open` is true. If hosted submission is disabled,
  closed, or invalid (`CLAWJOURNAL_SHARE_URL=` or failed validation), preserve the current
  package → done flow with download-only messaging.
- Add submit/done state: `receiptId`, `hostedStatus`, `supportContact`, and displayed
  consent metadata. When the URL already contains `share=<id>` or the user reloads on
  Submit/Done, fetch `api.shares.get(id)` and hydrate receipt/status from persisted share
  detail so the confirmation survives reload.
- **`SubmitStep` component** (reuse `btnPrimary` :461, `btnGhost` :475, `TrustChip` :292,
  `UsageDisclosure` :318, card styles from DoneStep stats :2679):
  - On mount: `api.share.consent()` + `api.share.uploadStatus()`.
  - If no valid token: inline verify sub-flow — email input → "Send code"
    (`api.share.verifyEmail`) → code input → "Verify" (`api.share.verifyConfirm`).
    In local/dev only, prefill or display the returned `dev_code` when present.
  - Render consent + retention text in a scrollable card; two required checkboxes →
    `accept_terms`, `ownership_certification` (no existing checkbox component — build a
    small one mirroring the Review approval toggle).
  - "Submit to ClawJournal Research" (enabled iff token valid **and** both boxes checked)
    → `api.shares.upload(id, { accept_terms, ownership_certification, consent_version,
    retention_policy_version })` → on success store `receiptId`, advance to `done`. Keep
    "Download zip instead" as a secondary action.
  - On stale-consent errors, reload consent text, clear both checkboxes, and require the user
    to review the new versions.
- If Package/Seal can cheaply compute the finalized zip size, return it from the seal
  response and show it in Submit/Done instead of relying only on the current token-estimate
  `approxSize`.
- **`DoneStep`**: when `receiptId` is set (from the upload response or reloaded share detail),
  show "Submitted ✓ — receipt `rcpt-…`" + support contact; otherwise the current
  local-only/download messaging.
- **`api.ts`**: add `share.consent()`, `share.verifyEmail(email)`, `share.verifyConfirm(code)`,
  `share.uploadStatus()`; change `shares.upload` to send the consent body and return `{receipt_id,...}`.

### 4. Server

No server change required: `/api/*` plus `/.well-known/clawjournal-share.json` already
support this flow, and the hosted validator already accepts the current finalized zip
(verified in `validation.py`). The only caveat is source-vs-deployed drift — this was read
from `clawjournal-share@main`; re-confirm the deployed `data.rayward.ai` runs a compatible
version (consent/retention versions, `accepted_schema_versions`, size limits) before
shipping. No coordinated server change is anticipated.

### 5. Email allowlist

`_is_edu_email` already accepts `rayward.ai` + academic suffixes in current `daemon.py`.
Keep it synchronized with `supported_institution_email_policy.domain_suffixes` from hosted
capabilities where practical, and fold in the remaining CLI hint text fix at
`cli.py:1737-1738` so errors no longer say "only .edu".

### 6. CLI behavior

The primary hosted submission path is the workbench UI because consent text must be shown
and accepted. Do **not** make `clawjournal share` or `clawjournal bundle-share` silently
upload to hosted research without consent.

Chosen CLI policy for this plan:
- Hosted research submission is workbench-only. `clawjournal share` and
  `clawjournal bundle-share` must not POST to `data.rayward.ai/api/submissions`.
- `clawjournal share --preview` remains a dry run. Non-preview `share` should either create
  and package a share then print the local workbench URL/next step, or exit with a clear
  message directing the user to the Share tab's Submit step.
- If self-hosted ingest remains supported, keep it explicit and separately named in code and
  docs. Its commands must not be described as hosted Rayward submission.
- Future non-interactive hosted CLI upload would need explicit consent flags and tests, but
  that is out of scope for this simplification.

## Files touched

- `clawjournal/workbench/daemon.py` — networking rewrite, new handlers, zip helper,
  hosted/self-hosted upload split, quick-share consent guard.
- `clawjournal/workbench/index.py` — new gated migration (bump `SECURITY_SCHEMA_VERSION`) adding `hosted_receipt_id` to `shares`; surface it in share detail/list queries.
- `clawjournal/cli.py` — `verify-email` pending-id flow, workbench-only hosted upload policy
  for `bundle-share`/`share`, self-hosted wording if retained, hint text.
- `clawjournal/web/frontend/src/views/Share.tsx` — new Submit step + verify/consent UI.
- `clawjournal/web/frontend/src/api.ts`, `types.ts` — new share API methods + types.
- `README.md`, `PRIVACY.md`, `skills/clawjournal/SKILL.md` — update upload documentation so
  it no longer describes the hosted path as a manual download/re-upload flow, while still
  documenting the download-only fallback.
- `tests/workbench/test_daemon.py`, `tests/test_cli.py` — update verify/destination tests to new contract; add new-endpoint tests (stub the hosted API by monkeypatching the urllib calls).

## Verification

1. `pytest tests/workbench/test_daemon.py tests/test_cli.py tests/workbench/test_index.py -q`
   (share-upload tests still need clean TruffleHog mocks; hosted upload must refuse bypassed
   scans).
2. Rebuild frontend: `cd clawjournal/web/frontend && npm install && npm run build`
   (CI `smoke` checks `dist/index.html`; required since we touch the UI).
3. Add unit tests for:
   - hosted capability discovery, disabled/closed submissions, max zip size, and accepted
     manifest schema propagation through `/api/share-destination`.
   - pending verification state: request stores `verification_id`, confirm uses it, CLI email
     mismatch is rejected, and browser responses never include `upload_token`.
   - already-submitted shares return the persisted receipt without re-uploading, and `force`
     is rejected/ignored for hosted submission.
   - quick-share and legacy `/share` aliases cannot upload without consent versions.
   - share detail/list include `hosted_receipt_id` but still suppress infrastructure fields.
4. **End-to-end against a local mock** (a mock implementing the **real** `/api/*` contract:
   `/.well-known/clawjournal-share.json` returns capabilities,
   `/api/verify-email` returns `dev_code`, `/api/verify-email/confirm` takes
   `verification_id`, `/api/consent` returns versions+text, `/api/submissions` takes a zip +
   consent and returns a `receipt_id`):
   - `export CLAWJOURNAL_SHARE_URL=http://localhost:8799/share` (API base derives to `:8799`).
   - Start daemon, open Share tab in Chrome, drive: package → verify email
     (`kai@rayward.ai`, dev code) → tick both consent boxes → Submit → see receipt on Done.
   - Confirm the mock received a **zip** (not loose files) with the 4 entries + both consent
     booleans true + the exact consent/retention versions displayed in the UI; confirm DB
     share is `shared`, `hosted_receipt_id` is persisted, and the token was cleared.
   - Reload the page on Done and confirm the receipt is still shown from persisted share
     detail.
   - Add a stale-consent test: mock `/api/consent` version `v1` in the UI, then reject
     submission because the server expects `v2`; the UI must tell the user to refresh and
     re-review terms rather than submitting `v2` implicitly.
   - Add a zip-limit test using a generated oversized zip body so the daemon rejects before
     calling the hosted service.
   - Verify in browser devtools/mock logs that `upload_token` is never returned to frontend
     JavaScript.

## Design decisions

- Consent versions submitted are the versions the user actually saw and accepted. The hosted
  service remains authoritative by rejecting stale versions; the local daemon must not swap
  in newer terms after consent is checked.
- Upload token remains **single-use, local-only**; never sent to the browser.
- GCS is never exposed to the client in this design; the hosted service owns storage and
  returns a receipt ID.
- Hosted capability discovery is the source of truth for max size, accepted manifest schemas,
  whether submissions are open, and support contact. The daemon may cache it briefly but must
  fail closed when the hosted destination is invalid or closed.
- Download-only export remains valid and must not be marked shared.
- Hosted CLI upload is out of scope; existing CLI commands may package or guide users to the
  workbench, but they must not silently submit hosted research data.
- Node/npm must be available to rebuild the wheel's bundled `dist/`.
