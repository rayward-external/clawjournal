# Redefine AI Score

Status: implementation proposal
Last updated: 2026-05-26

Target decisions:
- 5/5 does not require `agent_caused`; gate on whether agent behavior is the lesson.
- Scores of 4 or 5 require at least one `ai_failure_evidence` snippet.
- Hide the legacy `ai_quality_score` from primary UI surfaces; keep it in the
  data model, CLI, and exports for compatibility.

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

Target field split:

| Field | Meaning | Product role |
| --- | --- | --- |
| `ai_failure_value_score` | 1-5 value for failure-corpus review | Primary score |
| `ai_quality_score` | 1-5 productivity/substance legacy score | Hidden from primary UI surfaces; kept in data model, CLI, and exports |
| `ai_failure_modes` | What kind of failure happened | Failure reason labels |
| `ai_failure_attribution` | Dominant cause of the key failure | Failure reason context |
| `ai_recovery_labels` | Whether and how recovery happened | Failure/recovery context |
| `ai_failure_evidence` | Trace-backed evidence snippets | Label support in `ai_scoring_detail` and exports |
| `ai_learning_summary` | One concise lesson from the trace | Human review summary |
| `ai_meta_labels` | Evaluation/scoring artifacts | Meta-level caveats in `ai_scoring_detail` and exports |

Current repo state:

- The schema already stores `ai_failure_value_score`, `ai_failure_modes`,
  `ai_failure_attribution`, `ai_recovery_labels`, and `ai_learning_summary`.
  `ai_failure_evidence` and `ai_meta_labels` live inside `ai_scoring_detail`
  and are included in export/share payloads.
- The scorer already emits both `ai_quality_score` and
  `ai_failure_value_score`. The canonical rubric prompt
  (`clawjournal/prompts/agents/scoring/rubric.md`) reflects the new semantics
  (5/5 does not require `agent_caused`; 4-5 require evidence).
- Scorer validation enforces the evidence cap at
  `clawjournal/scoring/scoring.py:1090`: a failure-value score of 4 or 5 with
  no `ai_failure_evidence` is downgraded to 3.
- `set-score --failure-value` exists for manual failure-value overrides.
- The workbench list (`TraceCard.tsx`), session detail score panel, share
  recommendations, and recent highlights now use failure value as the primary
  review score.
- The legacy `ai_quality_score` is still visible on four UI surfaces that
  need follow-up migration:
  - `web/frontend/src/views/SessionDetail.tsx:582-587` renders
    `Legacy productivity {score}/5` next to the failure-value panel.
  - `web/frontend/src/views/Dashboard.tsx:230` computes
    `totalProductivityScored` and renders a `by_quality_score` histogram
    alongside `by_failure_value_score`.
  - `web/frontend/src/views/Insights.tsx:169` falls back to
    `ai_quality_score` when `ai_failure_value_score` is null, so quality
    still drives ranking on legacy traces.
  - `web/frontend/src/views/Share.tsx` (and the `api.ts` / `types.ts`
    contracts) keep `ai_quality_score` in the share-ready payload.

## Score Semantics

The 1-5 scale should rank traces by corpus value, not by how badly the agent did.

| Score | Label | Meaning |
| --- | --- | --- |
| 1 | No signal | No useful failure signal: smooth success, trivial session, control sample, or noise. |
| 2 | Weak signal | A possible failure exists, but it is routine, unclear, expected debugging, or low-value. |
| 3 | Usable signal | There is a real failure signal, but it is minor, ambiguous, repeated, mostly environmental, or only partly attributable. |
| 4 | Strong pattern | Meaningful agent failure or recovery pattern with clear evidence and a useful lesson. Worth review and likely share/export consideration. |
| 5 | Canonical failure trace | Consequential, evidence-rich failure or recovery pattern on real expert work. Strong lesson for evals, training data, product design, or agent behavior analysis. |

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
  snippet can be quoted, fall back to `3`. The scorer validation path enforces
  this cap after parsing judge output.
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

Recommended UI changes:

1. Rename the main score panel from "Productivity Score" to "Failure Value".
2. Show `ai_failure_value_score` as the primary 1-5 score in session detail.
3. Place failure modes, attribution, recovery labels, evidence, and learning
   summary directly under the failure score.
