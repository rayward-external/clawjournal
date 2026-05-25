# STEM Failure-Value Scoring Algorithm, 2026-05-24

This note captures the scoring direction for ClawJournal when the raw trace
supply comes from STEM PhD students using SOTA models and coding agents. The
core product question is not "which traces are good work?" It is "which traces
reveal frontier-agent failures on real expert work?"

## Goal

ClawJournal should prioritize traces that reveal useful agent failure behavior
inside real STEM work. For model evaluation and training, a smooth success is
often less valuable than a session where the agent misunderstood a scientific
task, used the wrong method, overreached, introduced a regression, ignored a
constraint, recovered after feedback, or failed in a way that teaches something
concrete.

The main scoring question should be:

> How valuable is this trace for understanding and improving agent failure behavior?

This is different from judging whether the final code was good.

## Session Capture Objective

Raw traces from supported coding agents should be scored for failure value at
the **session** level. v1 stores one failure-value row per session. Per-episode
sample extraction (one trace producing multiple bounded samples) is deferred to
v2; it would require a separate `failure_samples` table and changes the
workbench query surface meaningfully.

A session is worth scoring when all three are true:

1. The task is real expert work, not a synthetic prompt or warmup.
2. A SOTA model or agent shows a meaningful failure, near-failure, or recovery
   pattern.
3. The trace contains enough evidence to classify what happened.

To support this, the formatted judge input must include:

- the user task and subsequent user feedback,
- each assistant plan before tool use,
- tool/action input and result,
- assistant reflection after tool results (currently captured as `Step.reflect`
  but dropped from the prompt — fix this first),
- final assistant claims and the final user response when available.

Smooth successes should still be scored (they are useful as control/baseline
data), but they will land at the low end of the failure-value scale rather than
dominating the inbox.

## Current Behavior

The existing backend mechanism is already close to what we need.

- `backend=auto` detects the current coding agent.
- In Claude Code, scoring dispatches to the Claude CLI.
- In Codex, scoring dispatches to `codex exec` with structured output.
- The scoring agent is separate from the trace source. A Codex scorer can score
  Claude Code traces, and vice versa.
- The trace is anonymized (home paths + usernames) before the judge sees it.
- The current judge emits a `substance` score, `resolution`, summary, tags, and
  privacy flags.

The main mismatch is product semantics. The runtime rubric has already moved
toward "substance" rather than pure success quality, but many surfaces still
describe the score as quality:

- README and skill docs describe `1-5` as quality.
- The workbench sorts by "Best first".
- Share recommendations prioritize recent five-star traces.
- Insights compare average score and resolution rate.

For a failure-focused product, these surfaces should become failure-value
oriented. The new judge emits both `ai_quality_score` (kept for back-compat)
and `ai_failure_value_score` in **a single call** — no separate scoring pass.

## Codebase Review Findings (verified 2026-05-24)

A review of the current scoring, workbench, share, and analytics paths surfaced
five concrete gaps. The first two are pure bugs/limitations independent of any
rubric change; the rest are the surface-level pivots that this proposal
addresses.

1. **`Step.reflect` is dropped from the judge prompt.** The formatter at
   [scoring.py:319](clawjournal/scoring/scoring.py:319)–391 captures post-tool
   assistant text but never emits it. That text often contains the agent's
   interpretation of tool results, success claims, self-corrections, and repair
   explanations — exactly the failure signal we care about. Fix this early; the
   benefit is independent of the rubric pivot.
2. **Structured schema rejects unknown keys.** `JUDGE_SCHEMA` at
   [scoring.py:450](clawjournal/scoring/scoring.py:450) has
   `additionalProperties: False`; `_validate_judge_result` strips unrecognized
   keys; persistence writes only the existing score metadata. New failure
   fields must land in the schema **before** the rubric prompt asks for them
   (see Migration Plan).
3. **`--source both` maps to `auto`.** [cli.py:128](clawjournal/cli.py:128).
   `auto` means "no filter" — which now spans Claude, Codex, OpenCode,
   OpenClaw, Cursor, Copilot, Aider, Kimi, Gemini, Custom. Too broad for v1
   scoring scope. Replace with explicit-list normalization.
