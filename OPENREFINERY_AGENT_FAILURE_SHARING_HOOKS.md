# OpenRefinery Agent Failure Sharing Hooks

## Purpose

OpenRefinery Agent Failure Sharing is an enrollment-only reminder workflow for
participants who have already agreed to share agent failure traces for research.
It gives Claude Code and Codex a daily nudge similar to the built-in Claude Code
session-quality prompt, but routes the participant through ClawJournal's local
review and redaction workflow instead of uploading anything directly.

The hook is intentionally narrow:

- It prompts at most once per local day.
- It can open the ClawJournal Share UI or point to the terminal Share wizard.
- It never packages, submits, or uploads traces by itself.
- It leaves ClawJournal's existing source/project confirmation, hold-state,
  redaction, AI-PII opt-in, and TruffleHog gates as the only share path.

## Participant Workflow

For a new participant, the enrollment instructions should first install or
update ClawJournal from the public GitHub source:

```bash
git clone https://github.com/rayward-external/clawjournal.git ~/clawjournal
cd ~/clawjournal
./scripts/install.sh --with-frontend
```

Once `clawjournal` is available, enroll the participant:

```bash
clawjournal enroll openrefinery --agent all --ui auto
```

That command:

1. Runs the safe synchronous updater (`clawjournal selfupdate`) when the install
   is an editable Git checkout.
2. Installs Stop hooks for Claude Code and Codex.
3. Enables the local `openrefinery-failures` state profile.
4. Sets the source scope to `both` so Claude Code and Codex traces are in scope.
5. Does not mark projects confirmed. The participant still reviews project
   scope before sharing.

When the daily hook fires, it asks the agent to prompt:

```text
OpenRefinery Agent Failure Sharing is enabled for your research enrollment.
ClawJournal can review and redact your recent agent failure traces locally, then
submit them for OpenRefinery research.

Review now?  y: Review and submit  n: Later  d: Pause reminders
```

If the participant chooses `y`, the agent should run:

```bash
clawjournal hooks launch openrefinery-failures
```

If the participant chooses `d`, the agent should run:

```bash
clawjournal hooks snooze openrefinery-failures --days 30
```

## Installed Files

The hook installer writes only user-level agent config and ClawJournal state:

| Path | Purpose |
|---|---|
| `~/.claude/settings.json` | Claude Code Stop hook definition |
| `~/.codex/hooks.json` | Codex Stop hook definition |
| `~/.clawjournal/hooks/openrefinery-failures.json` | Local profile state, daily prompt cap, prompt counts, snooze status |
| `~/.clawjournal/config.json` | Existing ClawJournal config; enrollment sets `source` to `both` |

The hook command installed into both agents is:

```bash
<python> -m clawjournal.cli hooks run openrefinery-failures --client <claude|codex>
```

Using the current Python executable keeps the hook tied to the installed
ClawJournal environment rather than relying on whatever `clawjournal` binary
happens to be first on `PATH`.

## Commands

| Command | Use |
|---|---|
| `clawjournal enroll openrefinery --agent all --ui auto` | Update if possible, install Claude+Codex hooks, enable the profile |
| `clawjournal hooks install openrefinery-failures --agent all --ui auto` | Install or repair hooks without running selfupdate |
| `clawjournal hooks status openrefinery-failures` | Show enabled state, installed agents, daily cap, prompt count, snooze date |
| `clawjournal hooks launch openrefinery-failures` | Open Share UI or print terminal wizard fallback |
| `clawjournal hooks snooze openrefinery-failures --days 30` | Pause daily reminders |
| `clawjournal hooks disable openrefinery-failures` | Keep hook files but stop prompting |
| `clawjournal hooks uninstall openrefinery-failures --agent all` | Remove hook definitions from agent config and disable if no agents remain |
| `clawjournal hooks run openrefinery-failures --client codex --json` | Diagnostic hook invocation |

## Hook Runtime Behavior

