# ClawJournal — Competitor / Prior-Art Similarity Comparison

> Generated 2026-07-08 via a multi-agent survey (29 items from `self-improving-skills-survey.md`:
> 24 repos + 5 papers/clusters). Each item was web-researched, profiled against a fixed ClawJournal
> reference on 5 axes, scored 0–100 for similarity, then every high-scorer was adversarially re-checked
> to strip "merely generates skills" false positives. Primary ranking key = adversarially-verified similarity.
> Local planning material (untracked), companion to `self-improving-skills-survey.md` and `self-improving-skills/plan.md`.

## ClawJournal Competitive Comparison

ClawJournal's identity sits at the intersection of five hard axes: (1) INPUT = the user's **own real, messy personal** coding-agent logs; (2) local-first with a deterministic **redaction/TruffleHog egress gate**; (3) SIGNAL = **failure-driven objective feedback** (corrections + recurring tool-errors + rejections) plus 1–5 judge scoring, ranked by recurrence×impact×recency; (4) OUTPUT = **one capped (≤5), human-previewed, installable do/avoid lessons skill** for Claude Code + Codex; (5) a **recurring weekly re-distill** with weakest-rule replacement and rejected-fingerprint suppression. Very few projects hit more than two of these; none hit all five.

Ranking below uses `verified_similarity` (adversarially re-scored) as the primary key.

### Ranked comparison table

| Project | Sim | Input source | Local-first | Output | Verdict |
|---|---|---|---|---|---|
| SpecStory Lore | 72 | Personal agent history | Local, opt-in cloud | Installable skill folders | Genuinely similar |
| openclaw-self-evolving | 72 | Personal agent history | Local only | Rules → CLAUDE.md/AGENTS.md | Genuinely similar |
| claude-reflect | 68 | Personal agent history | Local only | Skills + CLAUDE.md/AGENTS.md memory | Genuinely similar |
| Aristotle | 58 | Personal agent history | Local only | Installable markdown rules | Genuinely similar |
| dzianisv/agents-supervisor | 54 | Personal agent history | Local, opt-in HF | patterns.json + runtime nudges | Partially similar |
| align (/align) | 47 | Current conversation | Local only | CLAUDE.md/AGENTS.md memory | Not similar |
| skill-builder | 44 | Mixed (shell/git/browser + CC) | Local, opt-in API | Slash-command skill folders | Not similar |
| ECC (everything-claude-code) | 42 | Mixed | Local, opt-in app | Skills + memory + hooks | Not similar |
| agentmemory | 42 | Personal agent history | Local, opt-in cloud | Memory/RAG store + viewer | Not similar |
| Trace2Skill (2603.25158) | 34 | Benchmark tasks | Cloud/API | SKILL.md folders | Not similar |
| SkillAdaptor | 30 | Benchmark tasks | Cloud/API | SKILL.md folders | Not similar |
| microsoft/SkillOpt (repo) | 30 | Benchmark tasks | Cloud/API | best_skill.md | Not similar |
| SkillOpt (2605.23904) | 30 | Benchmark tasks | Unknown | Markdown skill | Not similar |
| Academic AutoSkill cluster | 30 | Mixed/benchmark | Cloud/API | Skill KB | Not similar |
| CCHV (history viewer) | 24 | Personal agent history | Local only | Viewer UI | Not similar |
| EvoSkill | 24 | Benchmark tasks | Cloud/API | Skill folders | Not similar |
| SkillX | 24 | Benchmark tasks | Unknown | Skill KB | Not similar |
| CoEvoSkills | 24 | Benchmark tasks | Unknown | Multi-file skills | Not similar |
| CODESKILL | 22 | Benchmark tasks | Cloud/API | Skill bank | Not similar |
| Trace2Skill (Qwen-Applications repo) | 22 | Benchmark tasks | Cloud/API | Skill KB | Not similar |
| Claude Command & Control | 22 | Docs/repos/PDFs | Local only | Skill folders | Not similar |
| anthropics skill-creator | 20 | Current conversation | Local only | Skill folders | Not similar |
| claude-code-transcripts | 18 | Personal agent history | Local, opt-in gist | Viewer (HTML) | Not similar |
| Skill Seekers | 17 | Docs/repos/media | Local, opt-in cloud | Skill KB | Not similar |
| openai/skills catalog | 15 | Docs/authored | Unknown | Skill folders | Not similar |
| antfu/skills | 14 | Framework docs | Local only | Rules/skill folders | Not similar |
| netresearch/agent-rules-skill | 13 | Project manifests | Local only | AGENTS.md | Not similar |
| daymade/claude-code-skills | 12 | Authored/PR | Local only | Skill folders (marketplace) | Not similar |
| 2ykwang/agent-skills | 12 | Docs/code | Local only | Skill folders (marketplace) | Not similar |