4. **Scorer provenance is not first-class.**
   [`_persist_scoring_result`](clawjournal/workbench/daemon.py:101) does not
   record backend, model, rubric version, or scored timestamp as queryable
   columns. Add them.
5. **The share UI currently auto-approves unapproved queued sessions during
   packaging.** The stale comment at
   [index.py:3066](clawjournal/workbench/index.py:3066) matches real frontend
   behavior in [Share.tsx:940](clawjournal/web/frontend/src/views/Share.tsx:940).
   For failure-rich traces, remove that auto-approval path. High-value failures
   should be surfaced as review suggestions until explicitly approved/released.

## Failure-Value Score

Use a new primary score rather than silently reinterpreting historical
`ai_quality_score` rows.

Field name: `ai_failure_value_score` (1–5 scalar).

| Score | Meaning |
| --- | --- |
| 5 | Clear, consequential SOTA-agent failure on real STEM work, with strong evidence and useful lessons. |
| 4 | Meaningful failure or recovery pattern worth reviewing or turning into eval/training data. |
| 3 | Some failure signal, but ambiguous, minor, repeated, or mostly environmental. |
| 2 | Weak signal: routine retry, expected debugging, or unclear attribution. |
| 1 | No useful failure signal: smooth success, trivial session, or noise. |

A resolved trace can still score `5` if the session contains a valuable failure
pattern (e.g., the agent made a wrong change, the user caught it, the agent
recovered). Conversely, a smooth verified implementation usually scores `1` or
`2` unless retained as a control sample.

**Multi-failure sessions.** A session can contain multiple distinct failure
episodes. The judge should:

- Set `ai_failure_value_score` to the value of the **highest single teaching
  moment** in the session — not an average.
- Populate `ai_failure_modes` with **all applicable modes** across all
  failures.
- Populate `ai_recovery_labels` with **all applicable recovery patterns** (the
  judge can mark a session as both `user_corrected_recovery` and `unrecovered`
  if some episodes recovered and others did not).
- Set `ai_failure_attribution` (scalar) to the dominant cause of the most
  consequential failure.

## Structured Judge Output

The judge emits one JSON object per session, containing both legacy quality
fields and the new failure-value fields. Example:

```json
{
  "ai_quality_score": 4,
  "resolution": "resolved",
  "ai_failure_value_score": 5,
  "ai_recovery_labels": ["user_corrected_recovery"],
  "ai_failure_attribution": "agent_caused",
  "ai_failure_modes": ["wrong_assumption", "wrong_approach"],
  "ai_failure_evidence": [
    "Student corrected that the dataset was paired, not independent",
    "Agent replaced the statistical test and revised the analysis"
  ],
  "ai_learning_summary": "The agent selected the wrong statistical method until the student supplied domain correction.",
  "display_title": "Correct paired-data test choice",
  "summary": "The agent initially treated paired experimental data as independent and proposed the wrong test. The student corrected the assumption, after which the agent revised the analysis. The case shows expert feedback correcting a plausible but invalid scientific method choice.",
  "task_type": "data_analysis",
  "session_tags": ["agent_failure", "user_correction", "statistics", "recovery"],
  "privacy_flags": [],
  "project_areas": []
}
```

`ai_failure_evidence` is stored inside the existing `ai_scoring_detail`
(`detail_json`) blob, not as a top-level column — it is UI display, not a
queryable facet.

## Controlled Values

### `ai_recovery_labels` (list)

The judge emits zero or more of:

- `self_recovered`: The agent makes or encounters a meaningful mistake, detects
  it through its own process, and fixes it without student correction.
- `user_corrected_recovery`: The student points out the problem, supplies a
  missing constraint, or corrects a domain assumption, and the agent then
  recovers.
- `unrecovered`: A meaningful failure remains unresolved by the end of the
  session, even if the student tried to correct it.
