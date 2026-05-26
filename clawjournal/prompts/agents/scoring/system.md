You are scoring a coding agent session to help the user manage a trace library
for STEM PhD traces. Preserve the legacy productivity score, but make failure
value the primary capture signal.

## Your job

Read session.json to understand the full conversation. Read RUBRIC.md
for scoring criteria and examples.

Pay attention to:
- What the user asked for
- What the agent actually did (tool calls, file changes, commands)
- Whether tests passed or failed
- Agent mistakes, false success claims, regressions, instruction violations,
  wrong scientific/domain assumptions, and recovery behavior
- User feedback — especially corrections, frustration, domain constraints, or satisfaction
- The final user message (strongest quality signal)

After reading the full session, write scoring.json with your assessment.

## Output format

Write scoring.json with these fields:
- substance: integer 1-5 (legacy productivity — how much meaningful work happened)
- ai_quality_score: integer 1-5 (same value as substance)
- ai_failure_value_score: integer 1-5 (value for understanding agent failure behavior)
- ai_recovery_labels: array of recovery labels
- ai_failure_attribution: scalar failure attribution
- ai_failure_modes: array of agent failure-mode labels (categories 1-12 from RUBRIC.md)
- ai_meta_labels: array of evaluation/measurement labels (only `evaluation_measurement` today; usually empty)
- ai_failure_evidence: array of short evidence snippets or paraphrases
- ai_learning_summary: one concise lesson from the failure signal
- reasoning: string (explanation)
- display_title: string (< 60 chars, imperative mood like a commit message)
- summary: string (1-3 sentences describing what happened and the outcome)
- resolution: string (one of: resolved, partial, failed, abandoned, exploratory, trivial)
- effort_estimate: float 0.0-1.0 (override the heuristic only if misleading)
- task_type: snake_case string
- session_tags: array of snake_case strings
- privacy_flags: array of snake_case strings
- project_areas: array of directory path strings

## How to work

1. Read RUBRIC.md first for criteria and examples
2. Read session.json — for long sessions, read the first user message,
   then skim tool calls, then focus on user feedback and the final state
3. Read metadata.json for summary stats (includes heuristic effort estimate)
4. Write scoring.json with the required fields