---

### Tier clustering

#### Direct analogs (personal-history → installable rules, local-first)
- **SpecStory Lore (72)**, **openclaw-self-evolving (72)**, **claude-reflect (68)**, **Aristotle (58)**

These four are the only projects that share ClawJournal's *identity*: they mine the user's own real agent-session logs, run local-first, and emit small human-previewed installable rules to off-tree agent locations. They differ from ClawJournal (and each other) mainly on **which signal** they learn from, **whether the loop is recurring with replacement**, and **whether there is any redaction/egress gate**.

#### Adjacent (partial overlap)
- **dzianisv/agents-supervisor (54)** — same personal-history + correction-loop + local-first DNA, but output is a weights file + runtime nudges targeting a single failure mode (premature abandonment), not an installable lessons skill.
- **align (47)** — identical human-correction philosophy and recurring `/retro` clustering cadence, but its input is the *current conversation's* graded claims, not a batch index of on-disk history; output is CLAUDE.md accretion.
- **skill-builder (44)** — mines local history and emits multi-agent installable skills on a daily dedup'd cadence, but the signal is raw command/prompt *frequency* (which ClawJournal explicitly rejects) and it dilutes the agent-log focus with shell/git/browser history.
- **ECC (42)** — has a personal-log `/learn` subsystem and confidence-scored instincts with correction/rejection degradation, but is fundamentally a large hand-authored skills/agents marketplace with mixed (git + build-failure) input.
- **agentmemory (42)** — ingests real agent-session data locally with secret-stripping and decay weighting, but its goal is context *retention/RAG recall*, not extracting behavioral do/avoid lessons; no human-preview gate.

#### Research engines (benchmark/trajectory-driven — algorithm overlap only)
- **Trace2Skill (2603.25158 & Qwen repo)**, **SkillAdaptor (30)**, **SkillOpt (repo + 2605.23904)**, **Academic AutoSkill cluster (30)**, **EvoSkill (24)**, **SkillX (24)**, **CoEvoSkills (24)**, **CODESKILL (22)**

All consume **benchmark task trajectories** with ground-truth/verifiable pass-fail, require cloud/API inference, and have no privacy posture. They overlap with ClawJournal only on the *mechanics* of the distill/merge/replace loop — and several have machinery ClawJournal should study (step-level failure attribution, validation-gated acceptance, recurrence-gated conflict-free merge). But none touch personal history, human objective-feedback channels, or local-first redaction. Their validation is powered held-out A/B — a methodological strength ClawJournal deliberately forgoes (observational-only).

#### Out of scope (viewers, marketplaces, docs/git miners)
- **Viewers:** CCHV (24), claude-code-transcripts (18) — read the exact same on-disk logs but produce browseable UI, no learning signal, no loop.
- **Meta/authoring:** anthropics skill-creator (20), Claude Command & Control (22) — author skills from intent/docs, not from lived mistakes.
- **Marketplaces/catalogs:** daymade (12), 2ykwang (12), openai/skills (15) — distribution channels for hand-authored skills.
- **Docs/code/manifest miners:** Skill Seekers (17), antfu/skills (14), netresearch (13) — generate knowledge/context from reference material, never personal history.

---

### Top 5 closest matches — overlaps and gaps

**1. SpecStory Lore (72).** A true analog on ClawJournal's three heaviest axes: it mines the user's own cross-agent history (Claude Code, Codex, Cursor, Gemini, Droid) into ONE persistent local corpus, forges installable Agent Skills distributed via `npx skills add`, is local-first with cloud as a separate opt-in login, and gates every skill behind an explicit human "approve the evidence dossier" step. **Gaps:** its learning signal is the *inverse* of ClawJournal's — positive "what worked" evidence and human success labels, not failure-mode/correction/rejection channels ranked by recurrence×impact×recency. Forging is on-demand (`/lore`), with no automated weekly re-distill, no weakest-rule replacement, and no rejected-fingerprint suppression. It captures via its own wrapper into project-local `.specstory/history/` rather than reading native logs in place, has no deterministic secrets-scrub/TruffleHog gate or anonymize-before-AI step, no rule-count cap, and no observational validation.

