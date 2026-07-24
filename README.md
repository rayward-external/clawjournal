# ClawJournal

ClawJournal helps you review the work you have done with coding agents, remove sensitive information, and choose what—if anything—you want to share.

It works with Claude Code, Claude Desktop, Claude Science, Codex, Gemini CLI, OpenCode, OpenClaw, Kimi CLI, WorkBuddy, Cursor, Copilot, and Aider.

## Start with a prompt

Copy the prompt that matches what you want to do into your coding assistant.

### Set it up for me

```text
Set up or safely update ClawJournal from
https://github.com/rayward-external/clawjournal.

I have joined the ClawJournal research program. Any data I later explicitly
choose to contribute through ClawJournal is for research purposes. Program
participation does not by itself authorize an upload or Automatic uploads.

First determine whether this machine supports a local browser UI or is
CLI-only/headless. If that is unclear, ask me before installing.

ClawJournal may already be installed even if `clawjournal` is not on my
PATH — check the default locations (`~/.clawjournal-venv` and
`~/clawjournal`) before treating this as a fresh install.

If it is not installed yet, install it with the project's installer for my
operating system: include the browser workbench in UI mode, skip it in
CLI-only mode, and include the managed sharing safety tools in both. If
something the installer needs (such as Git, Python, or Node.js) is missing,
help me install that first.

If it is already installed, run `clawjournal selfupdate --reinstall` once.
That single command brings everything — code, dependencies, workbench, and
safety scanners — to the latest published version, and does nothing if it is
already current.

Either way I must end up on the latest published version; verify that and
tell me if I am not. Never use force options, and never delete or overwrite
anything of mine to make an update succeed. If the installer or updater
reports a problem, show me its message, explain it in plain words, and stop.
After this setup, ClawJournal keeps itself up to date automatically, so I
should not need to repeat any of this.

Look across all supported coding agents unless I tell you otherwise. Set that
scope explicitly with `clawjournal config --source all`, then show me the
discovered projects before confirming them so I can exclude personal,
confidential, third-party, or unrelated work.

Use the exact ClawJournal executable printed by the installer; do not assume
it is on PATH. Scan my sessions and run `clawjournal status`.

For UI mode, start and open the local workbench. For CLI-only mode, show me
my recent sessions and explain the main review commands. Do not start a
sharing flow yet. If I later request terminal sharing, use
`clawjournal share --interactive --weekly --no-score` unless I explicitly
opt into AI scoring.

Keep this setup local. Do not run AI scoring, AI-assisted review, or
Automatic uploads. If any of those features is already enabled, tell me
before continuing. Do not upload anything.

Finish by reporting: the selected mode; the installed version and whether it
is the latest published one; the exact executable path; the scan result;
confirmation that automatic updates are active (or the plain-words reason
they are not); and the exact command I should use next.
```

This prompt confirms research-program participation and research intent, but it allows installation and local review only. You can decide whether to contribute data later.

### Open it for me

```text
Open my local ClawJournal workbench, make sure its session index is current,
and help me find the work I did recently. Do not run AI features or upload
anything. If either is already enabled, tell me before opening the workbench.

Before you run any ClawJournal command, make sure my install is current:
run `clawjournal selfupdate --check`, and if it is behind, bring it up to
date with `clawjournal selfupdate --reinstall`. Never discard local changes
or pass `--force`. If the update is blocked, tell me why and carry on with
the version I have.
```

### Help me review my work

```text
Open ClawJournal and help me review my recent coding-agent sessions.

Before you run any ClawJournal command, make sure my install is current:
run `clawjournal selfupdate --check`, and if it is behind, bring it up to
date with `clawjournal selfupdate --reinstall`. Never discard local changes
or pass `--force`. If the update is blocked, tell me why and carry on with
the version I have.

Keep the original session text in the local workbench, not in chat. Help me
identify useful sessions, exclude anything unrelated, and place anything
sensitive or uncertain on hold. Do not run AI features, prepare a share, or
upload anything.
```

### Help me share safely

```text
Open ClawJournal and help me prepare a small share from sessions I have
already reviewed.

Before you run any ClawJournal command, make sure my install is current:
run `clawjournal selfupdate --check`, and if it is behind, bring it up to
date with `clawjournal selfupdate --reinstall` so the redaction rules and
secret scanners are the latest ones. Never discard local changes or pass
`--force`. If the update is blocked, tell me why before you prepare anything.

Keep raw traces and secret values in the local workbench or terminal, not in
chat. Guide me through Queue, Redact, Review, Package, and Submit. Preserve
every hold, embargo, finding, redaction, consent, and secret-scan safeguard.
Do not bypass a failed or missing safety check.

Use the built-in redaction rules unless I separately ask for AI-assisted
review. I have joined the ClawJournal research program, and this one-time
package is intended as a research contribution. This statement does not
replace the consent shown at Submit. This request authorizes one manual share
only; it does not authorize Automatic uploads.

Before anything is uploaded, tell me:
- how many sessions are selected;
- which sources and projects they came from;
- whether redaction and secret scans passed, and whether AI review was used; and
- where the package would be sent.

Wait for my final confirmation before uploading. If hosted submission is
unavailable, save the package locally and explain the supported next step.
```

