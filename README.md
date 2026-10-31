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

If it is already installed, run one command for the selected mode:
`clawjournal selfupdate --reinstall --with-frontend --with-sharing` in UI
mode, or `clawjournal selfupdate --reinstall --with-sharing` in CLI-only
mode. That command brings the selected components to the latest published
version. It reruns the installer even when the checkout is already current
so older unrecorded dependency or scanner drift is repaired too.

Either way I must end up on the latest published version; verify that and
tell me if I am not. Never use force options, and never delete or overwrite
anything of mine to make an update succeed. If the installer or updater
reports a problem, show me its message, explain it in plain words, and stop.
After this setup, ClawJournal keeps itself up to date automatically, so I
should not need to repeat any of this.

Look across all supported coding agents unless I tell you otherwise. ClawJournal
defaults to that all-source scope; show me the
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
- **Problem reports start as local drafts.** **Report a problem** never captures your screen automatically or puts report text in a URL. It builds an editable draft from your description and a small allowlist of path-free diagnostics. Copy, download, and the blank GitHub issue remain local/manual. When a purpose-separated private support receiver is configured, the dialog shows its current purpose and retention terms and offers an explicit **Send privately** action for the exact reviewed Markdown. If that receiver separately advertises optional PNG intake, **Capture masked app view** renders only the visible app viewport with default-deny masking, excludes the reporter, and requires review of the exact full-resolution PNG and SHA-256. The browser talks only to the loopback daemon; the daemon owns durable upload/recovery and whole-report deletion. Logs, traces, hidden diagnostics, and unreviewed screenshots are never attached.
- **Private report delivery is recoverable.** A queued report is kept in a user-private, SQLite-independent outbox so index failures and daemon restarts cannot lose its state. Ambiguous network results are reconciled by receipt before retrying the same bytes. After acceptance or rejection, the local Markdown is removed; only minimal receipt and management data remain so you can check status or request deletion from **Recent private reports**.
- **Index recovery is backup-first.** The workbench checks its local SQLite index at startup. If the index is damaged, database-backed work and uploads stay stopped while the UI offers one guided action that backs up the old index before rebuilding it from the original agent logs.
- **AI features are optional.** If you enable them, ClawJournal removes home-folder paths and usernames locally first. The remaining session text is sent to the AI service you choose and may still contain identifying details.
- **Sharing has safety checks.** Redaction and secret scans run before a package can be submitted. One scanner may contact a credential provider to check whether a suspected secret is live. A missing or failed required scan blocks sharing.
- **Automatic uploads remain off until a successful manual share.** When the hosted recurring terms are available, the final Submit screen selects the combined “submit and enable” action by default, automatically includes every observed recurring-capable source, shows the exact cadence/cap, and links to the full terms and source/project pairs. You can uncheck it before submitting. After the manual receipt is saved, recurring setup is queued locally and continues in the daemon without holding the receipt screen open; closing the browser does not cancel it, and restarting `clawjournal serve` resumes it. If automatic uploads are already configured, the same row stays checked and locked; review, pause, or turn them off from Settings. You also keep session-level control without shrinking the enrolled scope: a session you **hold**, **embargo**, or **block** in the workbench is excluded from automatic uploads and shows up in the exclusion counts on the automatic-uploads status.
- **ClawJournal keeps itself current.** Installs from a git checkout quietly fast-forward to the latest published version (at most once an hour) and, when an update changes dependencies, the workbench, or the pinned scanners, rerun the installer in the background. A running workbench switches to the new version at the next quiet moment (never during a request or right after you changed something) — just reload the page. Updates never touch your local changes, and updating never uploads anything. Set `CLAWJOURNAL_NO_AUTO_UPDATE=1` to opt out.

- **One long agent conversation still makes progress.** Claude Code and Codex logs are read incrementally and split only at complete assistant-turn checkpoints after bounded limits are reached. Closed checkpoints can become eligible while you keep using the same conversation; the current unfinished tail stays local.

A trace with an unclear redaction boundary stays local. Other selected traces
can still be packaged. Exports record skipped session IDs in the manifest;
terminal commands also report skipped traces. The upload gates still scan every
exported trace.