**2. openclaw-self-evolving (72).** The closest match on ClawJournal's *loop shape and signal polarity*: a recurring **weekly cron** re-distill over the user's own Claude Code/OpenClaw JSONL, learning from **failure/frustration signals** (retry loops, repeating errors, rule violations, reaction-based rejections), emitting human-gated before/after diffs into CLAUDE.md/AGENTS.md, with **rejected proposals stored and never re-surfaced** — a direct parallel to ClawJournal's suppressed-fingerprint invariant. It even reports a measured ~8% false-positive rate (an observational quality metric worth emulating). **Gaps:** detection is pure pattern/keyword matching with no LLM judge and no 1–5 quality scoring; no mistake→correction→fix channel or permission-denial signal beyond frustration keywords; its ranking uses **raw frequency** (frequency×severity×impact) which ClawJournal deliberately avoids; second target is OpenClaw not Codex; output is diffs merged into an existing file rather than a standalone capped, recency-decayed lessons skill with stronger-replaces-weakest logic; no anonymization/secrets-scrub/TruffleHog gate (sidestepped by never egressing).

**3. claude-reflect (68).** Shares the same INPUT identity (real Claude Code logs) and a first-class **human-correction channel** ("no, use X not Y"), outputs human-previewed memory to off-tree global locations (~/.claude/CLAUDE.md + commands + AGENTS.md managed region for Codex/Cursor/Aider), keeps a human approval gate, and retroactively scans 14–90 days like ClawJournal's first-run index. Its hybrid **regex-trigger + deferred LLM-validation with confidence scoring (0.60–0.95)** and its `/reflect-skills` semantic clustering of repeating patterns are strong borrowable algorithms. **Gaps:** narrower signal (no judge scoring, no env tool-error recurrence, no explicit reject-button/permission-denial channel); no recurrence×impact×recency ranking and no rejected-fingerprint suppression; cadence is hook-queue + manual `/reflect` + post-commit reminders rather than a scheduled weekly re-distill with weakest-rule replacement; it also ingests **git commit history** (a source ClawJournal deliberately excludes); output is **unbounded growing memory** rather than a capped ≤5-rule skill; no redaction/egress gate.

**4. Aristotle (58).** Genuine ClawJournal DNA: reads the user's own session logs, runs local-first through the host agent's own model (no separate paid API), distills model **mistakes** into small installable markdown rules with YAML frontmatter (confidence/risk/intent), enforces a DRAFT→review→confirm human gate, and — notably — **tracks and suppresses rejected rules** in a git-backed pending→staging→verified/rejected lifecycle with conflict detection and audit scoring. **Gaps:** it is **reactive single-session reflection** over the last ~50 messages via `/aristotle`, the opposite of ClawJournal's whole-history batch index with cross-session recurrence ranking; targets **OpenCode only** (no Claude Code or Codex); **no privacy layer at all** (rule files even embed source_session paths); signal is error-reflection only (no corrections/tool-error-recurrence/rejection channels or judge scoring); no recency-decayed installed cap; several features documented as unimplemented (earlier maturity); no run-over-run validation.

**5. dzianisv/agents-supervisor (54).** Strong identity overlap: mines "agent stopped → user followed up" pairs from the user's real Claude Code transcripts + OpenCode DBs (exactly ClawJournal's correction channel), local-first with dataset git-ignored and only an optional `--push-hf` to a *private* HF dataset (mirrors Mode B), re-derives anti-pattern weights over a trailing ~14-day window, and offers a `--dry-run` preview. Its **three-tier precedence hierarchy** (shipped defaults / user-learned / project-specific) is a clean borrowable idea. **Gaps:** output is a `patterns.json` weights file + runtime continuation-nudges injected at Stop/idle, **not** a human-previewed installable do/avoid skill — a different deliverable and a different enforcement mechanism (runtime re-prompting vs preloaded rules). It targets a **single narrow failure mode** (premature abandonment), lacks ClawJournal's other objective channels and judge scoring, is manually triggered rather than auto-weekly, and has no deterministic deny-token/redaction/TruffleHog gate.

---

### What is genuinely unique to ClawJournal

No other project in the field combines all of the following; these are ClawJournal's real differentiators:

1. **Failure-driven objective-feedback triad ranked by recurrence×impact×recency.** ClawJournal fuses *three* distinct human/environment channels — mistake→correction→fix, recurring **environment tool-error signatures across ≥3 sessions**, and explicit **reject-button/permission-denial** rejections — plus a 1–5 judge. Lore learns *positive* evidence; openclaw uses frustration keywords + raw frequency; claude-reflect and supervisor have only corrections; Aristotle only error-reflection. **The recurring env-tool-error signature channel and the reject-button/permission-denial channel appear in essentially no competitor.** Explicit rejection of raw frequency in favor of recency×recurrence×impact is also distinctive.