- `blocked`: Progress is stopped mainly by external constraints — missing data,
  credentials, cluster quota, proprietary software, network/API access, lab
  policy, or unavailable dependencies. **Use `blocked` only when the dominant
  session story is external; if there is an agent-caused failure alongside a
  blocker, include both labels.**

Use an empty list when no failure signal is present (smooth success, trivial
session, control sample).

### `ai_failure_attribution` (scalar)

- `agent_caused`
- `environment`
- `preexisting_problem`
- `user_redirect`
- `unclear`

For multi-failure sessions, pick the attribution of the most consequential
failure.

### `ai_failure_modes` (list, 7-mode taxonomy)

| Mode | Covers |
| --- | --- |
| `wrong_approach` | Wrong method, abstraction, model choice, or computational setup. |
| `wrong_assumption` | Misread the data, domain, or scientific/statistical premise (paired-as-independent, batch effects, units, controls). |
| `false_success` | Claimed done/correct/verified but evidence shows otherwise. |
| `regression` | Broke a previously-working analysis, test, notebook, or file. |
| `instruction_violation` | Ignored user constraint, scope, safety, or architecture rule. |
| `excessive_work` | Substantial wasted effort, scope creep, or long-horizon drift across turns. |
| `blocker_mishandled` | Env/deps/creds/data blocker handled poorly. The *handling* is the failure, not the blocker itself. |

Include all that apply. Empty list = no meaningful failure signal.

`user_correction` and `self_recovery` are intentionally **not** in this list —
they belong in `ai_recovery_labels`. `unrecovered_failure` is captured by
`ai_recovery_labels: ["unrecovered"]`. `unsafe_action`, `scope_creep`, and
`tool_misuse` collapse into `instruction_violation` / `excessive_work` /
`blocker_mishandled`.

## What Counts As Valuable Failure

Prioritize sessions with clear evidence of one or more of these patterns:

- The user explicitly corrects the agent or repeats an unmet request.
- The agent claims completion but verification fails.
- The agent introduces a regression and must undo or repair it.
- The agent violates scope, privacy, safety, or architecture constraints.
- The agent spends substantial effort on the wrong direction.
- The agent fails to finish after meaningful attempts.
- The agent recovers from a mistake in a way that teaches useful repair behavior.

Do not over-rank:

- A failing test discovered at the start of a normal debugging task.
- Routine compile/test iteration that resolves quickly.
- Pure permission, network, or dependency failure unless the agent mishandles
  it.
- Smooth success with no visible mistake, correction, or failed verification.
- Trivial slash commands, warmups, and model switches.

## STEM-Specific Capture Rules

In STEM PhD traces, the strongest signals usually come from expert corrections
that a general annotator could miss. The scorer should treat substantive
domain corrections (short user messages that introduce a constraint, correct
an assumption, or supply expert context) as first-class evidence.

High-priority STEM evidence:

- Wrong statistical assumptions (paired-as-independent, ignored batch effects,
  invalid test choice).
- Wrong scientific assumptions (control group, sample type, protocol step,
  units, coordinate system, biological/chemical/physical constraints).
- Wrong computational setup (invalid simulation parameters, mistaken data
  joins, train/test leakage, incorrect preprocessing).
- Wrong interpretation of plots, tables, experimental results, logs, or model
  outputs.
- Reproducibility failures (code that runs only in the agent's assumed
  environment, missing seeds, missing dependencies, undocumented state).
- Agent behavior that would mislead a less expert user, even if the PhD
  student catches it quickly.

Lower-priority STEM evidence:

- Normal notebook iteration where the agent fixes syntax or import errors.
- Missing file or credential blockers that the agent handles correctly.
- Simple plotting, formatting, or prose edits with no scientific mistake.

The judge cannot know that the user is a PhD student. Phrase rubric guidance
in terms of the *evidence visible in the trace* (short substantive domain
corrections, technical pushback, supplied constraints) rather than asserted
roles.

## Source Scope

v1 failure-value scoring covers:

- `claude` (Claude Code)
- `codex` (Codex)
- `opencode` (OpenCode)
- `openclaw` (OpenClaw)

