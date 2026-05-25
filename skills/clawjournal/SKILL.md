---
name: clawjournal
description: Review, curate, and share coding agent conversation traces. Use when the user asks to review traces, curate sessions, manage their workbench, export conversations, share a trace, or review PII/secrets in exports. Triggers on "review traces", "share traces", "clawjournal", "my sessions", "what did I work on", or trace curation requests.
---

# ClawJournal

ClawJournal helps you review, curate, and share coding agent traces from Claude Code, Codex, Gemini CLI, OpenCode, OpenClaw, Kimi CLI, and Cline.

## Prerequisite

```bash
command -v clawjournal >/dev/null 2>&1 && echo "clawjournal: installed" || echo "clawjournal not found — run /clawjournal-setup first"
```

If clawjournal is not installed, tell the user to run `/clawjournal-setup` (or invoke the clawjournal-setup skill) first.

## Communication Guidelines

**Adapt to the user's technical level.** When interacting with non-technical users (especially via Telegram, Discord, or other messaging channels):

- **Never show raw commands, UUIDs, file paths, or CLI flags.** Run the commands yourself and present only the human-readable results.
- **Never expose infrastructure details.** Do not show GCS bucket addresses, ingest service URLs, Cloud Function endpoints, or any internal system identifiers. If the JSON output contains fields like `gcs_uri`, `bundle_hash`, or `device_id`, suppress them entirely.
- **Use simple, non-technical language.** Say "your sessions" not "indexed sessions in the workbench DB." Say "uploaded" not "shared via ingest service."
- **When asked "how to use clawjournal"** — don't write a reference manual. Instead, run the commands yourself and guide the user interactively step by step.

**Always show what was redacted.** When sharing data, the share result includes a `redaction_summary` with `total_redactions` and `by_type`. Always present this to the user:

> Your data has been cleaned before sharing:
> - 3 API keys redacted
> - 2 JWT tokens redacted
> - 1 database URL redacted
> - All file paths anonymized, usernames hashed
>
> 5 sessions uploaded successfully.

If `total_redactions` is 0, still reassure:

> Your data was scanned for secrets, API keys, tokens, and PII — nothing sensitive was found. All file paths were anonymized and usernames hashed. 5 sessions uploaded.

**Never skip the redaction report.** The user must always know their data was checked and what (if anything) was removed.

## Two Modes

ClawJournal has two workflows:

1. **Workbench Mode** — Review and curate traces (local-first, scientist-facing)
2. **Export Mode** — Bulk export to local JSONL

Default to **Workbench Mode** unless the user specifically asks about bulk export.

---

## Workbench Mode

When the user asks to review traces, curate sessions, look at their work history, or manage data:

### Quick Start

```bash
clawjournal scan                              # Index sessions into local DB
clawjournal inbox --json --limit 15           # Show trace cards (JSON for you to parse)
```

Parse the JSON output. Each session has: `index`, `session_id`, `display_title`, `source`, `model`, `messages`, `tokens`, `outcome_badge`, `value_badges`, `risk_badges`, `review_status`.

Present the traces to the user as a numbered list showing title, source, badges, and status. Then ask: **"Quick triage here, or open the full review UI?"**

**On a remote VM?** Skip the browser — do the full review right here in the terminal. Follow the Review & Share steps below.

### Review & Share

**Step 1 — Scan & score**

```bash
clawjournal scan
clawjournal score --batch --source failure-v1 --auto-triage
```

`--source failure-v1` scopes scoring to claude / codex / opencode / openclaw. Each session gets two AI ratings: productivity (legacy) and failure-value (the new primary signal for finding teachable agent failures).

Present summary:

> Found 47 sessions. 23 scored. Productivity 4-5: 14, productivity 1-2: 8 (auto-archived). Failure-value 4-5: 9 (high-value failure traces — review first). 16 still need review.

Ask:
> How would you like to review?
> - **"review here"** — I'll show the session list and we'll triage together (works everywhere)
> - **"open UI"** — I'll launch the workbench in your browser

If user chooses the UI:

```bash
clawjournal serve
```

Tell user: "Workbench is open at localhost:8384. Triage sessions in the Inbox, then come back and say 'share' when ready."

For remote VMs: `clawjournal serve --remote` prints the SSH tunnel command.

When user returns, continue to Step 2.

**Step 2 — Show preview**

```bash
clawjournal share --status approved --preview --json
```

Parse the JSON. Present up to 10 sessions at a time:

> 23 approved sessions. Here are the first 10:
>
> 1. Fix auth bug (failure 5 · productivity 5) — Agent's first patch broke a different test; user corrected; agent recovered with the right fix.
> 2. Refactor DB layer (failure 1 · productivity 4) — Clean refactor, no failure signal.
> 3. Add parser tests (failure 4 · productivity 4) — Agent claimed coverage; tests didn't actually exercise the new branch until user pointed it out.
> ...

Failure value is shown first because it's the primary signal for this corpus — a high failure value means the trace teaches something about agent behavior (failures + recoveries), independent of whether the work succeeded.
>
> You can:
> - Remove sessions: "remove 4"
> - Inspect one: "show me 4"
> - See more: "next 10"
> - Add a comment: "note: my week 12 traces"
> - Confirm all: "looks good" or "share"

**Step 3 — Handle user feedback (loop until confirmed)**

If user says "remove 4, 7":

```bash
clawjournal block <id-for-4> <id-for-7> --reason "user excluded"
```

Then re-run preview and present updated list.

If user says "show me 4":

```bash
clawjournal score-view <id-for-4>
```

Present the condensed session transcript, then return to the preview.

If user adds a note, remember it for the `--note` flag.