Sharing is always a separate decision. An assistant permission prompt is not the same as your final approval to upload.

## What you will see

1. **Choose projects.** ClawJournal finds your coding-agent sessions. Review the project list and exclude anything you do not want included.
2. **Review locally.** Use the browser workbench to search, approve, block, hold, or embargo sessions. No account or upload is required.
3. **Choose a share.** Select up to 50 reviewed sessions. Anything you do not select stays out of that package.
4. **Redact and check.** Inspect the redacted preview, remove sessions if needed, and run the required secret scans.
5. **Confirm.** Review the destination and consent details. Nothing is uploaded until you approve the final submission.

If you are unsure at any point, stop before **Submit**. Your review and package can remain on your computer.

## Privacy in plain English

- **Local by default.** Scanning and reviewing create a local index and local copies of your agent sessions. Those copies can contain the original text.
- **AI features are optional.** If you enable them, ClawJournal removes home-folder paths and usernames locally first. The remaining session text is sent to the AI service you choose and may still contain identifying details.
- **Sharing has safety checks.** Redaction and secret scans run before a package can be submitted. One scanner may contact a credential provider to check whether a suspected secret is live. A missing or failed required scan blocks sharing.
- **Automatic uploads are off by default.** They require a separate setup and authorization after a successful manual share.
- **ClawJournal keeps itself current.** Installs from a git checkout quietly fast-forward to the latest published version (at most once an hour) and, when an update changes dependencies, the workbench, or the pinned scanners, rerun the installer in the background. Updates never touch your local changes, and updating never uploads anything. Set `CLAWJOURNAL_NO_AUTO_UPDATE=1` to opt out.

For the complete details, see [PRIVACY.md](PRIVACY.md).

## Prefer the terminal?

These are the main commands:

```bash
clawjournal config --source all           # explicitly include all supported agent sources
clawjournal serve                        # open the local workbench
clawjournal share --interactive --weekly --no-score # guided sharing without AI scoring
clawjournal status                       # check your setup
clawjournal selfupdate --check           # see whether a newer version is available
clawjournal selfupdate --reinstall       # update and rerun the installer in one step
clawjournal --help                       # see every command
```

If `clawjournal` is not found, use the full command printed by the installer.

The terminal guide keeps the essential selection, redaction, consent, and destination safeguards in a simpler interface.

<details>
<summary><b>Manual installation</b></summary>

You need Git, Python 3.10 or newer, and a current LTS version of Node.js.

**macOS, Linux, WSL, or Git Bash**

```bash
git clone https://github.com/rayward-external/clawjournal.git ~/clawjournal
cd ~/clawjournal
./scripts/install.sh --with-frontend --with-sharing
```

**Windows PowerShell**

```powershell
git clone https://github.com/rayward-external/clawjournal.git "$HOME\clawjournal"
Set-Location "$HOME\clawjournal"
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -WithFrontend -WithSharing
```

When installation finishes, use the exact `clawjournal serve` command it prints, then open [http://localhost:8384](http://localhost:8384).

Re-running the installer safely updates an existing install to the latest published version. It never overwrites your local changes; if your checkout can't be updated cleanly, it says why and installs the code you already have.

</details>

## A few useful prompts

**Add a desktop shortcut**

```text
Install or refresh the ClawJournal desktop shortcut, then open the local
workbench. Check first with `clawjournal selfupdate --check` and run
`clawjournal selfupdate --reinstall` if it is behind, without discarding
local changes. Do not upload anything.
```

**Find missing WorkBuddy sessions**

```text
ClawJournal is not finding my WorkBuddy sessions. First check I am on the
latest version with `clawjournal selfupdate --check` and run
`clawjournal selfupdate --reinstall` if I am behind, since source discovery
improves between versions. Then locate the
local WorkBuddy export or trace folder, add it to ClawJournal's supported
import location, rescan, and show me what was found. Keep everything local.
```

**OpenRefinery participants**

If you are enrolled in OpenRefinery Agent Failure Sharing, ask your coding assistant:

```text
Set up the ClawJournal OpenRefinery reminder for my coding agents. Preview
what it will do first. Keep the normal review, confirmation, and sharing
safeguards in place; the reminder must never upload by itself.
```

See [OPENREFINERY_AGENT_FAILURE_SHARING_HOOKS.md](OPENREFINERY_AGENT_FAILURE_SHARING_HOOKS.md) for enrollment details.

## Project docs

- [PRIVACY.md](PRIVACY.md) — what stays local and what can leave your computer
- [ARCHITECTURE.md](ARCHITECTURE.md) — how ClawJournal works
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to contribute
- [SECURITY.md](SECURITY.md) — how to report a security issue

## Acknowledgments

ClawJournal builds on early work from [dataclaw](https://github.com/peteromallet/dataclaw) by [@peteromallet](https://github.com/peteromallet).

## License

Apache-2.0
