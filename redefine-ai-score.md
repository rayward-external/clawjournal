# Redefine AI Score

Status: draft for iteration
Last updated: 2026-05-26

Recent decisions:
- 5/5 does not require `agent_caused`; gate on whether agent behavior is the lesson.
- Scores of 4 or 5 require at least one `ai_failure_evidence` snippet.
- Hide the legacy `ai_quality_score` in the UI; keep it in the data model and CLI.

## Problem

ClawJournal originally used a 1-5 AI score as a productivity/substance rating:
"is this session worth keeping as a work trace?"

Our current focus is different. We care most about why agents fail, how they
recover, and which traces are valuable for improving frontier coding agents. A
generic "quality" or "productivity" star rating is now misleading because:

- a failed session can be extremely valuable;
- a recovered session can be more valuable than a smooth success;
- a long productive session can have little failure-reason value;
- a low-quality session is not automatically useful if the cause is unclear;
- failure reasons should be evidence-backed labels, not vague dissatisfaction.

The visible 1-5 score should therefore mean "failure value", not "agent quality"
or "user satisfaction".

## Proposed Decision

Make the primary user-facing 1-5 score the failure-value score:

> How useful is this trace for understanding and improving agent failure
> behavior on real work?

Keep the existing productivity score as a secondary/legacy field. Do not reuse
one field for both meanings.

Current field split:

| Field | Meaning | Product role |
| --- | --- | --- |
| `ai_failure_value_score` | 1-5 value for failure-corpus review | Primary score |
| `ai_quality_score` | 1-5 productivity/substance legacy score | Hidden in UI; kept in data model and CLI |
| `ai_failure_modes` | What kind of failure happened | Failure reason labels |
| `ai_failure_attribution` | Dominant cause of the key failure | Failure reason context |
| `ai_recovery_labels` | Whether and how recovery happened | Failure/recovery context |
| `ai_failure_evidence` | Trace-backed evidence snippets | Label support |
| `ai_learning_summary` | One concise lesson from the trace | Human review summary |
| `ai_meta_labels` | Evaluation/scoring artifacts | Meta-level caveats |

## Score Semantics

The 1-5 scale should rank traces by corpus value, not by how badly the agent did.

| Score | Label | Meaning |
| --- | --- | --- |
| 1 | No signal | No useful failure signal: smooth success, trivial session, control sample, or noise. |
| 2 | Weak signal | A possible failure exists, but it is routine, unclear, expected debugging, or low-value. |
| 3 | Usable signal | There is a real failure signal, but it is minor, ambiguous, repeated, mostly environmental, or only partly attributable. |
| 4 | Strong pattern | Meaningful agent failure or recovery pattern with clear evidence and a useful lesson. Worth review and likely share/export consideration. |
| 5 | Canonical failure trace | Consequential, evidence-rich failure on real expert work. Strong lesson for evals, training data, product design, or agent behavior analysis. |

Important implications:

- A resolved session can be `5/5` if it contains a valuable failure and recovery
  pattern.
- A failed session can be `2/5` if the trace does not show a clear, useful, or
  attributable failure reason.
- A smooth success should usually be `1/5` for failure value, even if it is
  `5/5` productivity.
- A blocked session is not automatically valuable. It needs to reveal something
  about the agent's handling of constraints, uncertainty, collaboration, or
  recovery.
- Scores of `4` or `5` require at least one `ai_failure_evidence` snippet. If no
  snippet can be quoted, fall back to `3`.
- `5/5` does not require `agent_caused` attribution. A non-agent-caused trace
  can score `5/5` if the agent's behavior (recovery, uncertainty handling,
  collaboration, escalation) is itself the lesson. Treat `agent_caused` as a
  category inside `ai_failure_attribution`, not a gate on the score.

## Failure Reasons Are Separate From The Score

The score answers: "How valuable is this trace for the failure corpus?"

Failure reasons answer: "What happened, based on trace evidence?"

That distinction matters. The score should not become a disguised category. A
trace can have the same failure reason as another trace but a different score
because one has clearer evidence, stronger consequences, better recovery signal,
or more reusable lessons.

Failure reason labels should require concrete trace evidence. Do not label a
reason from:

- benchmark rank alone;
- aggregate metrics alone;
- "the user seemed unhappy" without a specific trace signal;
- final bad outcome without visible agent behavior;
- normal iteration that caused no meaningful burden, harm, or misleading output.

## UI Direction

The product surface should make failure value the primary review lens.

Recommended changes:

1. Rename the main score panel from "Productivity Score" to "Failure Value".
2. Show `ai_failure_value_score` as the primary 1-5 score in session detail.
3. Place failure modes, attribution, recovery labels, evidence, and learning
   summary directly under the failure score.
4. Hide the legacy productivity score (`ai_quality_score`) in the UI. Keep it
   in the data model and CLI/exports for backwards compatibility — showing two
   1-5 scores with different meanings side-by-side confuses reviewers.
5. In list views, sort by failure value by default and label the sort as "Top
   failure value" or "Most valuable failures".