v1 excludes `cursor`, `copilot`, `aider`, `kimi`, `gemini`, `custom`, and
`hermes`. They remain visible in the workbench and continue to use legacy
`ai_quality_score`. Expanded source coverage is a v2 concern. Hermes is a
follow-up connector, not part of the failure-value v1 rollout.

The judge backend default is unchanged:

- Running inside Claude Code uses Claude as the judge.
- Running inside Codex uses Codex as the judge.
- Explicit `--backend` remains available for debugging.

`ai_scorer_backend` and `ai_scorer_model` are persisted so cross-backend bias
can be audited later.

The CLI source filter should accept the explicit v1 list. `--source both` is
deprecated and prints a warning; recommend either a new explicit alias such as
`--source failure-v1` or comma-separated values
(`--source claude,codex,opencode,openclaw`). Keep `all` and `auto` as their
existing broad "no filter" semantics; do not silently widen `both` to `auto`.

## Storage And Provenance

Persist enough context to compare scorer behavior and safely rescore later.
Nine new columns on `sessions`:

**Data (5):**

```sql
ai_failure_value_score INTEGER         -- 1..5
ai_recovery_labels     TEXT            -- JSON array
ai_failure_attribution TEXT            -- scalar enum
ai_failure_modes       TEXT            -- JSON array
ai_learning_summary    TEXT
```

**Provenance (4):**

```sql
ai_scorer_backend      TEXT
ai_scorer_model        TEXT
ai_rubric_git_sha      TEXT            -- commit hash of the rubric file at scoring time
ai_scored_at           TEXT            -- ISO timestamp
```

Folded into the existing `ai_scoring_detail` (`detail_json`) blob, not their
own columns: `ai_failure_evidence` (JSON array of short evidence quotes). The
detail blob is exposed via the HTTP API at
[daemon.py:1147](clawjournal/workbench/daemon.py:1147) but is not currently
rendered in the frontend; folding evidence into it is safe.

`ai_quality_score` is kept. The judge emits both scores in one call.

Add the columns through the workbench's additive migration path or a dedicated
workbench scoring schema gate. Do not use `SECURITY_SCHEMA_VERSION` for this:
that gate is specifically tied to findings and hold-state security migrations.

JSON-array columns use the same `json_each(...)` filter pattern as
`value_badges` / `risk_badges` ([index.py:2465](clawjournal/workbench/index.py:2465)).
This is boundary-safe — no fragile `LIKE` substring matching.

## Workbench Behavior

Default review experience becomes failure-first:

- Rename "Best first" to **"Top failures"**.
- Sort:

  ```sql
  ORDER BY
    ai_failure_value_score IS NULL,
    ai_failure_value_score DESC,
    start_time DESC
  ```

- Source filter chip defaults to the v1 supported list
  (`claude`, `codex`, `opencode`, `openclaw`). A "show all sources"
  toggle reveals legacy / out-of-scope sources, which fall to the bottom of
  the sort because they have `ai_failure_value_score = NULL`.
- Add filters for `ai_recovery_labels`, `ai_failure_attribution`, and
  `ai_failure_modes` (all using `json_each` for the array columns).
- Show a compact failure chip on trace cards (e.g., `5 failure value`,
  `agent_caused`, `user_corrected_recovery + unrecovered`).
- Show `ai_quality_score` as a secondary chip alongside the failure chip.
  The productivity signal stays visible but is no longer the primary sort —
  this preserves continuity for users who relied on the old score.
- Keep `resolution` visible, but do not let final success hide a valuable
  failure pattern.

Recommended queue default:

```text
ai_failure_value_score >= 4
source IN ('claude', 'codex', 'opencode', 'openclaw')
```

## Share Flow

The share flow recommends traces by failure value, not success quality.

Priority:

1. Recent approved traces with `ai_failure_value_score >= 4`.
2. Recent unreviewed traces with strong failure signals, shown as review
   suggestions rather than auto-added share candidates.
3. Older approved high-value failures.