2. **A mandatory deterministic privacy/egress gate on personal data.** Anonymize-before-any-AI-call + deterministic secrets scrub + **TruffleHog hard-deny** + hold-state upload gating. Among the personal-history miners, this is unique: Lore, claude-reflect, openclaw, Aristotle, and supervisor all lack a deterministic scrub/egress gate (most sidestep it by not egressing). agentmemory strips secrets but has no TruffleHog-style hard-deny.

3. **A single capped, recency-decayed, human-previewed lessons skill (≤5 active installed).** Nearly every analog produces *unbounded, growing* output (claude-reflect's CLAUDE.md accretion, ECC's hundreds of skills, agentmemory's memory graph) or diffs into an existing file (openclaw). The disciplined budget + recency decay is rare outside the benchmark optimizers.

4. **Automated recurring weekly re-distill with stronger-replaces-weakest AND rejected-fingerprint suppression, together.** openclaw has the weekly cron + suppression; Aristotle has the suppression; SkillOpt has the rejected-edit buffer + validation-gated replacement — but only ClawJournal combines the scheduled cadence, the capped weakest-replacement merge, AND the never-re-propose-unchanged suppression in one loop over lived personal history.

5. **Near-free distill routed through the user's own agent CLI subscription** (ANTHROPIC_API_KEY stripped, no separate API key). Aristotle and Skill Seekers share the "use the user's own CLI" trick, but combined with #1–#4 this is unique.

6. **Batch index of the *entire* cross-agent history then cross-session recurrence ranking** — versus the reactive single-session reflection of Aristotle or the current-conversation scope of align/skill-creator.

The honest caveat: on **validation**, ClawJournal is *weaker* than the research tier. It is explicitly observational/directional-not-powered, whereas SkillOpt, SkillAdaptor, Trace2Skill, EvoSkill, SkillX, CODESKILL, and CoEvoSkills all use powered held-out A/B replay. That is a deliberate scope choice, but it is a genuine gap, not a differentiator.

---

### Best ideas worth borrowing

- **Step-level failure attribution** (SkillAdaptor / Trace2Skill 2603.25158): a Localizer finds the earliest failing step t★ and a Linker scores which injected rule *caused* the failure — a principled way to attribute recurring failures to specific candidate rules.
- **Validation-gated acceptance + rejected-edit buffer** (SkillOpt / microsoft/SkillOpt): accept a candidate edit only if it strictly improves a held-out score, and remember rejected edits so they are not re-proposed — a stronger, more powered version of ClawJournal's observational replace-weakest, and a natural home for a lightweight A/B harness.
- **Recurrence-gated conflict-free hierarchical merge** (Trace2Skill 2603.25158): keep only edits appearing ≥2×, with file-existence/line-range conflict detection and trial-apply validation before adopting — directly applicable to ClawJournal's candidate-replaces-weakest step.
- **Hybrid regex-trigger + deferred LLM-validation with confidence scores, and semantic clustering of repeating patterns** (claude-reflect): fast real-time detection plus deferred semantic confirmation (0.60–0.95), then cluster similar recurring patterns across sessions before synthesizing a rule.
- **Positive "what worked" evidence + human-reply success labels** (SpecStory Lore; also claude-reflect's affirmation channel): add a reinforcement channel so lessons capture not just what to avoid but what to repeat.
- **Three-tier precedence hierarchy** (dzianisv/agents-supervisor): shipped-defaults / user-learned / project-specific layering for clean rule precedence; complements ClawJournal's cap.
- **Falsifiable-claim taxonomy** (align): the 6-category correct/wrong/almost/needs-nuance/cant-verify/skipped decomposition turns fuzzy human corrections into structured, gradable signal.
- **Learnable skill-bank management policy + multi-granularity tiering** (CODESKILL, SkillX): frame add/evolve/prune as an explicit policy, and tier rules by specificity (planning/functional/atomic) rather than a flat list.
- **Surrogate co-evolving verifier** (CoEvoSkills): a learned proxy critic that gives actionable feedback without ground-truth tests — a candidate remedy for ClawJournal's validation gap where no held-out replay exists.
- **Retrieval-gated skill injection** (SkillAdaptor / SkillX): attach a lesson only when embedding-relevant to the current task, so the active ≤5 budget isn't spent on irrelevant rules.
- **Measured false-positive rate as an observational quality metric** (openclaw-self-evolving): report a concrete FP rate for proposed rules to make "directional" validation less hand-wavy.
- **Recency-windowed candidate-review UX** (SpecStory Lore's "/lore last 30 days, just show candidates"; skill-builder's `--days`): dry-run "show me the candidates for the last N days" before install, reinforcing the human gate.
