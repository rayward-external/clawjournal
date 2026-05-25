---
name: clawjournal-score
description: Score coding agent sessions for failure value and productivity. Use when the user asks to score a session, score sessions, re-score, auto-score, batch score, or evaluate traces. Triggers on "score sessions", "rate my traces", "quality check", or scoring requests.
metadata:
  argument-hint: "<session-id>"
---

# Score Sessions

## Quick Path: Batch Auto-Score

If no session ID was provided, suggest the automated approach first:

```bash
# Score in-scope failure-value traces automatically (recommended)
clawjournal score --batch --source failure-v1 --auto-triage --limit 20

# Or without auto-triage:
clawjournal score --batch --source failure-v1 --limit 20
```

For hands-on scoring of a specific session, continue below.

## Session Data

Run this to view the session:

```bash
clawjournal score-view <session-id>
```

If no session ID was provided, list available sessions:

```bash
clawjournal score --batch --limit 10
```

## Scoring Rubric (1-5)

ClawJournal now records two scores in one pass:

- `ai_quality_score`: legacy productivity/substance score.
- `ai_failure_value_score`: primary capture score for SOTA-agent failure behavior.

Failure value:

**5 = Clear consequential failure** — Real expert work, strong evidence, useful lesson.

**4 = Meaningful failure/recovery** — Worth reviewing or turning into eval/training data.

**3 = Some signal** — Ambiguous, minor, repeated, or mostly environmental.

**2 = Weak signal** — Routine retry, expected debugging, or unclear attribution.

**1 = No useful failure signal** — Smooth success, trivial session, control sample, or noise.

### Labels

Recovery labels: `self_recovered`, `user_corrected_recovery`, `unrecovered`, `blocked`.

Failure attribution: `agent_caused`, `environment`, `preexisting_problem`, `user_redirect`, `unclear`.

Failure modes: `wrong_approach`, `wrong_assumption`, `false_success`, `regression`, `instruction_violation`, `excessive_work`, `blocker_mishandled`.

### Detailed rubric

See `RUBRIC.md` in this directory for the full scoring rubric with examples. In the repo, this file is generated from `clawjournal/prompts/agents/scoring/rubric.md`; edit the canonical prompt copy first, then run `python -m clawjournal.prompt_sync`.
- How to read user feedback signals and final user response
- How to score failure value without punishing normal debugging
- STEM/domain correction evidence
- Seven failure modes and recovery labels
- Structured output fields

## Store the Score

Manual score setting only updates the legacy productivity score. Prefer
`clawjournal score` for failure-value annotations.

```bash
clawjournal set-score <session-id> --quality <score> --reason "<1-2 sentence explanation>"
```
