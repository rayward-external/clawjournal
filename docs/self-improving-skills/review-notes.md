# Self-Improving Skills - Review Notes

> Perspective: product/implementation feedback on the current Mode A design.
> This note does not replace `plan.md`; it clarifies a few operational decisions
> that should stay explicit before implementation starts.

## Summary

The design is directionally sound: ClawJournal should not try to replace Claude
Code or Codex memory. It should act as a high-signal distillation layer over the
user's own cross-agent history, then write a small reviewed lesson set back into
the native agent surfaces.

My main recommendation is to keep the first implementation deliberately narrow:
distill only after scoring has produced an eligible corpus and only after the
user explicitly asks to generate lessons. The installed lesson set should stay
capped at five active rules, while ClawJournal keeps a local history of approved,
rejected, installed, replaced, and resurfaced candidates for future ranking.

## 1. Trigger Timing

Distillation should not run on application startup.

Startup is too early and too implicit for an operation that may call an AI
backend and may later affect an agent's long-lived instruction surface. At
startup, the corpus may not be scanned, scoring may be incomplete, and the user
has not expressed intent to generate or install lessons.

Recommended flow:

```text
scan/index completes
-> scoring completes
-> eligible scored sessions exist
-> user clicks Generate Lessons or runs `clawjournal skill`
-> select candidates
-> anonymize and scrub secrets before any AI call
-> distill
-> deterministic gates
-> preview
-> user confirms install
-> write to Claude Code / Codex surfaces
```

Scoring should make sessions eligible. The explicit Generate Lessons action
should start distillation.

Reasoning:

- Distillation is an AI egress, even when routed through the user's own agent CLI.
- The output can influence future Claude Code or Codex behavior.
- The operation depends on scored sessions, not just raw indexed logs.
- A user-facing preview is part of the trust boundary, not a cosmetic step.
- A weekly refresh should prompt the user rather than silently install changes.

Good background behavior:

- On startup, show a lightweight "new scored sessions available" prompt if useful.
- After scoring, surface a Generate Lessons action.
- On a weekly cadence, prepare or suggest a refresh, but still require preview and
  confirmation before install.

Avoid:

- Distilling on app startup.
- Distilling immediately after every scan.
- Distilling after every single session.
- Installing without preview.

## 2. Five Active Lessons vs Local Archive

The five-rule cap is reasonable if it is treated as the active context budget,
not as the total memory budget.

Claude Code and Codex already have instruction and memory surfaces. ClawJournal
should be conservative about what it injects into those surfaces. Too many
lessons will increase review burden, dilute attention, and raise the risk of
stale or conflicting guidance.

Recommended model:

```text
Active lessons: <= 5 rules installed into agent surfaces
Local archive: many candidates and historical rule states kept in ClawJournal
```

The current design already implies this through durable fields such as
`fingerprint`, `approved_at`, `rejected_at`, `installed_at`, `last_seen_at`, and
`evidence_session_ids`. It would be useful to state the model more directly:
ClawJournal remembers more than it installs.

Suggested lifecycle buckets:

- Candidate: proposed by the latest scored corpus.
- Approved: accepted by the user.
- Installed: currently present in Claude Code or Codex.
- Replaced: removed because a stronger candidate displaced it.
- Rejected: explicitly declined by the user and not resurfaced unchanged.
- Stale: no longer supported by recent evidence, but retained for history.

Weekly refresh should rank new candidates together with existing active lessons.
If a new rule outranks the weakest active rule, the preview should show a
replacement diff. The displaced rule should remain in local history rather than
being forgotten.

This keeps the installed surface small while preserving enough local state to
avoid repetitive proposals and to support future re-ranking.

## 3. Relationship To Native Agent Memory

Claude Code and Codex already have long-lived instruction or memory-like
surfaces. ClawJournal should not position this feature as "adding memory where no
memory exists."

The better positioning is:

> ClawJournal safely distills high-signal lessons from cross-agent history and
> delivers the current top lessons into each agent's native instruction surface.

That distinction matters. Native memory is good for preferences, project notes,
and manually maintained guidance. ClawJournal's value is the scoring, recurrence
analysis, privacy gates, review step, and cross-agent aggregation.

Implementation implication:

- Do not grow an unbounded memory file.
- Do not write broad preference notes that native memory can already handle.
- Focus on repeated failure, recovery, rejection, and tool-error patterns.
- Keep generated lessons concrete, short, and evidence-backed.

## 4. Error Signals And Judge Reliability

If a Claude Code session contains the original error and Claude is also used as
the judge, the judge may identify obvious failures, but it should not be treated
as the only source of truth.

The strongest signals are the ones where the user or environment forced a
correction:

- Explicit user correction or rejection.
- Follow-up instruction that redirects the agent.
- Test, build, or command failure.
- Tool error or repeated environment error signature.
- Permission denial or rejected action.
- Agent recovery after a failed approach.

The scorer should use the judge as a synthesis layer over these signals, not as a
replacement for them.

Recommended scoring posture:

```text
Prefer: "the transcript shows the user/environment forced a correction"
Avoid: "the model thinks the model was probably wrong"
```

Where practical, cross-backend scoring can reduce same-model blind spots. For
example, Claude sessions can be judged by Codex and Codex sessions by Claude.
This should be optional rather than required for Mode A.

## 5. Suggested Clarifications To `plan.md`

I would make three small clarifications in the design before implementation:

1. Distillation is triggered by an explicit Generate Lessons action after scoring
   has produced an eligible corpus; startup and scan should not trigger it.
2. The five-rule cap applies to active installed lessons, not to all locally
   retained candidates or historical lesson states.
3. User/environment correction signals are primary evidence; judge scoring is a
   structured interpretation layer over that evidence.

These clarifications preserve the current Mode A design while making the
implementation boundary easier to enforce.

