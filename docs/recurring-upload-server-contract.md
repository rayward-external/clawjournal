# Recurring Upload Server Contract

## Purpose

ClawJournal performs discovery, indexing, selection, redaction, secret scanning,
and packaging locally. The hosted service receives only the small set of final
eligible traces selected for a recurring cycle.

The existing manual upload token is not suitable for unattended recurring
uploads because it is short-lived, intended for a manually confirmed
submission, and cleared after a successful manual upload. Recurring uploads
therefore require a distinct, versioned authorization with separate active and
recovery credentials.

This document defines the hosted API contract expected by the ClawJournal
client.

## Client Responsibilities

The client is responsible for:

- keeping the full session history and authoritative selection state locally;
- enrolling an exact source and project scope after explicit consent;
- considering only sessions after the server-accepted enrollment timestamp;
- waiting until append-only sessions have remained unchanged for 24 hours;
- selecting at most five traces with deterministic ordering;
- enforcing release, source, revision, privacy, and secret-scanning gates;
- sealing and hashing the exact ZIP before attempting egress;
- persisting the client submission ID, revision keys, artifact path, and hash;
- retrying with the same artifact bytes after an unambiguous transient failure;
- reconciling ambiguous receipts before allowing another submission; and
- stopping submission when the user pauses, disables, or changes enrollment.

The server must not assume that client-side enforcement replaces server-side
authorization, limits, idempotency, or validation.

## Complete Staging Service

The client derives one hosted API origin from `CLAWJOURNAL_SHARE_URL`. A staging
deployment must therefore provide the existing manual sharing API and the new
recurring API on the same origin.

Existing routes required by enrollment and manual sharing:

```http
GET  /.well-known/clawjournal-share.json
GET  /api/consent
POST /api/verify-email
POST /api/verify-email/confirm
POST /api/submissions
```

New recurring routes:

```http
GET  /api/recurring-upload/terms
POST /api/recurring-upload/enroll
POST /api/recurring-upload/submissions
POST /api/recurring-upload/receipt
POST /api/recurring-upload/revoke
```

Staging should deploy the existing hosted service with these extensions rather
than introducing an unrelated recurring-only service.

## Capability Document

`GET /.well-known/clawjournal-share.json` must retain the existing manual share
fields and advertise the recurring contract:

```json
{
  "submissions_open": true,
  "share_page_url": "https://staging.example/share",
  "maximum_bundle_size": 52428800,
  "recurring_upload_api_version": 1,
  "recurring_upload_max_sessions": 5,
  "recurring_consent_version": "consent-v1",
  "recurring_retention_policy_version": "retention-v1"
}
```

The client exposes recurring enrollment only when:

- `recurring_upload_api_version` is exactly `1`; and
- `recurring_upload_max_sessions` is exactly `5`.

The client also includes `share_page_url` in the authorized profile hash. A
destination change therefore requires user review rather than silently moving
an existing authorization to another service.

## Recurring Terms

### Request

```http
GET /api/recurring-upload/terms
```

### Response

```json
{
  "consent_text": "Recurring upload consent text...",
  "retention_text": "Recurring upload retention policy...",
  "consent_version": "consent-v1",
  "retention_policy_version": "retention-v1"
}
```

The response versions must match the versions advertised by the capability
document. Changing either advertised version causes enrolled clients to stop
with `action_required` until the user reviews and accepts the new terms.

## Enrollment

### Request

```http
POST /api/recurring-upload/enroll
Content-Type: application/json
```

```json
{
  "client_enrollment_id": "client-generated-uuid",
  "identity_token": "fresh-manual-verification-token",
  "consent_version": "consent-v1",
  "retention_policy_version": "retention-v1",
  "scope": {
    "source_scope": "all",
    "included_projects": ["codex:project-a"],
    "excluded_projects": [],
    "ai_pii_review_enabled": false,
    "destination_origin": "https://staging.example/share"
  },
  "cadence_days": 7,
  "max_sessions_per_cycle": 5
}
```

### Server Requirements

The server must:

1. validate that `identity_token` is fresh, valid, and not revoked;
2. bind the enrollment to the verified identity;
3. confirm that the identity has completed a successful hosted manual share;
4. validate the current consent and retention versions;
5. persist the exact authorized scope;
6. require `max_sessions_per_cycle` to equal `5`;
7. create a versioned enrollment;
8. issue distinct active and recovery credentials; and
9. return a trusted server acceptance timestamp.

### Response

```json
{
  "enrollment_id": "server-enrollment-id",
  "active_token": "cj_active_random-value",
  "recovery_token": "cj_recovery_random-value",
  "authorization_revision": "auth-rev-1",
  "accepted_at": "2026-07-16T10:00:00Z"
}
```

All five values are required non-empty strings. `accepted_at` must be a
timezone-aware ISO 8601 timestamp. The client uses it for both boundaries:

```text
future-only baseline = accepted_at
first next_due_at     = accepted_at + cadence_days
```

## Credentials

The server should issue opaque credentials with at least 256 bits of entropy.
Only a one-way digest or keyed digest should be stored server-side.

Suggested credential separation:

| Credential | Create enrollment | Upload | Lookup receipt | Revoke |
|---|---:|---:|---:|---:|
| Manual identity token | Yes | No | No | No |
| Active recurring token | No | Yes | No | No |
| Recovery token | No | No | Yes | Yes |

Persist at least:

```text
token_type
token_digest
enrollment_id
authorization_revision
created_at
revoked_at
last_used_at
```

The V1 client does not implement automatic token rotation. Recurring
credentials should therefore remain valid until revocation or authorization
revision invalidation, unless a rotation endpoint and corresponding client
support are added.

## Recurring Submission

### Request

```http
POST /api/recurring-upload/submissions
Content-Type: multipart/form-data
```

Text fields:

```text
upload_token
client_submission_id
enrollment_id
authorization_revision
revision_keys
consent_version
retention_policy_version
accept_terms=true
ownership_certification=true
```

File field:

```text
bundle=<finalized ZIP>
```

`revision_keys` is a JSON-encoded string array containing no more than five
entries.

### Server Validation

The server must validate that:

- the active token is valid and belongs to `enrollment_id`;
- the enrollment is active and not revoked;
- `authorization_revision` is current;
- consent and retention versions remain accepted;
- no more than five revision keys are present;
- the ZIP is within the advertised maximum size;
- the ZIP structure and manifest schema are accepted; and
- `client_submission_id` satisfies the idempotency rules below.

The server must compute its own artifact hash from the received ZIP and must
not trust a client-provided digest.

### Success Response

```json
{
  "receipt_id": "receipt-id",
  "status": "received",
  "submission_url": "https://staging.example/submissions/receipt-id"
}
```

`receipt_id` is required. The remaining fields may be omitted when the hosted
service does not expose them.

## Idempotency

Use this unique key:

```text
(enrollment_id, client_submission_id)
```

Persist the submission identity and result atomically:

```text
enrollment_id
client_submission_id
artifact_hash
object_path
object_generation
receipt_id
status
created_at
```

Required behavior:

- same key and same artifact hash: return the original receipt without storing
  another object;
- same key and different artifact hash: return `409 Conflict` and never
  overwrite the accepted object;
- a transient response failure after storage must remain recoverable through
  receipt lookup.

Object creation should use a non-overwrite precondition when supported by the
storage backend.

## Receipt Reconciliation

### Request

```http
POST /api/recurring-upload/receipt
Content-Type: application/json
```

```json
{
  "enrollment_id": "server-enrollment-id",
  "client_submission_id": "client-submission-uuid",
  "recovery_token": "cj_recovery_random-value"
}
```

### Found Response

```json
{
  "receipt_id": "receipt-id",
  "status": "received"
}
```

When no submission exists, the service may return `404` or a successful JSON
response without `receipt_id`. A found receipt must contain a non-empty string.

## Revocation

### Request

```http
POST /api/recurring-upload/revoke
Content-Type: application/json
```

```json
{
  "enrollment_id": "server-enrollment-id",
  "recovery_token": "cj_recovery_random-value"
}
```

The server must:

- mark the enrollment revoked;
- immediately reject the active credential;
- invalidate the recovery credential after successful revocation;
- reject later submissions with `401` or `403`; and
- record the revocation timestamp.

The client deletes its active credential before attempting remote revocation.
If the network request fails, it retains the recovery credential and records
`revocation_pending` rather than restoring upload authority.

## HTTP Status Contract

| Status | Meaning |
|---|---|
| `200` / `201` | Enrollment, submission, lookup, or revocation succeeded |
| `400` | Invalid fields, request format, or policy versions |
| `401` / `403` | Invalid, expired, or revoked credential |
| `409` | Authorization revision, idempotency hash, or state conflict |
| `413` | ZIP exceeds the advertised maximum size |
| `429` | Rate limited; the client may retry later |
| `5xx` | Transient server failure; the client retains the exact artifact |

Errors should include a readable message:

```json
{
  "error": "Human-readable error description"
}
```

## Storage Boundary

The ClawJournal client must not receive Google Cloud service-account keys or
general bucket IAM access. The hosted service runtime owns the storage
credential and writes accepted bundles to the staging or production bucket.

A direct-to-GCS `gcloud` flow may be used as a developer-only canary, but it is
not part of the production client contract.

## Staging Acceptance Checklist

- Existing manual email verification and submission still pass.
- Capability and recurring terms versions match.
- Enrollment returns complete dual credentials and server `accepted_at`.
- The first due time is seven days after server acceptance.
- A valid recurring ZIP produces one stored object and one receipt.
- A same-ID, same-byte retry returns the original receipt.
- A same-ID, different-byte retry returns `409`.
- A simulated response timeout is resolved by receipt lookup.
- More than five revision keys are rejected.
- Revoked credentials cannot upload or query receipts.
- Pausing or disabling locally before egress prevents submission.
- Staging logs do not expose raw tokens, recovery credentials, or trace data.

## Current Delivery Boundary

The ClawJournal repository contains the client enrollment, scheduling,
selection, privacy gates, artifact sealing, retry, reconciliation, CLI, and UI
implementation. End-to-end production readiness additionally requires:

- implementation of the hosted routes in this document;
- database migrations and token storage;
- staging deployment of the complete hosted service;
- a real staging canary; and
- compatibility verification before production rollout.