4. Hide the legacy productivity score (`ai_quality_score`) from primary UI
   surfaces. Keep it in the data model and CLI/exports for backwards
   compatibility; showing two 1-5 scores with different meanings side-by-side
   confuses reviewers.
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

## CLI And Data Model Direction

Keep the current columns and add behavior around them rather than renaming the
schema immediately.

Near-term behavior:

- `score` should continue emitting both `ai_failure_value_score` and
  `ai_quality_score`.
- `score-view` should lead with failure value and failure reasons.
- `set-score --quality` should remain a legacy productivity override.
- Use the manual failure-value override when human review needs to correct the
  score:
  `set-score --failure-value 4`.
- Auto-triage should not hide a trace solely because productivity is low if
  failure value is high. `--auto-triage` should only auto-block productivity-1
  sessions when failure value is below 4. Rationale: failure value 3 is
  "usable signal" worth keeping reviewable; 4-5 is share-worthy. So 4 is the
  cutoff between "could be reviewed later" and "definitely keep in the queue."

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
4. Is "below 4" the right auto-triage cutoff? The current proposal keeps
   productivity-1 + failure-value-3 traces reviewable; an alternative is
   "below 3" (only auto-block productivity-1 + failure-value 1-2). Trade-off:
   reviewer queue size vs. capturing 3/5 "usable but minor" signals.
5. Should the dashboard scope to "all time" or "last 7 days" by default? A
   7-day default makes the histogram and recent feed feel alive; all-time is
   stable but may underweight recent regressions and starve low-volume users.

## Implementation Sketch

### Already shipped

1. Canonical rubric prompt updated
   (`clawjournal/prompts/agents/scoring/rubric.md`) — the failure-value
   section, the 5/5 no-`agent_caused` rule, and the 4-5 evidence requirement
   are all in the live judge prompt.
2. Scorer validation enforces the evidence cap at
   `clawjournal/scoring/scoring.py:1090`: a score of 4 or 5 with no
   `ai_failure_evidence` is downgraded to 3.
3. Session Detail leads with the failure-value panel; failure modes,
   attribution, recovery, evidence, and learning summary surface directly
   under it.
4. Manual failure-value override: `set-score --failure-value <n>`.
5. Workbench list (`TraceCard.tsx`), share recommendations, and recent
   highlights rank by failure value.

### Remaining

1. **Hide the legacy productivity score from primary UI surfaces.** Four
   concrete touch-points (see "Current repo state" above for exact line
   refs):
   - Remove or gate the `Legacy productivity {score}/5` badge in
     `SessionDetail.tsx:582-587`.
   - Drop or relabel the `by_quality_score` histogram in
     `Dashboard.tsx:230` (and stop computing `totalProductivityScored` for
     the primary surface).
   - In `Insights.tsx:169`, stop falling back to `ai_quality_score` for
     the primary score lens — render "no failure score yet" for legacy
     traces instead.
   - In `Share.tsx`, drop `ai_quality_score` from the share-ready UI;
     keep it in the API contract (`api.ts` / `types.ts`) for backwards
     compatibility.
2. **Update CLI text and README language** so "quality" is no longer the
   main score; lead with failure value in `score-view` output.
3. **Auto-triage gate.** Implement the "below 4" rule so productivity-1
   sessions with high failure value are not auto-blocked.
4. **Migrate "quality" copy in the Insights recommendations engine.** The
   recommendation cards rendered on the Insights view still read "best
   quality score", "Model quality vs cost trade-off", and "Highest quality:
   …". Source:
   - `clawjournal/scoring/insights.py:292` — `"{type} work has the best
     quality score"` title.
   - `clawjournal/scoring/insights.py:310` — `"Model quality vs cost
     trade-off"` title.
   - `clawjournal/scoring/insights.py:312` — `"Highest quality: {model}
     ({avg_score:.1f}/5 avg, ...)"` body.
   - `clawjournal/cli.py:1997` — matching CLI line `Highest quality:
     {summary['highest_quality_model']}`.

   Decide whether the recommendation should switch to failure-value
   framing or to a neutral "score" framing, and whether the underlying
   metric should still be `ai_quality_score` or move to
   `ai_failure_value_score` (the recommendation logic likely needs the
   same migration, not just the label).
5. **Re-run the frontend build** once the UI surfaces above are reworked —
   the CI smoke job verifies the built wheel ships
   `clawjournal/web/frontend/dist/index.html`.
