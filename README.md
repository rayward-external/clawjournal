# ClawJournal

ClawJournal helps you review the work you have done with coding agents, remove sensitive information, and choose what—if anything—you want to share.

It works with Claude Code, Claude Desktop, Claude Science, Codex, Gemini CLI, OpenCode, OpenClaw, Kimi CLI, WorkBuddy, Cursor, Copilot, and Aider.

## Start with a prompt

Copy the prompt that matches what you want to do into your coding assistant.

### Set it up for me

```text
Set up or update ClawJournal from
https://github.com/rayward-external/clawjournal.

Install the browser workbench and its sharing safety tools using the
project's installer. Look across all supported coding agents unless I tell
you otherwise. Show me the projects you find before confirming them, so I
can exclude personal, confidential, third-party, or unrelated work.

Then scan my coding-agent sessions, open the local workbench, and tell me
what to do next.

Use ClawJournal only for local indexing and review. Do not run AI scoring or
AI-assisted review, and do not enable or trigger Automatic uploads. If any
of those features is already enabled, tell me before opening the workbench.
Do not upload anything.
```

This prompt allows installation and local review only. You can decide about sharing later.

### Open it for me

```text
Open my local ClawJournal workbench, make sure its session index is current,
and help me find the work I did recently. Do not run AI features or upload
anything. If either is already enabled, tell me before opening the workbench.
```

### Help me review my work

```text
Open ClawJournal and help me review my recent coding-agent sessions.

Keep the original session text in the local workbench, not in chat. Help me
identify useful sessions, exclude anything unrelated, and place anything
sensitive or uncertain on hold. Do not run AI features, prepare a share, or
upload anything.
```

### Help me share safely

```text
Open ClawJournal and help me prepare a small share from sessions I have
already reviewed.

Keep raw traces and secret values in the local workbench or terminal, not in
chat. Guide me through Queue, Redact, Review, Package, and Submit. Preserve
every hold, embargo, finding, redaction, consent, and secret-scan safeguard.
Do not bypass a failed or missing safety check.

Use the built-in redaction rules unless I separately ask for AI-assisted
review. This request authorizes one manual share only; it does not authorize
Automatic uploads.

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

For the complete details, see [PRIVACY.md](PRIVACY.md).

## Prefer the terminal?

These are the main commands:

```bash
clawjournal serve                        # open the local workbench
clawjournal share --interactive --weekly # guided sharing over SSH or without a browser
clawjournal status                       # check your setup
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

</details>

## A few useful prompts

**Add a desktop shortcut**

```text
Install or refresh the ClawJournal desktop shortcut, then open the local
workbench. Do not upload anything.
```

**Find missing WorkBuddy sessions**

```text
ClawJournal is not finding my WorkBuddy sessions. Locate the local WorkBuddy
export or trace folder, add it to ClawJournal's supported import location,
rescan, and show me what was found. Keep everything local.
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