**Do not auto-approve high-value failure traces during packaging.** A
failure-rich trace is more likely to contain sensitive project detail,
embarrassing behavior, or user correction context. Remove the frontend path that
auto-approves queued, unapproved sessions before packaging. Share-time approval
must stay explicit.

**AI-PII review default-on for `ai_failure_value_score >= 4`.** When the user
selects a share package (typically 5–10 traces), the share UI pre-checks the
AI-PII review checkbox for any selected session with score ≥ 4. The user can
uncheck it but it is on by default. This adds 5–10 AI calls per share batch in
the worst case — acceptable.

Unchanged hold-state / share invariants:

- Hold-state still gates upload (`auto_redacted` or `released` only).
- Source and project confirmation still gate export.
- Regex redaction still runs on export.
- AI PII review remains an additional share-time layer (default-on for ≥ 4,
  opt-in below).
- TruffleHog remains a mandatory post-redaction gate.

Delete the stale share-ready comment at
[index.py:3066](clawjournal/workbench/index.py:3066) in the same change, and
remove the corresponding frontend auto-approval behavior at
[Share.tsx:940](clawjournal/web/frontend/src/views/Share.tsx:940).

## Insights

Failure-focused analytics should answer:

- Which source produces the most high-value failure traces?
- Which failure modes are most common?
- How often does the agent recover from its own mistakes?
- How often does recovery require user correction?
- How often are failures agent-caused vs environment-caused?
- Which models / reasoning-effort tiers correlate with false success claims,
  excessive work, or instruction violations?

Replace or de-emphasize success-oriented labels such as "highest quality
model" when viewing the failure corpus.

Aggregate metrics:

- High-value failure count: `ai_failure_value_score >= 4`
- Agent-attributed failure rate
- User-corrected recovery rate
- Self-recovery rate
- Unrecovered failure rate
- Blocked-but-mishandled rate
- Failure modes by source
- Failure modes by model and reasoning effort
- Failure value per dollar / per token (requires per-session cost — verify
  during implementation)

**Caveat — `model_effort` coverage is partial.** The parser captures
`model_effort` for Codex, OpenCode, and Kimi
([parser.py:1018](clawjournal/parsing/parser.py:1018)) but **not for Claude
Code or Gemini CLI** (see [parser.py:1026](clawjournal/parsing/parser.py:1026)).
Any "reasoning-effort × failure-mode" insight will be NULL for Claude/Gemini
rows; label that explicitly in the UI rather than silently averaging over a
partial population.

## Rubric Guidance

The judge should be told:

- Score failure value, not final task quality.
- A resolved session can be highly valuable if it contains a clear failure
  and recovery pattern.
- A failed session is not automatically valuable; it must contain useful,
  attributable evidence.
- Distinguish discovered failures from agent-caused failures.
- Treat substantive domain corrections (short user messages introducing a
  constraint, correcting an assumption, or supplying expert context) as
  high-signal evidence — do not require user role labels.
- Treat final user feedback as important, but not the only signal.
- Avoid punishing sessions just because tests initially failed during
  ordinary debugging.
- Prefer evidence-backed labels over speculation.
- Prefer sessions where a frontier model fails on realistic expert work over
  synthetic puzzle-like failures.
- Separate agent-caused failures from preexisting scientific/codebase
  problems.
- Keep control samples, but do not let smooth success dominate the capture
  set.
- For multi-failure sessions: score by the highest single teaching moment;
  emit all applicable `ai_failure_modes` and `ai_recovery_labels`; pick one
  `ai_failure_attribution` for the most consequential failure.
- If `display_title` describes a session, prefer naming the failure pattern
  when `ai_failure_value_score >= 3`; otherwise name the task.

## Migration Plan

Order matters: each step must be no-op-safe if the next is rolled back. In
particular, **schema must accept new fields before the rubric prompt asks for
them** — otherwise the validator silently strips the judge's response.

**Checklist for every write-path change.** The CLAUDE.md implementation note
applies: any new column or persisted field must land in all three write paths
together, or a normal re-scan will wipe newly-written scores.