If user says "looks good" / "share" / "yes" — proceed to Step 4.

**Step 4 — Share with confirmation**

Only after explicit user confirmation:

```bash
clawjournal share --status approved --note "<user's comment>" --json
```

Parse the JSON result. **Always report the redaction summary to the user** (see Communication Guidelines above).

### Quick Share

For quickly sharing a single trace — no approval workflow, no bundles:

**Step 1 — Show recent sessions**

```bash
clawjournal recent --json --limit 5
```

Present as a numbered list:

> Recent sessions:
>
> 1. Auth Middleware Fix — 23 min · 4/5 · Tests passed
> 2. Debug Memory Leak — 45 min · 3/5 · Tests failed
> 3. Add Parser Tests — 12 min · 5/5 · Tests passed
>
> Reply "share 1" to send a card, or "details 2" for more info.

**Step 2 — User picks a session**

```bash
clawjournal card <session-id> --depth summary --json
```

The `card_text` field is pre-formatted and ready to send.

**Depth options** (user can specify, or choose based on context):
- `--depth workflow` — safe for public channels (no content, just tool sequence + stats)
- `--depth summary` — safe for team channels (task descriptions, no code) **← default**
- `--depth full` — for trusted recipients (full redacted content)

### Quick Triage (without scoring)

For manual triage without AI scoring:

```bash
clawjournal inbox --json --limit 15
```

Show numbered list. Take instructions like "approve 1,3,5" or "block 2":

```bash
clawjournal approve <session-id> [session-id ...] --reason "good debugging trace"
clawjournal block <session-id> [session-id ...] --reason "contains proprietary code"
```

Map the user's index numbers to session_ids from the inbox JSON output.

### Full Review (web UI)

```bash
clawjournal serve
```

Opens a browser at `localhost:8384` with:
- **Inbox**: Trace cards with value/risk/outcome badges and one-click triage
- **Search**: Full-text search across all session transcripts
- **Session Detail**: Three-pane view (timeline | transcript | metadata)
- **Bundles**: Assemble and export curated upload sets
- **Policies**: Manage redaction rules and project exclusions

---

## Export Mode

When the user asks to bulk export their conversations locally:

### THE RULE

**Every `clawjournal` command outputs `next_steps`. FOLLOW THEM.**

Do not memorize the flow. Do not skip steps. Do not improvise.
Run the command → read the output → follow `next_steps`. That's it.

### Getting Started

Run `clawjournal status` (or `clawjournal prep` for full details) and follow the `next_steps`.

### Output Format

- `clawjournal prep`, `clawjournal config`, `clawjournal status`, and `clawjournal confirm` output pure JSON
- `clawjournal export` outputs human-readable text followed by `---CLAWJOURNAL_JSON---` and a JSON block
- Always parse the JSON and act on `next_steps`

### PII Audit

After `clawjournal export --no-push`, follow the `next_steps` in the JSON output. The flow is:

1. **Ask the user their full name** — then grep the export for it
2. **Run the pii_commands** from the JSON output and review results with the user
3. **Ask the user what else to look for** — company names, client names, private URLs, other people's names, custom domains
4. **Deep manual scan** — sample ~20 sessions (beginning, middle, end) and look for anything sensitive the regex missed
5. **Fix and re-export** if anything found: `clawjournal config --redact "string"` then `clawjournal export --no-push`
6. **Run `clawjournal confirm` with text attestations** — pass `--full-name`, `--attest-full-name`, `--attest-sensitive`, and `--attest-manual-scan`

---

## Command Reference

```bash
# Quick Share
clawjournal recent [--source openclaw] [--since today] [--limit 5] [--json]
clawjournal card <id> [--depth workflow|summary|full] [--json]

# Triage & Review
clawjournal scan [--source claude|codex|openclaw]
clawjournal inbox [--status new|shortlisted|approved|blocked] [--limit 20] [--json]
clawjournal approve <id> [id ...] [--reason "..."]
clawjournal block <id> [id ...] [--reason "..."]
clawjournal shortlist <id> [id ...]
clawjournal search <query> [--json] [--limit 20]

# Scoring
clawjournal score --batch [--source failure-v1] [--auto-triage] [--limit 20]
clawjournal rescore --window 7d [--source failure-v1] [--limit 200]
clawjournal score-view <id>
clawjournal set-score <id> <1-5> [--reason "..."]  # legacy productivity only

# Share
clawjournal share --status approved [--note "..."] [--preview] [--json]

# Bundles
clawjournal bundle-create [ids ...] [--status approved]
clawjournal bundle-list
clawjournal bundle-view <bundle_id>
clawjournal bundle-export <bundle_id>
clawjournal bundle-share <bundle_id>

# Export (bulk)
clawjournal status
clawjournal prep [--source all|claude|codex|gemini|opencode|openclaw]
clawjournal config --source all
clawjournal config --exclude "a,b"
clawjournal config --redact "str1,str2"
clawjournal config --redact-usernames "u1,u2"
clawjournal config --confirm-projects
clawjournal export [--no-push] [--no-thinking] [--pii-review --pii-apply]
clawjournal confirm --full-name "NAME" --attest-full-name "..." --attest-sensitive "..." --attest-manual-scan "..."

# Workbench
clawjournal serve [--port 8384] [--no-browser] [--remote]
```

## Gotchas

- **`--exclude`, `--redact`, `--redact-usernames` APPEND** — they never overwrite. Safe to call repeatedly.
- **`clawjournal inbox --json`** is the preferred way for agents to read trace data.
- **`clawjournal serve`** opens a browser automatically. Use `--no-browser` to suppress.
- **Everything is 100% local** — nothing leaves the machine unless the user explicitly runs `share`.