6. In CLI output, lead with failure value:
   `failure value 4/5; productivity 3/5`.
7. Avoid presenting failure value as generic gold stars if that makes it feel
   like praise. Consider `FV 4/5`, a severity-style badge, or a compact numeric
   meter instead.

## Dashboard And List View

The dashboard and list view are the primary review surfaces. Their job is
triage — "what should I review next?" — not a leaderboard.

Reframing first: under the new score, "best cases" and "failure cases" overlap.
A canonical 5/5 trace *is* a best case (best for the corpus). There is no
separate "top successes" ranking. Smooth successes appear only as an ambient
counter, not a parallel ranked surface.

### Dashboard

Four tiles, nothing more:

1. Failure-value distribution — small 1-5 histogram of recent traces.
2. Top failure modes — top 5 by count.
3. Top recovery patterns — top 5 by count.
4. Recent 4-5s — the review inbox.

Plus one ambient line at the top: `N sessions this week, M smooth successes`.
Baseline activity, no ranked board.

### List view

One default surface, sorted by failure value desc. Each row shows four things:

- failure value badge (primary);
- outcome chip (success / partial / fail / blocked);
- one top `ai_failure_modes` tag;
- one-line `ai_learning_summary` excerpt.

Filters: failure value, failure mode, recovery label, outcome.

Anything denser turns the list into a spreadsheet.

### What to resist

Trying to make the list view serve every lens at once — recency, productivity,
failure, outcome. Lock failure-value as the default lens and let timeline /
outcome live as filters or alternate sorts, not parallel default surfaces. Two
ranked boards always splits attention and never pays for the complexity.

Sharp tradeoff: a user who wants "what happened this week, all of it" needs a
separate timeline view. Fine to build, but keep it clearly marked as a second
lens — don't let it leak into the dashboard default. The dashboard's job is
"what should I review next," not "what did I do."

### Open question

- Should the dashboard scope to "all time" or "last 7 days" by default? A
  7-day default makes the histogram and recent feed feel alive; all-time is
  stable but may underweight recent regressions and starve low-volume users.

## CLI And Data Model Direction

Keep the current columns and add behavior around them rather than renaming the
schema immediately.

Near-term behavior:

- `score` should continue emitting both `ai_failure_value_score` and
  `ai_quality_score`.
- `score-view` should lead with failure value and failure reasons.
- `set-score --quality` should remain a legacy productivity override.
- Add or consider a manual failure-value override, for example:
  `set-score --failure-value 4`.
- Auto-triage should not hide a trace solely because productivity is low if
  failure value is high.

Provenance remains important when changing score meaning:

- preserve scorer backend;
- preserve scorer model;
- preserve rubric revision;
- preserve scoring timestamp;
- make rescoring explicit when the rubric meaning changes materially.

## Candidate Rubric Text

This can replace or sharpen the current failure-value section:

```text
Failure value score (1-5) measures how useful the trace is for understanding
and improving coding-agent failure behavior. It is not a quality score and not a
severity score.

5 = Canonical high-value trace: consequential failure or recovery pattern on
real expert work, where the agent's behavior is the lesson. Strong trace-backed
evidence and a reusable insight for evals, training, product design, or agent
behavior analysis. Attribution may be agent-caused or non-agent-caused.

4 = Strong failure or recovery pattern: meaningful trace-backed agent behavior
worth reviewing and likely worth sharing/exporting.

3 = Usable failure signal: real signal exists, but it is minor, ambiguous,
common, mostly environmental, or only partly attributable.

2 = Weak signal: routine retry, expected debugging, unclear attribution, or
little reusable lesson.

1 = No useful failure signal: smooth success, trivial session, control sample,
or noise.

Evidence and attribution rules:
- Scores of 4 or 5 must be backed by at least one `ai_failure_evidence` snippet.
  If no snippet can be quoted, fall back to 3.
- 5/5 does not require `agent_caused`. The teaching moment matters, not the
  source of the failure.
- Do not infer failure value from bad outcome alone. Score by the clearest
  trace-backed teaching moment. A resolved session can score high; a failed
  session can score low.
```

## Open Questions

1. Should the visible score use stars at all, or should failure value use a
   different visual language?
2. Should high failure value require real user work, excluding synthetic evals
   unless explicitly marked as evaluation data?
3. Should we derive a `corpus_ready` state from failure value, evidence, hold
   state, and privacy gates?
4. Should low-productivity/high-failure-value traces bypass productivity-based
   auto-blocking? (Largely moot for the UI now that the legacy score is hidden,
   but the underlying auto-triage logic may still need a pass.)

## Implementation Sketch

If we implement this direction, the smallest useful path is:

1. Update Session Detail so `ai_failure_value_score` is the primary score panel.
2. Hide the legacy productivity score in the UI. Keep it in the data model and
   CLI for backwards compatibility.
3. Update CLI text and README language to stop treating "quality" as the main
   score.
4. Add manual override support for failure value.
5. Review share recommendations, dashboard averages, and insights so they rank
   by failure value where appropriate.
6. Re-run frontend build if frontend files change.

