# Self-Improving Skills — Implementation Plan (v8, lean)

> Status: **plan for iteration** (no code yet). Date: 2026-06-30.
> **Two modes:** **A — light/local (the focus)** and **B — heavy/upload (next)**.
> Companion rationale: `brief.md`. Figures: `figures/fig1–7.png`. This version
> **merges the Claude + Codex reviews**, adds the Mode-A build-readiness fixes,
> trims nice-to-haves to §13, and supersedes the standalone review file.
> When this and the brief disagree, fix the brief to match this.

---

## 0. How to iterate (humans + AI agents)

- Don't re-litigate §2 "Locked decisions" or §4 "Invariants" without an explicit
  proposal (what + why + what it breaks). Reference decisions by ID, findings by §.
- v1 = **Mode A only**. Mode B (§11) is designed but built after A.
- §13 lists deferred nice-to-haves; pull from there only after A ships.

---

## 1. Goal & the two modes

Turn a participant's own coding-agent sessions into a **tiny, high-signal skill
their agent can use** — and keep it fresh as they keep working.

- **Mode A — Light (local-first; SHIP THIS FIRST).** Everything runs on the user's
  own computer: they install clawjournal, it indexes their sessions, scores the
  needed corpus window, and distills **up to 5 rules** (good *and* bad cases
  combined) into one
  `clawjournal-lessons` skill. Claude Code can invoke it via an Agent Skill trigger;
  Codex reads it from its instructions surface. **No ClawJournal/service upload, no
  user data stored on our side, and no service-side IP exposure**; provider egress
  still occurs through the user's own agent CLI. The user re-runs weekly to keep the
  skills current.
- **Mode B — Heavy (upload; build AFTER A).** For deeper, more-intelligent skills
  that need analysis a single machine can't do (cross-session embeddings, cross-user
  patterns, expensive validation), the user opts to upload **gated + redacted** data
  to a private service. The IP boundary and value exchange live here (§11).

**The self-improvement loop is the cadence:** keep using clawjournal → the skill
keeps improving. **First run indexes all history (slow, one-time); after that the
default scoring/selection window is the last 7 days.**

---

## 2. Locked decisions (lean)

| # | Decision | Choice |
|---|---|---|
| D1 | Two modes | **A = local-only (focus)**, B = upload (next, §11). |
| D2 | Output size | **One `clawjournal-lessons` skill with up to 5 rules total** — good "do X" + bad "avoid Y" combined, **hard cap 5**. Too many are hard to review/use/manage. |
| D3 | Targets | **Claude Code + Codex** — Claude Agent Skill `.claude/skills/…` (on-demand via trigger/description) and Codex `AGENTS.md` instructions (always-read). Do not claim Claude sees the body before every task until a `CLAUDE.md` path ships. |
| D4 | Cadence | **First run = full history indexing; then weekly, last-7-days scoring/selection default.** |
| D5 | Source | The user's own **scored** sessions: recurring **failures** → "avoid this" skills; recurring **successes / recovered failures** → "do this" skills. **Recurrence × impact, not raw frequency** (nontrivial only). |
| D6 | Inference | The user's **own agent CLI** (Claude/Codex) via `run_default_agent_task` — `ANTHROPIC_API_KEY` stripped → subscription, **no API key, ~free**. One distill call per run, using stronger distill-only defaults (Claude Opus + `xhigh`, Codex `gpt-5.5` + `xhigh`) while batch scoring keeps its fast defaults. |
| D7 | Privacy (Mode A) | Local-to-ClawJournal: **anonymize + net-new secrets-scrub before the distill call**; deterministic PII/secrets/TruffleHog gate before writing a skill. Default Mode A has one AI egress: the distill call through the user's own agent CLI. Any AI-PII pass is deferred or must be an explicit second-egress opt-in. Egress honesty: skills reach the model provider when the agent loads them. |
| D8 | Review | The 5 are **previewed before install** (a poisoned/over-general rule must not auto-activate); deterministic **hard-deny** of external/exec tokens in rule text. |
| D9 | Validation (Mode A) | **Light/observational only** — track whether targeted failure modes recur less week-over-week. Directional, not a powered metric. Rigorous A/B is Mode B. |
| D10 | Leverage | Reference `mindsdb/anton` / `claude-reflect` (MIT); reuse clawjournal's scoring + redaction + installer. `claude-memory-compiler` (unlicensed) = ideas only. |
| D11 | Mode B (deferred) | Upload = **gated + redacted only** (source/project confirmed; never raw/embargoed). Private service owns deep analysis (embeddings, cross-user, community lessons, expensive validation, ranking). **Open-core IP boundary + withholding-free exchange.** Returned candidates re-enter local gates + preview. |

