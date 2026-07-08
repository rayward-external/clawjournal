# Automatic Failure-Corpus Scoring

## Summary

ClawJournal should keep share-ready failure traces warm automatically. When a
student opens the local workbench, the daemon starts a background scoring warmup
for the latest 20 unscored `failure-corpus` sessions. The UI remains usable
while scoring runs.

`failure-corpus` is the public scoring source preset for `claude`,
`claude-science`, `codex`, `opencode`, and `openclaw`. The older `failure-v1`
alias remains accepted for backward compatibility.

## Behavior

- `clawjournal serve` runs the normal initial scan, then triggers background
  scoring.
- The frontend also calls `POST /api/scoring/warmup` once on app load so an
  already-running daemon can start scoring when the browser is opened or
  reloaded.
- Warmup scores the latest 20 unscored `failure-corpus` sessions by
  `start_time DESC`.
- Warmup applies the same AI-egress gates as the skill/share path before
  scoring: held or active-embargo sessions are skipped, excluded-project
  policies are skipped, and configured redaction strings/usernames/domains are
  applied to the scoring prompt.
- Warmup uses the existing scorer lock, so repeated browser reloads cannot
  start duplicate scoring jobs.
- Share-ready recommendations continue to require `ai_failure_value_score`.
  Unscored sessions are not recommended.
- Bundle export and upload never run AI scoring inline.

## Backend Selection

Warmup detects the current agent context first. If no current agent is detected,
it falls back to installed CLIs in this order:

1. `codex`
2. `claude`
3. `hermes`
4. `openclaw`

If the user has not confirmed a backend before, warmup returns
`needs_confirmation`. The frontend asks once, then stores the confirmed backend
in `~/.clawjournal/config.json`.

The background warmup never auto-confirms a backend — it only starts after an
explicit confirmation. Users who never open the workbench (CLI-only) can confirm
the headless equivalent so the daemon's background scoring can run:

```bash
clawjournal config --scorer-backend codex   # confirm; persists to config.json
clawjournal config --scorer-backend none    # clear the confirmation
```

`CLAWJOURNAL_SCORER_BACKEND` is also honored and takes precedence over the
stored confirmation.

Hermes Agent (`https://github.com/NousResearch/hermes-agent`) is supported as a
scoring backend only. ClawJournal invokes Hermes' scripted one-shot CLI mode and
parses JSON from stdout. Stdout parsing tolerates markdown fences or surrounding
prose by falling back to the outermost `{...}` span.

## Manual Commands

```bash
clawjournal score --batch --source failure-corpus --limit 20
clawjournal score --batch --source failure-corpus --window 7d --limit 50
clawjournal rescore --source failure-corpus --window 7d --limit 50
```

## Self-Improving Skills

Automatic warmup is the feeder, not the installer. It keeps recent
failure-corpus traces scored so `clawjournal skill --preview` and
`clawjournal skill` have fresh local evidence to distill. Writing the generated
`clawjournal-lessons` skill to Claude Code or Codex remains an explicit
preview/approval step.