Saved Share previews use a bounded local cache. New previews do not evict
previously reviewed queues. If the 128 MiB cache is full, finish pending shares
or explicitly clear saved reviews. In Share, select **Clear saved reviews**, or
run `clawjournal review-cache --clear` to remove unused previews. Add `--all`
to also invalidate pending saved reviews.

For the complete details, see [PRIVACY.md](PRIVACY.md).

## Prefer the terminal?

These are the main commands:

```bash
clawjournal open                         # open the local workbench, then return
clawjournal skill --preview              # preview lessons + one evidence-backed weekly focus
clawjournal skill --install-nudge        # opt in to a stale-lessons SessionStart reminder
clawjournal skill --uninstall-nudge      # disable that reminder and its context output
clawjournal share --interactive --weekly --no-score # guided sharing without AI scoring
clawjournal status                       # check your setup
clawjournal --version                    # show the package version and checkout revision
clawjournal doctor index --json          # collect bounded, path-free index diagnostics
clawjournal storage allow-network        # keep state on cluster/network storage for this machine only
clawjournal selfupdate --check           # see whether a newer version is available
clawjournal selfupdate --reinstall       # update and rerun the installer in one step
clawjournal selfupdate --reinstall --with-frontend --with-sharing # also add UI + managed scanners
clawjournal --help                       # see every command
```

If `clawjournal` is not found, use the full command printed by the installer.

`clawjournal open` starts or reuses the local workbench, waits briefly for it to
be ready, opens your browser, and then returns. `clawjournal serve` is the
foreground, long-running daemon command for manual process management and
debugging; agents and scripts should not wait for it to exit. On a remote or
headless machine, use `clawjournal serve --remote` in a dedicated terminal or
background process and follow the SSH tunnel instructions it prints.

On a cluster or another machine whose home directory is on a shared/network
filesystem, set `CLAWJOURNAL_HOME` to a private, persistent local-filesystem
directory before first use. For an existing install, stop ClawJournal and copy
the whole state directory together (index, review state, tokens, and local trace
copies), keeping the original unchanged until recovery succeeds, before setting
the variable; setting it alone selects a separate state
root. For example, use `export CLAWJOURNAL_HOME=/local/path/clawjournal` on
macOS/Linux or `$env:CLAWJOURNAL_HOME='D:\local\clawjournal'` in PowerShell.
ClawJournal refuses to create, open, or rebuild its mutable SQLite index when it
can positively identify the state directory as a network filesystem. Unknown
filesystem types keep the existing behavior, so cluster users should still
confirm the target with their administrator. Do not use an ephemeral node-local
temporary directory for state that must survive a reboot or job migration.

Many HPC clusters have no private, persistent local storage at all: home is on
NFS, scratch is on Lustre or GPFS and purged on a schedule, and `/tmp` is wiped
on reboot. There, keep the state where it is (normally your home directory, not
purged scratch) and run `clawjournal storage allow-network` once. ClawJournal
then uses its state on network storage from that machine only; it uses SQLite's
rollback journal, which works on network storage as long as only one machine
uses it. Every other machine stays blocked so two login nodes can never write
the index at the same time. To switch machines, stop ClawJournal on the old one
and run `clawjournal storage allow-network --take-over` on the new one. The
command refuses filesystems that do not support the file locks SQLite needs.
Keep the index, allowance, and other state in the same directory; a symlink for
only `index.db` on network storage is refused. If checking the allowance stalls,
ClawJournal keeps the index blocked and reports that network storage could not
be checked. Restore access to the mount, then restart ClawJournal.

`clawjournal doctor index --json` is read-only and does not require the external
`sqlite3` command. Its output is deliberately bounded and omits state paths,
mount sources, hostnames, usernames, and session content, so it can be reviewed
before attaching it to a support report. On known network storage or while live
SQLite sidecars are present, it reports that condition without opening the
database. Do not attach `index.db` itself.

`clawjournal skill` uses one optional AI distill call to propose up to five
coding-agent lessons. Its preview names one human-facing weekly focus only when a
fresh avoid rule directly cites matching failure or clean-recovery evidence from at
least 3 sessions across 2 days and 2 projects; otherwise it explicitly abstains when
no rule also passes its focus safeguards. The spotlight framing and aggregate
evidence are not installed into agent instructions; the underlying lesson remains in
the proposed skill set. It does not claim to assess your personality or performance
at work.