- [ ] `_score_single_session` in [cli.py](clawjournal/cli.py)
- [ ] `_persist_scoring_result` in [daemon.py](clawjournal/workbench/daemon.py)
- [ ] Session upsert in scan / incremental scan
      ([workbench/index.py](clawjournal/workbench/index.py))
- [ ] HTTP API serializers ([daemon.py](clawjournal/workbench/daemon.py))
- [ ] Frontend types ([web/frontend/src/](clawjournal/web/frontend/src/))

### Steps

1. **Schema first.** Extend `JUDGE_SCHEMA` to accept the new failure fields
   (still `additionalProperties: False`, but the new keys are recognized).
   Update `_validate_judge_result` to pass them through. Confirm current
   rubric still works (no new fields emitted yet) — pure additive change.
2. **DB columns + migration.** Add the 9 new columns through the workbench
   additive migration path or a dedicated scoring schema gate. Default values
   NULL. Do not use `SECURITY_SCHEMA_VERSION`.
3. **Three write paths.** Update CLI, daemon, scan/upsert together (see
   checklist above) so the new columns are persisted and *preserved on
   re-scan*. Add API + frontend type changes in the same commit.
4. **Prompt formatter.** Update `format_session_for_judge` to include
   `Step.reflect`, user feedback after assistant turns, final assistant
   claims, and final user response. This is an independently useful change
   even before the rubric pivot.
5. **Rubric prompt update.** Ask the judge for both `ai_quality_score` and
   `ai_failure_value_score` plus the new failure fields, in one call.
6. **`clawjournal rescore --window 7d` command.** Manual rescore command for
   in-scope sources. Bounded by `start_time` rolling window.
7. **Daemon auto-pipeline.** The background scanner auto-scores any session
   in the last 7 days from in-scope sources that is missing
   `ai_failure_value_score`. This is the primary scoring path — users should
   not need to run the CLI command.
8. **Workbench inbox.** Sort, label ("Top failures"), source filter chip
   default, failure-mode / recovery-label / attribution filters.
9. **Share flow.** AI-PII review default-on for `ai_failure_value_score >= 4`
   on selected sessions only (typically 5–10 per share). Remove frontend
   auto-approval during packaging and delete the stale share-ready comment at
   index.py:3066.
10. **`bundle-export` JSONL format.** Include the new failure fields in the
    exported training-data bundle ([export/training_data.py](clawjournal/export/training_data.py)).
    Failure-annotated sessions are the point of the v1 export — this cannot be
    skipped. Reserve per-episode failure samples for v2 after a
    `failure_samples` table exists.
11. **README.** Replace the "quality rating" section with failure-value
    semantics; keep a brief mention of `ai_quality_score` as a secondary
    productivity score.
12. **`clawjournal-score` skill prompt.** Update [skills/](skills/) (single
    source of truth per CLAUDE.md — `plugins/clawjournal/skills` is a
    symlink) to describe the failure-value rubric, the 7-mode taxonomy, the
    recovery labels list, and the share-time PII default.
13. **Insights.** Failure modes by source, recovery rate, attribution rate,
    failure modes × `model_effort` (with the Claude/Gemini coverage caveat
    surfaced in the UI).
14. **Source follow-ups.** Do not block v1 scoring on Hermes. If demand
    materializes, write a separate Hermes connector spec/PR that reverses the
    current phase-2 deferral and follows the SQLite discipline used for Crush
    (`mode=ro&immutable=1`, `busy_timeout`, no `ATTACH`, parameterized only).
    The failure-value rubric will pick up Hermes sessions after `HERMES_SOURCE`
    is wired into the parser, source enum, and workbench source filters.

## Follow-up Work

- **Hermes parser** — current phase-2 planning explicitly defers Hermes. If
  demand changes, implement `HERMES_SOURCE` in
  [parsing/parser.py](clawjournal/parsing/parser.py) under a separate connector
  spec/PR. Follow the SQLite discipline used for Crush (`mode=ro&immutable=1`,
  `busy_timeout`, no `ATTACH`, parameterized only). Resolve exact on-disk path,
  schema, and session-boundary mapping during that parser spec.