The hook reads only local state and emits a response for the running agent.

Prompt eligibility:

1. The profile must be enabled.
2. `CLAWJOURNAL_DISABLE_SHARE_NUDGE=1` and
   `OPENREFINERY_SHARE_HOOK_DISABLE=1` must not be set.
3. The profile must not be snoozed through today.
4. The profile must not have reached `max_prompts_per_day` for today's local
   date unless `--force` is used.

When eligible, the hook increments `prompt_counts_by_date[today]` and records
`last_prompt_date` before returning the prompt. The default cap is 10 prompts
per day. This cap is shared across Claude Code and Codex: if both agents are
used on the same day, they draw from the same daily prompt budget.

## Agent-Specific Prompt Semantics

Claude Code and Codex both support Stop hooks, but they present hook output
differently.

For Claude Code, the hook returns JSON with:

- `hookSpecificOutput.hookEventName: "Stop"`
- `hookSpecificOutput.additionalContext: <prompt text>`

This asks Claude Code to continue the turn with the participant-facing prompt
without labeling the response as a Stop-hook error.

For Codex, the hook returns:

- `decision: "block"`
- `reason: <prompt text>`

Codex may also require the participant to review and trust the newly installed
hook through `/hooks` before non-managed user hooks run.

## Launch Behavior

`clawjournal hooks launch openrefinery-failures` uses the enrolled UI preference:

- `web`: require the browser workbench, start `clawjournal serve --no-browser`
  detached when needed, then open `http://localhost:8384/share`.
- `cli`: print `clawjournal share --interactive --weekly`.
- `auto`: prefer the web path, but fall back to the CLI wizard if the frontend is
  not built, the daemon cannot start, or the port does not become reachable.

The detached server launcher intentionally starts only the existing
ClawJournal daemon. It does not create a special upload path or bypass any
Share workflow step.

## Privacy and Safety Invariants

The hook design preserves the existing local-first privacy model:

- Source and project confirmation remain required before export/share.
- `pending_review` and active `embargoed` sessions remain blocked from upload.
- Only `auto_redacted` and `released` sessions can leave the machine.
- Regex redaction runs at share time even if findings were already computed.
- AI-assisted PII review remains optional and explicit.
- TruffleHog remains mandatory after redaction and blocks missing-binary or
  detected-secret cases.
- The hook itself does not read session transcripts, package bundles, submit
  HTTP requests, or call any AI backend.

## Failure Modes

| Failure | Behavior |
|---|---|
| ClawJournal is not installed yet | Enrollment cannot run; use the GitHub install path first |
| Editable checkout is dirty, ahead, diverged, or not on `main` | `enroll openrefinery` reports the `selfupdate` status but still installs hooks |
| Existing hook JSON is malformed | Installer fails with a user-actionable parse error |
| Browser workbench frontend is missing | `--ui auto` falls back to CLI; `--ui web` errors with rebuild guidance |
| Workbench port does not become reachable | `--ui auto` falls back to CLI; `--ui web` errors |
| Participant wants quiet | `hooks snooze ... --days 30`, `hooks disable ...`, or env opt-out |
| Codex hook is untrusted | Codex prompts for `/hooks` review before running the hook |

## Tests

The implementation is covered by `tests/test_openrefinery_hooks.py`:

- Claude and Codex hook JSON installation.
- Existing hook update without clobbering unrelated hooks.
- Shared daily prompt cap.
- Snooze suppression.
- Claude-specific `additionalContext` response.
- Web launch, CLI fallback, and missing-frontend fallback.
- Uninstall removes only the OpenRefinery hook.

The PR also keeps existing CLI and self-update coverage passing:

```bash
python -m pytest tests/test_cli.py tests/test_selfupdate.py tests/test_openrefinery_hooks.py -q
python -m py_compile clawjournal/openrefinery_hooks.py clawjournal/cli.py tests/test_openrefinery_hooks.py
```