---

## 3. Scope

**In (Mode A v1):** preflight → scan/index (first run full; later incremental) →
score the needed corpus window → select top-5 rules (good + bad) → local distill
(1 call) → preview → install the `clawjournal-lessons` skill for **Claude Code +
Codex** → weekly refresh.

**Out (Mode B or deferred — §13):** upload + deep analysis; cross-user/community
lessons; the open-core IP boundary + exchange; embeddings / matched-pair precision;
the elaborate lesson lifecycle (mute/pin/version/kill-switch); the heavy validation
stats; the benchmark-task handoff; Cline/Cursor/Copilot/Gemini/Kimi/Aider surfaces.

---

## 4. Non-negotiable invariants (Mode A)

1. **Anonymize-before-any-AI** — every field handed to the distill call passes the
   `Anonymizer` (home-dir paths + username) first.
2. **Net-new secrets scrub before distill** — a deterministic secrets pass over the
   substrate + bounded blob extract, *before* the LLM. (Not "reuse" — it's new.)
3. **Render-time block gate** — deterministic PII + `secrets.scan_text` +
   **TruffleHog** over the rendered skill text; **block the write on any hit**;
   surface the trigger for repair/discard; never ship or silently drop raw matched
   secrets. AI-PII is not part of default Mode A unless the UI/CLI explicitly
   discloses the second provider egress.
4. **Eligibility gate** — source scope explicit, projects confirmed, only
   `SHAREABLE_HOLD_STATES` feed the corpus. (Local today, but keeping the gate makes
   Mode B a clean extension.)
5. **Human preview before activation** — no skill installs without the user seeing
   the 5; **hard-deny** concrete external/executable tokens (URLs, out-of-repo paths,
   literal command lines, secret-like tokens, tool/MCP ids — not ordinary in-repo refs).
6. **Egress honesty** — local-first means *we* never upload it; the skill text still
   reaches the model provider when the agent loads it. Say so (no "all on-device").
7. **Off-tree global install** — write skills to the global agent dir, never into a
   repo `cwd` (a `git push` would bypass the gates).

---

## 5. Architecture (Mode A)

```
preflight (source + project confirmation, backend, TruffleHog)
  -> scan/index (first run: all history; later: incremental)
  -> [existing clawjournal scoring: failure modes, resolution, recovery labels]
     -> ensure scored corpus window (first run may chunk; weekly default: 7d)
        -> SELECT top-5 candidates (good + bad), by recurrence x impact (SQL/Python)
           -> anonymize + secrets scrub            <- before any LLM
              -> DISTILL (1 call, user's own agent CLI) -> <=5 skill rules
                 -> render-time gate (deterministic PII/secrets/TruffleHog + hard-deny)
                    -> PREVIEW the 5 -> user confirms
                       -> install clawjournal-lessons skill (Claude Code + Codex)
```

Only the distill calls an LLM by default — on the user's own subscription.
Everything else is SQL/Python. See **Fig 1** (pipeline) and **Fig 6** (good/bad
selection).

---

## 6. Picking the top 5 (good + bad)

Two candidate pools from scored sessions, then one ranked cut to 5:

- **Bad cases → "avoid this":** recurring `ai_failure_modes` (the fixed 12-value
  enum) with real impact (high failure-value / `resolution in {failed, abandoned}`),
  nontrivial.
- **Good cases → "do this":** recurring **strong successes** *and* **recovered
  failures** — the latter are the single best "do this instead" source (one trace
  holds both the error and the fix). **Causality guard:** teach the *fix / the
  delta*, not a coincidental habit (a success isn't caused by the named behavior).

**Rank** all candidates by **recurrence × impact × recency**; take the **top 5
overall** (the good/bad mix follows the data). **Hard cap = 5.** Never promote by
raw frequency — trivial repeated wins are noise.

Minimum candidate/rule fields:
`kind` (`do`/`avoid`), `target_failure_modes`, `trigger`, `guidance`, `why`,
`evidence_session_ids`, `support_count`, `impact`, `recency`, `source_agents`,
`confidence`, and a stable `fingerprint` used for merge/rejection tracking.

*(Matched failure/success pairs sharpen "do this instead" but are a bonus, not
required. If used, match deterministically — same `project`/`source` + same
`task_type` (`COALESCE(ai_task_type, task_type)`) + shared `ai_failure_mode`
(`json_each`) + opposite `resolution`. No embeddings in Mode A.)*

---

## 7. Pipeline, stage by stage (Mode A)

0. **Preflight** — require explicit source scope + confirmed projects before any
   corpus selection; verify an agent backend is available; verify TruffleHog is
   present unless an explicit test/dev bypass is active. If source/project
   confirmation is missing, block with concrete next steps (`clawjournal config
   --source ...`, `clawjournal config --confirm-projects`) rather than producing an
   empty skill.
1. **Scan/index** — first run indexes all discoverable history; later runs use the
   existing incremental scan/cursors. Do **not** depend on `clawjournal scan
   --since`; the current scan command has no such option. The 7-day default applies
   to scoring/selection windows.
2. **Score** — ensure the candidate corpus is scored. Use the existing scoring
   path over the needed window, with chunking/progress for first-run history and a
   bounded weekly 7-day default after that. Do not imply scan itself scores.
3. **Select** — top-5 candidates (good + bad), §6. Pure SQL/Python, no LLM.
4. **Scrub** — `Anonymizer(extra_usernames=cfg['redact_usernames'])` + net-new
   secrets scrub. Before the LLM.
5. **Distill** — one `run_default_agent_task` call → `rules:[{kind: do|avoid,
   trigger, guidance, why, evidence_session_ids}]`, ≤5. Reuse `generate.py`'s robust
   JSON extraction.
6. **Gate + preview** — hard-deny + deterministic PII/secrets/TruffleHog; show the
   5; user confirms.
7. **Install** — write the `clawjournal-lessons` skill for Claude Code + Codex
   (§8); idempotent.

---

## 8. Delivery: Claude Code + Codex

One **`clawjournal-lessons`** skill, ≤5 rules, rendered to each agent's native
surface:

| Agent | Surface | v1 |
|---|---|---|
| **Claude Code** | Agent Skill: `~/.claude/skills/clawjournal-lessons/SKILL.md` (global, off-tree; body is on-demand) | ✅ |
| **Codex** | instructions surface (`~/.codex/AGENTS.md` managed region; the existing `CLAWJOURNAL_AGENTS.md` install target is only a reference) | ✅ |

- **Global, off-tree, idempotent writes.** For Codex, use a begin/end managed
  region in `~/.codex/AGENTS.md` and only rewrite inside the markers. For Claude,
  own the `clawjournal-lessons/SKILL.md` file under the global skills directory.
  Preview before overwrite; never clobber a hand-edit.
- *Loading note:* a Claude Agent Skill body loads on a trigger/description, so write
  the `description` to fire on coding tasks; the Codex `AGENTS.md` surface is always
  read. Instrument or at least log Claude skill load/invocation rate before claiming
  it is always present. (Always-on for Claude via `CLAUDE.md` is a later refinement
  — §13.)

---

## 9. Self-improvement loop (the cadence is the point)

Keep using clawjournal → **weekly re-distill over the last 7 days** → merge into the
existing ≤5: if a new candidate outranks the weakest current skill, it **replaces**
it (cap stays 5) → **re-preview the diff** before install. Track targeted
failure-mode **recurrence** week-over-week as a directional "is it helping?" signal
(not a powered metric — single-user windows are usually "insufficient data"; honest
about that). Rigorous validation is Mode B.

Full lifecycle states can wait, but Mode A needs minimal durable state now:
`fingerprint`, `approved_at`, `rejected_at`, `installed_at`, `last_seen_at`, and
`evidence_session_ids`. A rejected fingerprint must not be proposed again unless the
underlying evidence or guidance materially changes.

---

## 10. Privacy

- **Mode A:** no ClawJournal/service upload and no data stored on our side. The
  only default egress is to the user's **own agent CLI** (their subscription) during
  the distill call, on anonymized + secrets-scrubbed text; the installed skill
  reaches the model provider when the agent loads it. Any AI-PII pass would be a
  second provider egress and is deferred or explicit opt-in.
- **Mode B (deferred):** opt-in upload of **gated + redacted** sessions through the
  existing share gate; private service; **withholding-free exchange** (contribution
  buys compute/depth, never privacy). Details in §11.

---

## 11. Mode B — Heavy (upload; build after A)

Same trust spine, more compute. **The cloud is strictly more compute, never fewer
gates** (Fig 5/Fig 7).

- **Upload** = all **gated + redacted** sessions (caps lifted, source/project
  confirmed; never raw/embargoed), through the existing `clawjournal-share` broker.
- **Deeper analysis** (the IP / moat): embeddings + stronger matching, cross-user
  clustering (k-anonymity floor), community lessons, expensive judge-independent
  validation (benchmark A/B), ranking/promotion heuristics.
- **Return path:** candidate skills come back and **re-enter the same local gates +
  preview** before install — the service never writes agent context directly.
- **Open-core boundary:** the public `clawjournal` repo stays auditable + usable
  standalone (Mode A); the private service owns the heavy/IP analysis.
- **Exchange:** withholding is the free default; contribution buys only
  compute/depth; a paid/credit path keeps the data route optional.

---

## 12. Roadmap

| Phase | Deliverable | Done when |
|---|---|---|
| **P1 — Mode A (light/local)** | the §5–9 pipeline: preflight, first-run full indexing + weekly 7d scoring/selection, one skill with ≤5 rules (good+bad), local distill, preview, install for Claude Code + Codex, weekly refresh | a user installs clawjournal, runs it, reviews ≤5 rules, Claude can invoke the skill, Codex reads the managed instructions, and a weekly re-run refreshes the set |
| **P2 — Mode B (heavy/upload)** | §11: gated+redacted upload, deep analysis, candidates re-enter local gates, open-core boundary + exchange | upload only accepts gated+redacted; returned candidates clear local review; budget owner + per-user cost cap exist |

A credible first cut: the §7 pipeline producing a previewed top-5-rule skill for
Claude Code, then add Codex, then the weekly merge/state tracking.

---

## 13. Deferred (nice-to-haves — add back after Mode A ships)

Full lesson lifecycle states (mute/pin/version/changelog/kill-switch beyond the
minimal approval/rejection state) · always-on Claude `CLAUDE.md` injection vs
on-demand skill · matched-pair embeddings /
cross-model verification / multi-pass distill / self-consistency / adversarial
refute pass · the heavy validation stats (difference-in-differences, binomial CI,
iatrogenic quarantine) · the benchmark-task handoff (`benchmark/generate.py`
keeps owning failures→tasks; `skill/` may contribute matched-pair seeds later,
deduped by `session_id`) · extra agent surfaces (Cline/Cursor/Copilot/Gemini/Kimi/
Aider) · project-scoped (vs global) skills.

---

## 14. Test plan (essentials)

Mirror `tests/benchmark/` with a fake `BackendCaller` (no real LLM in CI; TruffleHog
bypassed by the autouse fixture). Cover:
- cadence: first run indexes all history; subsequent refresh defaults to a
  last-7-days scoring/selection window;
- preflight blocks missing source/project confirmation with actionable next steps;
- refresh orchestration separates indexing from scoring and scores the required
  corpus window with caps/progress;
- selection returns good + bad candidates and **caps at 5**; trivial high-frequency
  wins are filtered; emitted rules include the required candidate/rule fields;
- anonymizer **and the net-new secrets scrub** run before any backend call;
- default Mode A makes exactly one backend call (distill); AI-PII is absent unless
  explicitly opted in and disclosed;
- render-time gate blocks on a planted secret/PII; hard-deny rejects an external/exec
  token;
- install writes the `clawjournal-lessons` skill for **both** Claude Code and Codex,
  idempotently, off the git tree;
- weekly merge replaces the weakest rule when a stronger candidate appears (cap 5);
- rejected fingerprints are not re-proposed unchanged.

---

## 15. Open questions

1. **Codex skill format** — confirm Codex's exact instructions/skills path + format
   (today the installer writes `CLAWJOURNAL_AGENTS.md`; verify the real always-read
   target, e.g. `~/.codex/AGENTS.md`).
2. **Claude skill loading** — exact trigger wording and load-rate/invocation metric
   for the on-demand Agent Skill body.
3. **Weekly trigger** — manual re-run only in v1, or a nudge/cron?
4. **First-run cost** — full-history indexing/scoring can be large; cap or chunk it, and surface
   progress.

---

## 16. Prior art & ideas to borrow (added 2026-07; see `competitor-comparison.md`)

A 29-item survey of the self-improving-skills field (`self-improving-skills-survey.md`)
was profiled against Mode A on five axes (personal-history input · local-first redaction
gate · failure-driven objective signal · capped previewed skill · recurring
replace/suppress loop), then adversarially re-scored for similarity. Full ranked report +
tier clustering: `competitor-comparison.md`.

**Closest analogs (verified similarity — what each proves / what we take):**

| Project | Sim | Proves is possible / what to take |
|---|---|---|
| SpecStory Lore | 72 | Cross-agent history → installable skills behind a human "approve the evidence dossier" gate. Take: positive "what-worked" evidence channel; "show candidates for last N days" dry-run UX. Lacks our redaction gate + failure signal. |
| openclaw-self-evolving | 72 | Weekly-cron re-distill over own logs, failure-signal polarity, **rejected proposals suppressed**, reports an ~8% false-positive rate. Take: publish a concrete FP rate as observational quality. Uses raw frequency (we deliberately don't). |
| claude-reflect | 68 | Real CC logs + first-class correction channel. Take: hybrid regex-trigger + deferred LLM-validation w/ confidence (0.60–0.95); semantic clustering of repeating patterns. Output is unbounded memory (we cap). |
| Aristotle | 58 | Own logs → installable rules via host model; git-backed **rejected-rule lifecycle** (pending→staging→verified/rejected). Single-session/OpenCode-only, zero privacy. |
| dzianisv/agents-supervisor | 54 | Mines "agent stopped → user followed up" pairs; optional private-HF push ≈ our Mode B. Take: three-tier precedence (shipped / user-learned / project-specific). |

**None of the 29 combine all five axes.** Genuinely unique to us: the **env-tool-error-signature**
and **reject-button/permission-denial** channels (absent in essentially every competitor),
the deterministic **anonymize + secrets + TruffleHog egress gate** on personal data (unique
among the personal-history miners — the others sidestep privacy by never egressing), and the
combined weekly **replace-and-suppress** loop over lived history.

**Top gap = validation.** We are observational-only ("directional, not powered"); the entire
research tier (SkillOpt/SkillAdaptor/Trace2Skill/EvoSkill/SkillX/CODESKILL/CoEvoSkills) uses
powered held-out A/B. This is the field's clearest open lever for us. Prioritized borrow list,
mapped to where each would land:

> **Designed in `validation-design.md` (Posture 1-B).** Headline: none of the research tier's
> validation transfers — we have no benchmark, no ground truth, no way to re-run a task. The
> honest ceiling is **association, never causation**. Primary signals are the *deterministic,
> candidate-side* gates (held-out generalization + recovery-delta grounding); outcome
> attribution is demoted to a directional DiD, and only for the machine-checkable minority of
> rules. Phases 1–2 add **zero new AI egress**. See also `validation-design-critique.md`.

- **Validation-gated acceptance + rejected-edit buffer** (SkillOpt) → strengthens §9's
  replace-weakest: accept a candidate only if it beats a held-out signal, and remember rejected
  edits. Natural home for a lightweight local A/B harness. *(highest-value)*
- **Step-level failure attribution** (SkillAdaptor / Trace2Skill) → §6 selection: a Localizer
  finds the earliest failing step, a Linker attributes it to the candidate rule — sharper
  "avoid" candidates than session-level labels.
- **Recurrence-gated conflict-free merge** (Trace2Skill) → §9: keep edits seen ≥2×; trial-apply
  + conflict-detect before adopting.
- **Surrogate co-evolving verifier** (CoEvoSkills) → a learned proxy critic for the common case
  where no held-out replay / ground-truth test exists — a candidate remedy for the validation
  gap without benchmark tasks.
- **Retrieval-gated skill injection** (SkillAdaptor / SkillX) → attach a lesson only when
  embedding-relevant to the task, so the ≤5/≤10 budget isn't spent on irrelevant rules (Mode B).
- **Falsifiable-claim taxonomy** (align) → §6 signal: the 6-category correct/wrong/almost/
  needs-nuance/cant-verify/skipped decomposition structures fuzzy human corrections.
- **Positive "what-worked" reinforcement channel** (SpecStory / claude-reflect) → §6 good-cases
  pool: capture what to repeat, not only what to avoid.
- **Learnable skill-bank policy + multi-granularity tiering** (CODESKILL / SkillX) → frame
  add/evolve/prune as an explicit policy; tier rules by specificity.

These are design references, not locked decisions — §0 iteration rules still apply.

---

## Appendix — file/symbol map (verify before building)

- Scoring/judge: `clawjournal/scoring/scoring.py` (`_VALID_FAILURE_MODES` @1017,
  `resolution` values @555, `ai_failure_value_score` desc @484, `JUDGE_SCHEMA`),
  `backends.py` (`run_default_agent_task`, `ANTHROPIC_API_KEY` strip, fast-tier).
- Benchmark (reuse patterns): `clawjournal/benchmark/{select,generate,store}.py`
  (`select_week_failures`, the `BackendCaller` seam @67, robust JSON extraction).
- Scan/capture: `clawjournal/capture/` (incremental cursors) + the `scan` command.
- Redaction/egress: `redaction/anonymizer.py`, `redaction/secrets.py` (extend),
  `redaction/pii.py::review_session_pii`, `redaction/trufflehog.py`,
  `workbench/index.py` (`SHAREABLE_HOLD_STATES` @2485, `release_gate_blockers` @2488).
- Skills/install: `cli.py::update_skill` @610, `SKILL_TARGETS` @586 (current
  installer is cwd-scoped: claude → `.claude/skills/clawjournal/SKILL.md`;
  codex/openclaw → `CLAWJOURNAL_AGENTS.md`). Mode A needs new global/off-tree
  writers for `~/.claude/skills/clawjournal-lessons/SKILL.md` and
  `~/.codex/AGENTS.md`.
- Reference modules (study only, MIT unless noted): anton `consolidator`/`cortex`/
  `skill_format`; claude-reflect `reflect_utils`. (`claude-memory-compiler` = unlicensed.)