The stale-lessons nudge is **off by default**. If you opt in with
`clawjournal skill --install-nudge`, a due reminder includes aggregate counts of
new sessions and sessions with failure evidence in agent context, so those counts
also reach the model provider when the agent starts. It does not include session
text, run distillation, or install lessons automatically. The count follows your
confirmed source/project exclusions and hold/embargo boundaries. Disable it at any
time with `clawjournal skill --uninstall-nudge`.

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

When installation finishes, run `clawjournal open`. It starts or reuses the local workbench, opens [http://localhost:8384](http://localhost:8384), and returns once the launch attempt finishes.

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
## Local redaction evaluation (opt-in)

### Save consent and corrections directly (recommended)

After initializing and capturing into your evaluation store, run:

```bash
clawjournal redaction-eval --store "$HOME/.clawjournal/redaction-eval" serve
```

Open the printed `review_url`. This uses the workbench's local HTTP and bearer
authentication framework, without starting conversation discovery or uploads.
The original/redacted permission, ongoing snippet permission, individual snippet
approval, and correction screens save directly to the selected local store.
Success appears only after the existing backend validators commit the change.
No permission-file download or manual import is needed. Each permission remains
separate; saving permission does not upload a conversation or certify labels.

The normal `clawjournal serve` workbench also exposes `/redaction-eval` for the
default evaluation store. For a custom store use the command above; `--port`
optionally chooses a loopback port. Stop the standalone service with Ctrl+C.
It accepts only local same-origin writes with the existing API token. Store
paths cannot be chosen through a browser request. Review responses are not cached.

Failed or stale saves leave corrections unsaved and keep the navigation warning.
If edits are made while a save is in progress, those newer edits still need a
second save. Reopening a page reloads its current revision from local storage.
Existing CLI import commands and standalone HTML pages remain available. The
standalone pages say “Download requested,” since they cannot confirm a saved
file; use the local service if the browser cannot save their generated files.

### Capture and evaluate

Use `clawjournal redaction-eval` to retain an original input and its local redacted
preview, record corrections, and repeat a PII benchmark. This is a separate local
evaluation workspace. It does not add originals to share bundles, change findings
decisions, or authorize an upload. The normal intake/export workflow does not
automatically collect examples.

```bash
# Use a dedicated private directory outside your source repository.
clawjournal redaction-eval --store "$HOME/.clawjournal/redaction-eval" init

# Capture one UTF-8 conversation/snippet that you own or have permission to use.
clawjournal redaction-eval --store "$HOME/.clawjournal/redaction-eval" capture \
  --text-file conversation.txt --group contributor-001 --split dev \
  --source-kind owned --provenance "My research conversation; approved for local evaluation"

# Substitute the capture_id printed above. Open the resulting local HTML file.
clawjournal redaction-eval --store "$HOME/.clawjournal/redaction-eval" review CAPTURE_ID

# Select original text, label redact/preserve/uncertain, then download corrections.
clawjournal redaction-eval --store "$HOME/.clawjournal/redaction-eval" import-corrections corrections.json

# Repeatable local tests; reports contain offsets/counts, not conversation text.
clawjournal redaction-eval --store "$HOME/.clawjournal/redaction-eval" run --engine rules
clawjournal redaction-eval --store "$HOME/.clawjournal/redaction-eval" run \
  --engine gliner --model-dir /absolute/path/to/downloaded/model --threshold 0.3
```

`--session-json` also accepts **one normalized ClawJournal session object** with
`messages`, including nested content/tool fields. A provider's full ZIP or JSON
export must first be converted into this shape. Capture runs the existing local
share-preview redactor using config strings, usernames and allowlists, with empty
saved finding decisions. It does not import indexed policy rules or reproduce an
already approved share. It requires Betterleaks; the final TruffleHog package gate
is outside this experiment. Input may already have been sanitized by an earlier
intake step: capture cannot recover removed originals.

In the review page, corrections apply to selected spans in the **original**, using
Unicode code-point offsets (end exclusive). Select previously missed PII as well
as unnecessary redactions. Corrections can also be entered with `annotate
CAPTURE_ID --unit /messages/0/content --start 10 --end 26 --decision redact
--entity-type email`. Labels default to `assistant_draft`. Only a person who has
checked the label should mark the page's human-review checkbox or use
`--human-reviewed`. Default tests exclude drafts and uncertain labels;
`--include-drafts` explicitly enables exploratory results. Reopen a fresh review
page after importing: imports reject stale revisions, overlapping spans and
incorrect capture hashes. Label corrections never automatically unredact exports.

The versioned policy `actual-identifiers-v1` redacts actual identifiers, including
public email addresses, while preserving measurements and ordinary numbers.
Reviewers should use `uncertain` for unresolved cases. Benchmark reports include
TP/FP/FN/TN, accuracy, precision, recall/TPR, FNR, specificity/TNR, FPR and F1.
Detection means **any overlap** with the annotated target; the separate sensitive
span coverage section checks **every character**. A partial hit counts as detected
but is not fully protected. Undefined rates are `null`, not zero. Results measure
selected targets with 120 characters of surrounding context on each side, not
whole-conversation recall. Targets over 256 characters or GLiNER contexts over 256
model tokens fail explicitly rather than silently truncate.

Keep related conversations/participants in the same pseudonymous `--group`.
Groups are locked to `train`, `dev` or `test`; exact-source duplicates cannot move
to another group. Near duplicates and repeated identities still need manual
grouping. Tune settings on `dev`, train on `train`, and leave `test` untouched
until a final evaluation. Reports fingerprint the dataset, policy, evaluator,
model files, library versions and threshold. The rules engine is the PII regex
baseline, not the entire production redaction pipeline. GLiNER runs on CPU using
an already downloaded local model in an environment with `gliner`, `torch` and
`onnxruntime`; these are optional and not added to the base install. The evaluator
uses offline model loading, disables hub telemetry and refuses Python socket
connections. It does not download weights or call a hosted inference API.

These are **selected-span labels**, not complete NER training annotations. Before
fine-tuning, fully annotate chosen training windows, resolve uncertain labels and
freeze an independent human-reviewed test set. Otherwise an unlabelled identifier
could incorrectly be taught as safe text. Model output and existing redactions
are suggestions to check, not an answer key.

Originals and review pages are plaintext, protected by local directory/file
permissions (0700/0600 on POSIX), not application-level encryption. Keep the store
off shared/synced folders and out of Git. `init` defaults to a 30-day retention
period and 100 MiB of original/redacted payloads; configure these at creation with
`--retention-days` and `--max-mib`. Expired captures, labels and managed review
pages are deleted when an evaluation command opens the store or on `prune`;
there is no background deletion timer. `disable` stops new captures without
deleting existing examples; `delete CAPTURE_ID` removes a capture and its labels
and managed page. Re-running `init` does not re-enable a disabled store. Review
downloads, copied pages, backups and reports are not automatically purged. The
payload quota excludes labels, reports and SQLite overhead; it is not a disk cap.

### Optional snippet contributions

`redaction-eval contribute` lets a volunteer choose a small example for the
ClawJournal project's redaction training/evaluation. It is a **local bundle
workflow**, not an admin-site integration or automatic uploader. Normal shares,
hold states, project confirmation and recurring-upload permissions are unchanged.
Opening a contribution page grants no consent. By default, each snippet needs
its own explicit approval covering the exact displayed version. Volunteers can
instead opt in to ongoing permission for future local captures, described below.

1. Capture a conversation you may use, review its labels, and import the reviewed
   corrections as described above. Choose a small window (at most 800 Unicode
   characters) with 1–40 resolved, human-reviewed labels. Label every occurrence
   of identifiers and review the surrounding text. Unlabelled rule matches and
   possible secrets block preparation; a scanner cannot prove the absence of PII.
2. Prepare that window. All selected names, emails, usernames, phone numbers and
   addresses labelled `redact` are replaced with invented stand-ins. Supported
   aliases are `name`, `phone` and `home_username`. Other sensitive categories
   currently block preparation: trim the window instead of retaining the real
   value. Research values labelled `preserve` are kept. Repeated occurrences of
   the same identifier receive the same substitute within a proposal.
3. Open the generated contribution HTML page. Inspect the original and the exact
   proposed snippet. Four unchecked boxes ask about permission, context review,
   label/replacement review and consent to training/evaluation. The approval
   download stays disabled until all four are selected. **No, don't contribute**
   creates no approval. The page cannot modify an approval already imported into
   the CLI; it shows a withdrawal command for that case.
4. Import the approval and export the local bundle. Delivering that bundle to a
   maintainer is a separate, deliberate action; this feature sends no bundle to
   a server. Prepare a fresh proposal after any correction—old approvals cannot
   authorize revised text, labels or invented replacements.

```bash
# End is exclusive; use original-text Unicode code-point offsets.
clawjournal redaction-eval --store YOUR_STORE contribute prepare CAPTURE_ID \
  --unit /messages/0/content --start 0 --end 120

# Open the printed HTML path, inspect the snippet and download approval.
clawjournal redaction-eval --store YOUR_STORE contribute approve approval-PROPOSAL_ID.json
clawjournal redaction-eval --store YOUR_STORE contribute export PROPOSAL_ID

# Reopen a review or withdraw a pending/approved proposal.
clawjournal redaction-eval --store YOUR_STORE contribute review PROPOSAL_ID
clawjournal redaction-eval --store YOUR_STORE contribute withdraw PROPOSAL_ID
```

Export requires the installed Betterleaks and TruffleHog scanners, refuses bypass
flags and fails closed on scanner errors. Betterleaks first checks the proposed
bundle locally. The existing final share gate then scans the exact candidate;
its TruffleHog verified-credential check retains its existing network behavior.
Only the proposed edited candidate is scanned at that stage. A finding, rewrite
or changed authorization prevents bundle creation; labels are never silently
shifted after approval. This does not authorize an upload or release a held
session through the ordinary share flow.

Portable bundles contain the edited snippet, its label offsets, a random
contributor pseudonym, the split and a matching approval record. They omit
original identifiers and their replacement map, original session/capture IDs,
source hashes, file paths, free-text notes and provenance fields. Remaining
unlabelled **context is included**, so the volunteer must inspect all of it.
Invented emails use `research.example.org`, phone numbers use the fictional
`202-555-01xx` range, and names/addresses are generated from limited vocabularies.
These constraints help avoid exporting real identifiers but introduce a dataset
bias; generated names are not guaranteed unique to fiction. Incoming labels must
use these supported stand-in formats. This does not prove that a manually
constructed bundle has no sensitive content.

The installation stores one random contributor UUID and one split in
`$CLAWJOURNAL_HOME/redaction-contributor.db` (normally `~/.clawjournal/`). The first
proposal adopts its source capture's split; later proposals across evaluation
stores cannot move that contributor to another split. Plan the split before
collecting data. On another device, pass the same assigned UUID with
`contribute prepare --contributor-id UUID` on first use. Never use an email/name
as that ID, and do not create another identity to move known examples into a
test set. A device cannot independently establish that two identities belong to
one person; the maintainer must track that association.

Maintainers combine approved bundles using a **stable assigned contributor UUID**
for each person, including across devices:

```bash
clawjournal redaction-eval contribute collect PROPOSAL_ID.bundle.json \
  --collection /private/local/collection --contributor-id ASSIGNED_UUID
```

The collection rescans incoming bundles and validates the strict schema, approval
hash and label boundaries. It stores them in `collection.db`. Repeat imports are
idempotent; conflicting IDs, contributor split changes, inconsistent contributor
aliases and identical/equivalent snippets crossing splits are rejected. Equivalent
snippets are compared after replacing labelled invented identifiers with their
types, so changing a fake name does not bypass this check. Paraphrases and other
near duplicates still need curator review. Approval records are attestations,
not cryptographic proof of human consent; an eventual hosted collector must bind
them to authenticated contributor accounts. These snippets still carry
`selected-spans-not-exhaustive-NER`: inspect and fully annotate training windows
before treating unlabelled tokens as safe.

Withdrawal, capture deletion/expiry and changed source labels remove managed
contribution review pages/bundles; they cannot recall downloads, backups or files
already delivered elsewhere. Contribution records are deleted with their source
capture. The pseudonym/split profile and maintainer collection persist separately
to prevent accidental reassignment; they are not covered by capture retention.

#### Ongoing permission for future examples

Volunteers can choose **Review each contribution** (the default) or **Allow
ongoing contributions**. Open the local preference page, read the scope and
select its three initially unchecked acknowledgements. Download and import the
permission file to enable the setting:

```bash
clawjournal redaction-eval --store YOUR_STORE contribute settings
clawjournal redaction-eval --store YOUR_STORE contribute consent import ongoing-contribution-permission.json
clawjournal redaction-eval --store YOUR_STORE contribute consent status

# After future conversations are captured and their draft/reviewed labels saved:
clawjournal redaction-eval --store YOUR_STORE contribute process --limit 5

# Stop automatic preparation/export and remove managed automatic bundles:
clawjournal redaction-eval --store YOUR_STORE contribute consent disable
```

Permission is specific to this evaluation store, its stated training/evaluation
purpose and the current terms version. It covers **new captures after opt-in**,
not existing captures or unrelated projects/stores. Existing research membership,
software updates and automatic-upload settings never enable it implicitly. A
permission file from another store or an earlier revoked setting is rejected.
Re-enabling does not revive old automatic proposals.

`process` is a bounded local batch command suitable for calling from an existing
automation runner; no scheduler, daemon hook or hosted upload is installed. It
uses labels already recorded in the evaluation store, selects small surrounding
windows, replaces supported identifiers, runs the mandatory scanners, and writes
at most five candidate results per invocation. Captures without labels produce
no examples. Repeated runs skip existing managed bundles and withdrawn proposals.
Uncertain labels, unsupported types, unlabelled detected PII and scanner failures
are held locally for review; later runs retry held windows only after fresh ones,
least recently tried first, so they cannot crowd out new captures. Other
automatically missed PII may remain in context; consent explicitly acknowledges
that limitation. Correct held examples through the existing label-review
workflow and rerun the command.

Automatic bundles use schema v2. They record ongoing authorization separately
from review: `reviewed_context` and `reviewed_labels` are **false**, context is
`unreviewed`, and each label keeps its actual `assistant_draft` or
`human_reviewed` status. They are candidates for curation, not a trusted benchmark
or a fully annotated training set. The collector accepts both manual v1 and
automatic v2 bundles, preserving these distinctions and contributor split checks.
The existing benchmark continues excluding draft labels unless explicitly asked
to include them. To approve a candidate manually, review/correct the source labels
and prepare a fresh manual proposal; consent cannot promote draft labels.

Permission is checked again after scanning before writing an automatic bundle.
Disable prevents further automatic exports and removes managed automatic bundles;
it leaves separately approved manual contributions intact. Previously copied,
delivered, or collected files cannot be recalled locally. Delivering any bundle
to maintainers remains a separate action, and normal session-sharing gates remain
unchanged.

#### Separately sharing original and redacted conversations

Snippet consent never includes originals. The optional `redaction-eval pairs`
workflow requires a **separate unchecked permission** for full normalized
conversations, including original personal information. The permission is bound
to one HTTPS destination and future captures in one local evaluation store.

**Original conversations are used only to evaluate and improve the redaction
pipeline, including redaction-specific model training. They are not for general
research, unrelated benchmarks, or general-purpose model training.** This exact
purpose is recorded with the contribution and shown on the hosted admin table.

```bash
clawjournal redaction-eval --store YOUR_STORE pairs settings --destination https://data.rayward.ai
# Open the printed page, select the separate original/redacted permissions,
# download the permission file, then import it. Opening the page changes nothing.
clawjournal redaction-eval --store YOUR_STORE pairs consent paired-conversation-permission.json
clawjournal redaction-eval --store YOUR_STORE pairs status

# Capture a NEW conversation after opt-in using the existing capture command.
# Existing project confirmation, exclusions and indexed holds still apply.
clawjournal config --confirm-projects
clawjournal redaction-eval --store YOUR_STORE pairs export CAPTURE_ID
# Save a verified upload token from the destination's existing email-verification
# flow in a private text file (chmod 600). Tokens are single-use for new uploads.
clawjournal redaction-eval --store YOUR_STORE pairs upload CAPTURE_ID --upload-token-file TOKEN_FILE

clawjournal redaction-eval --store YOUR_STORE pairs disable
# A fresh verified token for the same contributor can withdraw a hosted pair,
# including after local permission has been disabled.
clawjournal redaction-eval --store YOUR_STORE pairs withdraw BUNDLE_UUID --upload-token-file FRESH_TOKEN_FILE
```

Paired files contain both complete normalized conversations, hashes joining the
pair, labels with their actual review status, and the purpose-specific consent.
The redacted conversation is the captured pipeline preview checked by the final
secret gate; label corrections are annotations, not edits to that stored preview.
Originals are scanned locally for possible secrets and blocked on findings or
scanner errors; originals never enter online credential verification. The
redacted side passes the existing mandatory share gate. No originals are added
to ordinary redacted session uploads or invented-identifier snippet bundles.
Original normalized objects may include personal metadata; consent covers the
whole object. Captured labels are not exhaustive or automatically verified.

Upload requires a compatible hosted service with paired collection explicitly
enabled; it fails if that endpoint is unavailable and does not fall back to a
normal share. Redirects are refused, and permission is rechecked before sending.
There is no background upload enrollment in this feature. Disable removes
managed local pair files and prevents further pair exports/uploads; already
delivered copies, including an upload already sending when Disable runs, require
hosted withdrawal. Expiring/deleting a local capture also removes its managed
pair file; the store keeps only each uploaded pair's ID and destination, so
hosted withdrawal still works afterwards. Local files are private but
unencrypted; use an encrypted disk and avoid cloud-synced folders.

Authorized administrators can use `/admin/redaction-pairs` to download the
original, redacted version, or a JSON object containing both. Its CSV table
contains pair/contributor IDs, dataset split, hashes, expiry and download links;
it does not embed raw text. Hosted payloads are encrypted separately, excluded
from the normal research ingestion feed, expire after 30 days and can be withdrawn.
Administrators must retain the same purpose/access restrictions on downloaded
copies and delete them by their expiry. Deployment and retention cleanup belong
to the companion hosted-service change; this client change does not deploy it.

### Optional attachments in manual Share

The Share review screen lists attachments next to their source message. Each
starts unchecked. Availability distinguishes original bytes present in the
import, extracted text only, and a filename/reference with no contents.
Select an item to scan its text, compare its local original with the redacted
preview, then include the conversation. Changing attachment choices removes the
conversation's approval until the new preview is reviewed. Failed scans do not
leave an approvable saved preview.

This version supports extracted text and embedded base64 UTF-8 text files up to
2 MiB of decoded text per attachment. Original binary bytes are not shared.
Embedded images and PDFs require the separate page review below; unsupported
formats remain unavailable for selection. A filename, local path, URL, or
provider ID is never fetched automatically. Separate files in a provider's ZIP
must first be imported by an adapter that supplies supported contents; this
feature does not claim those files are present from their names alone.

Embedded still PNG, JPEG, WebP, and PDF files have a local visual review in Share.
Open the attachment, choose **Start page review**, and **Scan this page** on each
page. Compare the original and redacted images, correct suggested redactions with
their checkboxes, or use **Hide an area** to drag over another area. The last
drawn rectangle can be undone. Recognized text changes with the pixel redactions.
Then run **Run full privacy check**, inspect each final redacted page, and tick
its review checkbox. **Approve and include attachment** selects the approved
pages for the conversation preview. Any correction requires a new privacy check
and page review. Conversation approval and submission remain separate steps.

Visual review requires Python 3.11+ and Pillow 13. Python 3.10 installations retain
the text workflow and receive an upgrade message for visual review. For PDFs,
install `.[visual-attachments]`; scanning requires a local Tesseract installation
with English language data. Processing is on demand and requires the local API
token, exact source revision, and unchanged redaction settings. Drafts live only
in process memory, limited to four files and 128 MiB; after 15 minutes of
inactivity they become invalid and are removed on the next cache access. Restarting
the daemon or choosing **Clear saved reviews** removes them. Closing the preview
releases its browser image URLs. Failed scans do not create a redacted preview.

Images are limited to 10 MiB and 20 megapixels, with their full resolution used
for recognition and redaction. Corrupt or animated images are rejected. PDFs are
rendered in a separate process into metadata-free page images, retaining visible
figures and layout. Limits are 20 MiB per PDF, 20 pages, 20 megapixels per page,
50 MiB total rendered output, and 45 seconds to render. An incomplete render
fails the entire file rather than silently omitting pages. Images retain full
resolution within the existing input limits for text recognition and redaction.
Local English OCR supplies word coordinates; deterministic rules and custom
strings suggest masks, and explicit rectangles can cover additional areas.
Masks replace pixels and remove overlapping recognized words. OCR failures and
low-confidence results remain explicit; absence of recognized text is not a
clean privacy verdict. A rendered or redacted page does not authorize sharing.
The full check applies export redaction rules and both mandatory scanners to
recognized text, maps further changes onto the page, and scans again. A missing
scanner, bypass, or unstable result prevents approval. The export contains fresh
RGB PNG pages with the approved redactions, not the original PDF or image file.
Page hashes and message records bind the final bytes; a later text rewrite
requires another page review so older visible text cannot silently remain.

New Claude Code and Claude Science imports retain message attachment blocks for
the checklist, including attachment-only messages. Codex user-message imports
retain file/image/audio blocks and local-file references from both rollout
formats. Duplicate message records merge attachment metadata; an explicitly
replaced prompt does not retain the old prompt's attachments. Embedded data URLs
count as file contents, while paths remain references. Existing indexed records
that were parsed without this information need re-importing from their original
logs; no missing bytes can be recovered from the old parsed record alone.

Attachment choices apply only to that saved preview. Unattended/manual CLI
exports omit attachments by default. Re-importing a prepared share requires
fresh choices. Re-imported page references alone do not count as original files:
recognized text is offered as an unchecked text choice, with missing visual
content and unknown message attribution retained. Export removes known attachment carriers and duplicate raw
message copies, while retaining ordinary conversation text—including text that
already quotes an attachment. Excluded attachment names and provider IDs stay
local. This does not change the separate, purpose-limited consent for original
conversation pairs used to improve redaction.

Selected text passes through the existing custom rules, username/path masking,
deterministic secret/PII scans, optional AI review, and final package scans.
Selected text is stored under each message's
`extra.attachment_review.items`, with schema `clawjournal.attachments.v1`.
Each item records `attachment_id`, zero-based `message_index`, `availability`,
and `status` (`included`, `excluded`, or `unavailable`). Included items also
carry the scanned `name` and `text`; omissions carry neither. A null message
index identifies a conversation-level attachment whose source message is unknown.
Selected text from a recognized PDF or image also carries
`visual_context_missing: true`: text alone does not preserve its figures or layout.
This limitation survives re-import and reaches STEMTraceBench as missing context.
Missing context is explicit; selection does not certify scientific sufficiency.

Messages with approved visual pages use `clawjournal.attachments.v2`. Each visual
item contains its scanned name and ordered `pages`, with the page index, PNG
path and hash, dimensions, recognized text, and redaction count. The manifest
lists the exact message records and files. Completed local copies preserve these
files and refuse missing or changed pages. Hosted validation and STEMTraceBench
transport support this format. A local walkthrough with invented text, an image,
and a PDF verified page review, real scanner checks, authenticated ZIP download,
receiver validation, and ingestion. The embedded browser's download-event hook
did not confirm a saved download; the authenticated endpoint returned the exact
finalized bytes. Production deployment has not been performed.

Run `pytest tests/workbench/test_attachments.py` for inventory, exclusion,
redaction, stale-choice, re-import, and saved-preview/export regressions.
`tests/workbench/test_daemon.py::test_attachment_choices_are_authenticated_revision_bound_and_exported`
covers the authenticated HTTP workflow. Frontend tests include the checklist,
original/redacted comparison, and removal of prior approval after changes.
`pytest tests/parsing/test_parser.py tests/parsing/test_segmenter.py` also checks
that message imports and retry handling preserve attachment context.
`pytest tests/workbench/test_attachment_reviews.py` checks local draft limits,
revision/settings binding, explicit approval, corrections, and cleanup.
`pytest tests/workbench/test_visual_share.py` checks complete privacy processing,
exact page export, completed local copies, and invalidation after privacy changes.
The attachment HTTP tests
and frontend visual-review tests cover authentication and interactive controls.
`pytest tests/workbench/test_attachment_visuals.py` checks local visual rendering,
OCR failures, pixel/text redaction and preservation of invented scientific data.
PDF tests require the optional dependency; the real OCR test also requires
Tesseract. Skipped integration tests do not verify visual processing.
