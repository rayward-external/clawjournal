<!-- Provenance: authored 2026-07-09 via a multi-agent pass — 4 parallel investigations
     (current-code seams · SkillOpt validation-gate · SkillAdaptor/Trace2Skill step-attribution ·
     no-benchmark validation-signal analysis) -> draft -> adversarial critique -> revise.
     The critique that forced this doc's key retractions is preserved in validation-design-critique.md.
     Companion to plan.md §16 and competitor-comparison.md. Local planning material (untracked). -->

# ClawJournal — Validation & Attribution Layer (Posture 1-B)

**Status:** design / no code yet · **Owner:** skills · **Depends on:** shipped Mode A pipeline (`clawjournal/skill/`, `cli_skill.py`) · **Supersedes nothing** — additive.

All code anchors below were re-verified against the working tree (`clawjournal/skill/store.py`, `select.py`, `turns.py`, `distill.py`, `schema.py`, `cli_skill.py`). One correction to the grounding brief: `_SUPPORT_HALFLIFE_DAYS = 30.0` (cli_skill.py:49), not 49.

**What changed in this revision (read first).** An earlier draft promoted signal **B (trigger-relevance × outcome attribution)** to *primary validation* on the claim that relevance-conditioning "controls for corpus drift." That claim is false (it removes only the floating-denominator/compositional confound, not calendar drift, RTM, or the user's own improvement), and the machinery B needs is mostly unbuildable against the current store. This version **restores the source analysis's ranking** (the no-benchmark validation-signal analysis): the deterministic, candidate-side gates **A (held-out generalization)** and **D (recovery-delta grounding)** lead as primary; **B is demoted to a secondary, explicitly-directional reporting signal**, and only in a difference-in-differences form, exposure-gated, placebo-audited, multiplicity-corrected, and restricted to the machine-checkable minority of rules. The governing principle everywhere below: **where a local, no-rerun signal cannot support a claim, we weaken the claim rather than build machinery to rescue it.**

---

## 0. TL;DR + relationship to Posture 1-A

**The problem in one sentence:** today the only thing resembling "validation" is an unattributed run-over-run recurrence delta (`mode_rates()`/`objective_rates()` vs the single last snapshot, cli_skill.py:376-390) whose denominator is a *different session set every run* — it never records which rules were live, never conditions on whether a rule could even have fired, and therefore cannot distinguish "the rule helped" from "the corpus changed." That floating-denominator diagnosis is correct and is the one genuine improvement we carry forward — but fixing it buys *comparable populations*, not *causal attribution*.

**What we build (1-B):** validate the **input to distill** rigorously (where rigor is achievable), and report on the **output** honestly and directionally (where it is not). Concretely:

- **PRIMARY — candidate-side, deterministic, zero-egress gates on the pattern *before* it is distilled:**
  - **A — held-out, burst-robust generalization.** Require a candidate's support to survive **leave-one-time-bucket-out and leave-one-project-out** resampling (not leave-one-session-out, which is a no-op — see §4.1). Proves the pattern is real and not one afternoon's burst. Runs at candidate time, where **real session ids still exist** (aliasing happens later, distill.py:156), so it applies to *every* candidate including those that become distilled prose rules.
  - **D — recovery-delta grounding.** Where a failed→recovered pair exists, test whether the candidate's guidance matches the observed corrective delta. The strongest *local* efficacy evidence — "this action resolved this failure at least once in your own history" — bounded by survivorship (§4.2).
  - **E4 — contradiction gate** and **E5 — accept/reject as the one real human label**, both always-on.
- **CLAIM-NEUTRAL PLUMBING — the forward in-effect ledger.** Persist `skill_rule_effect(installed_at, retired_at)` intervals at `mark_installed`. This is the single genuinely-missing record (Q1). It makes **no claim by itself**; it merely lets later analysis ask "which rules were live at this session's `start_time`."
- **SECONDARY — B, as a difference-in-differences event study, directional only.** For the **machine-checkable minority** of rules (env-signature / rejection-derived / 1-A — *not* distilled prose), align each rule to its own `installed_at`, condition on `error_signature`/tool-family relevance, gate the treated cohort by **provable skill exposure** (E1), difference against **never-installed control failure modes** (E3) and against a **spike-matched placebo null** (E2), and correct for **multiple comparisons** across rules (M6). After all of that the claim ceiling is still *"directional, confounded association,"* never causation.
- **ACCEPTANCE/MERGE (SkillOpt transfer)** — bounded edits `L_t` + a **rejected-EDIT buffer ℬ** (soft, revivable) distinct from the **user-rejection buffer** (hard veto).
- **OPTIONAL — C surrogate re-judge**, an explicitly opt-in, disclosed **second egress**, ranker/tie-breaker only, never a gate, abstains when it has no held-out evidence.

**Locked decision (epistemics).** The honest ceiling of this entire layer is: **rules target real, out-of-sample-stable, historically-grounded failure patterns (A/D); and among exposure-verified, trigger-relevant later sessions the targeted failure is observed to recur less than a placebo-audited, drift-controlled baseline (B/E1/E2/E3) — directional, personal, and non-causal.** The human who reviews these sessions learns the same lesson the rule encodes, from the same sessions used to mint the rule; no local, no-rerun method removes that confound. Every surface that reports a number carries this framing.

**Relationship to Posture 1-A (deterministic objective→rule, a SEPARATE slice):** 1-A closes GAP (1) — it mints a rule directly from an objective signal the LLM distill ignored, without waiting for the distiller to cite it. 1-B (this doc) closes GAP (2) — validation/attribution. They compose: **1-B's candidate-side gates (A/D/E4) filter *all* candidates, including the ones 1-A produces.** A 1-A rule is not exempt from held-out generalization, contradiction, dedup, or the in-effect ledger. 1-A candidates enter the same candidate pool (`turns.add_env_candidates`/`add_rejection_candidate` already synthesize `env-signature-N`/`human-rejection`; 1-A generalizes that to "emit a rule even if distill drops it"). Where 1-A and 1-B touch the same seam (the candidate stage in `turns.py`/`select.py`), 1-A *produces* and 1-B *filters*; build order is 1-B Phase 1 gates first so 1-A never ships an ungated deterministic rule.

---

## 1. Problem: the validation gap, and why benchmark held-out A/B does not transfer

### 1.1 What "validation" means in the research we are borrowing from

Every self-evolving-skill system we studied validates by **re-running a frozen agent on the same inputs under the candidate skill and scoring with an automatic verifier**:

- **SkillOpt** (arXiv 2605.23904): three disjoint splits `D_tr : D_sel : D_test = 2:1:7`; a candidate skill is *applied, evaluated on `D_sel`, and accepted only on strict improvement* `score_cand > score_cur`. Reporting is on the disjoint `D_test`. The gate literally re-executes.
- **SkillAdaptor** (arXiv 2606.01311): Localizer→Linker attribute a failure to a skill, then the edit is adopted **only if `Δ = E_q[M(q;K+)] − E_q[M(q;K)] ≥ 0`** on held-out tasks.
- **Trace2Skill** (arXiv 2603.25158): the error analyst is a ReAct loop that *compares outputs to ground truth and validates candidate fixes* before emitting a patch.
- **CoEvoSkills:** a surrogate verifier stands in *only because* even it is checked against re-runnable rollouts.

### 1.2 Why none of that transfers to ClawJournal

ClawJournal is a **passive log analyzer**. It has:

- **No task suite** — nothing to run.
- **No ground truth** — the judge produces 1–5 scores and a 12-value `ai_failure_modes` enum, not pass/fail.
- **No re-execution** — we cannot replay a past session "under the new skill." We only ever observe *new* sessions on *different* tasks after a rule installs.

So the SkillOpt gate (`score_cand > score_cur` on identical `D_sel` tasks), the SkillAdaptor `Δ≥0` re-run, and Trace2Skill's compare-to-ground-truth all require a capability we structurally lack. Three specific breakages:

1. **No counterfactual ⇒ no pre-deployment gate.** "Did this edit help?" can only be answered by watching later, different sessions — an A/B-over-time *observational* estimate, confounded by task-mix drift, RTM, and the user's own improvement. Any efficacy-flavored gate must become a **trailing retirement mechanism**, not an acceptance filter.
2. **Strict inequality is unusable on our data.** Verifier scores are low-variance and re-runnable; our per-session judge scores + rare objective events are high-variance and confounded. "Ties rejected, strict improvement" would accept/reject on noise. We *must* reintroduce the noise margin SkillOpt omits (placebo null / minimum effect over minimum sessions).
3. **Deployment is irreversible per-user.** SkillOpt discards a losing candidate for free before shipping; we ship to the live `SKILL.md`/`AGENTS.md` and only learn later. Hence: install optimistically (human-gated), then *demote* rules that fail to earn their keep, and buffer the demoted rule so it isn't re-distilled.

### 1.3 The confound we can never remove (the locked ceiling)

The human who reviews these sessions **learns the same lesson the rule encodes, from the very same sessions used to mint the rule.** Any post-install decline is co-produced by the human's own learning. Removing this requires a holdout arm of re-runnable tasks — forbidden by the constraints. **This confound is treatment-specific and is invisible to every control we can build locally** (event-study control series, placebo shams, exposure gating all leave it intact — see §4.5). It is stated once here and re-asserted at every reporting surface (§2.4, §4.5, §6, §9).

---

## 2. The validation signal we CAN get locally

### 2.1 The three claims, kept strictly separate

No method may report a weaker claim as a stronger one:

1. **Generalization / non-noise** — the pattern is a real, stable feature of *this user's* work, not one-session or one-burst noise (**A**).
2. **Historical grounding of the encoded action** — the rule's guidance matches an action observed to resolve that failure at least once (**D**).
3. **Efficacy / causation** — installing the rule reduces the failure going forward (**approached only, and only directionally, by B/C — neither reaches causation**).

Claims 1 and 2 are where local rigor is actually achievable, and they never overreach. Claim 3 is where we are honest that we can only report a confounded, directional trend.

### 2.2 Options considered (from the no-benchmark brief), restored to the source ranking

We adopt the **source brief's labels verbatim** (a crosswalk to the earlier draft's relabeling is in §2.5) and its ranking, which put the deterministic candidate-side gates first and B in Phase 2:

| Signal | Honest claim | Egress | Buildability | Role in this design |
|---|---|---|---|---|
| **D** recovery-delta grounding | encoded action observably fixed it (≥1×) | none | high reuse (`grounding` rank term) | **PRIMARY** (candidate gate / booster) |
| **A** held-out generalization | pattern is real, survives resampling | none | highest | **PRIMARY** (candidate gate) |
| **E2** placebo / negative control | how much of any B number is drift+RTM | none | easy | Phase-2 audit; **B ships only if it passes** |
| **E5** accept/reject label | human ground-truth top-of-funnel | none (already stored) | half-built | Phase-1 calibration |
| **E4** contradiction gate | installed set stays coherent (safety) | none | small | Phase-1 hygiene |
| **E1** exposure gating | rule was *in context*, not just installed | none | moderate | Phase-2 enabler for B |
| **E3** staggered-adoption event study / DiD | drift-controlled *directional* trend | none | moderate | **the only form in which B is reportable** |
| **B** trigger-relevance × outcome | attributed *association*, directional | none\* | **partial — machine-checkable rules only** | **SECONDARY**, gated by E1/E2/E3/M6 |
| **C** surrogate re-judge | LLM plausibility (opinion) | **2nd egress, opt-in** | new stage | Phase-3 opt-in ranker only |

\* B is zero-egress **only for machine-checkable triggers** (env-signature, error-signature, rejection-derived — the deterministic minority). Distilled prose rules — the pipeline's *main output* — have no machine-checkable signature and are **excluded from B entirely** (`relevance: unknown`). See §4.5 and the scope statement in §2.4.

### 2.3 DECISION

> **Primary validation is on the candidate, pre-distill, and deterministic: A (held-out, burst-robust generalization) + D (recovery-delta grounding), with E4 contradiction and E5 accept/reject as always-on hygiene/calibration.** These validate the *input* to distill, make claims that never overreach, cost zero egress, and — because they run before aliasing — apply to every candidate including those that become prose rules.
>
> **The forward in-effect ledger (`skill_rule_effect`) ships alongside them as claim-neutral plumbing** — it records which rules were live when, and asserts nothing.
>
> **B is secondary and reportable only in its difference-in-differences (E3) form**, restricted to machine-checkable-trigger rules, exposure-gated (E1), placebo-audited (E2), multiplicity-corrected (M6), and silent below a cold-start floor. Its published claim is capped at *"directional, confounded association."* **The entire B reporting surface is gated behind a passing E2 placebo audit** (§7): if shams cannot be distinguished from real rules on this corpus, we do not print a B number at all.
>
> **C is an opt-in Phase-3 second egress**, ranker/tie-breaker only.

Rationale (this survives the corrected understanding of relevance-conditioning): A and D are simultaneously the highest-correctness, cheapest, and most buildable signals, and they attach to the *majority* of rules. B, even in its best (DiD) form, is confounded, directional, and covers only the deterministic minority — so it earns a supporting role, not the headline.

### 2.4 What each signal can and cannot claim (say this out loud, every time)

- **A:** *can* say "this failure pattern recurs across independent time-buckets and projects, so it isn't a burst artifact." *Cannot* say the rule phrased around it will fire, nor that installing it helps.
- **D:** *can* say "the rule's guidance matches an action observed to resolve this exact failure at least once in your history." *Cannot* generalize (survivorship: you only see failures that recovered — §4.2), nor prove stating it as a rule changes agent behavior. Grounding booster, **never** an efficacy gate.
- **B (+E1/E2/E3):** *can* say, **for machine-checkable rules only**, "among later sessions where the skill was provably loaded and the rule was `error_signature`-relevant, the targeted failure trended down after install, relative to a never-installed control series and above a spike-matched placebo null." *Cannot* say the rule *caused* it (treatment-specific RTM and the human-learning confound remain — §4.5), *cannot* separate two rules relevant to the same session (collinearity), and **does not touch distilled prose rules at all.**
  - **Scope truth, stated loudly (not a footnote):** B's primary reporting **does not cover the distilled prose rules that are the main output of the single distill call.** It covers env-signature / rejection-derived / 1-A rules only. For a distilled prose rule the attribution surface prints `relevance: unknown — not machine-checkable; unvalidated by this layer`.
- **E1:** *can* say "the skill was present in the later session's context" (transcript-verified). *Cannot* say the model attended to it.
- **E2:** *can* say "a real rule's Δ exceeds what drift + regression-to-the-mean produce for a sham of the same trigger installed at the same kind of spike." *Cannot* say it exceeds the **human-learning** confound (a sham is never shown to the user, so it has *zero* human-learning component — §4.5). This is the single most important honesty correction in the doc.
- **E4:** *can* say "the installed set has no rule contradicting a kept rule or a known-good recovery delta." Safety, not efficacy.
- **E5:** *can* say "the human keeps accepting this rule class and never retracts it" (acceptability). *Cannot* say measured efficacy.
- **C:** *can* say "an LLM thinks this rule would have helped, above sham, on evidence held out from its seeding sessions." *Cannot* be a standalone efficacy proof (sycophancy/hindsight). Ranker/tie-breaker only; abstains when no held-out evidence exists.

### 2.5 Label crosswalk (traceability — two source signals were previously dropped)

| Source brief (the no-benchmark validation-signal analysis) | Earlier draft's label | This doc |
|---|---|---|
| A held-out generalization | (folded into "E2 jackknife") | **A** — restored as primary; jackknife-over-sessions replaced with time/project-bucket resampling (§4.1) |
| B attribution | B (was PRIMARY) | **B** — demoted to secondary/directional (§4.5) |
| C surrogate re-judge | C | **C** (§4.6) |
| D recovery grounding | D | **D** — restored as primary (§4.2) |
| **E1 exposure gating** | *dropped; label reused for placebo* | **E1** — restored (§4.5); gates B's treated cohort |
| E2 placebo | "E1 placebo" | **E2** (§4.5) |
| **E3 event study / DiD** | *dropped; label reused for "currency"* | **E3** — restored (§4.5); the only form in which B is reportable |
| E4 contradiction gate | "E4 dedup" | **E4** (§4.4) |
| E5 accept/reject | "E5 revealed preference" | **E5** (§4.4) |

"Currency" (still-live prioritization) survives as a **prioritization heuristic only** (§4.1), not a validation signal and not an E-label — the earlier draft's promotion of it to "E3" silently deleted the source brief's event-study, the single biggest substantive regression this revision reverses.

---

## 3. Architecture — where the layer hooks in

```
                         ┌──────────────────────── generate_skill (cli_skill.py:276, pure/read-only) ─────────────────────────┐
                         │                                                                                                    │
  scan/score  ─────────▶ │  select_skill_candidates ──▶  env/rejection append ──▶  distill_skills ──▶  gate ──▶ merge_rules   │ ──▶ mark_installed
 (workbench)             │  (select.py:177)              (turns.py add_*)          (distill.py:250)    hard/PII  (cli:180)     │     (store.py:143)
                         │        │                            │                    1 LLM call         /Trufle   │            │        │
                         └────────┼────────────────────────────┼──────────────────────────────────────┼─────────┼────────────┘        │
                                  │                            │                                      │         │                     │
        ┌─────────────────────────┼───────────────────────────┼─────┐   ┌──────────┴───────────┐  ┌───┴─────────┼──────────┐  ┌────────┴──────────┐
        │  PRIMARY CANDIDATE GATES (no AI, pre-alias)                │   │ D recovery-delta     │  │ ACCEPTANCE (SkillOpt)  │  │ IN-EFFECT LEDGER  │
        │  A  held-out generalization: leave-one-DAY-out            │   │ grounding boost into │  │ • bounded edits L_t    │  │ (claim-neutral)   │
        │     + leave-one-PROJECT-out  (real session ids here)      │   │ _candidate_rank      │  │ • rejected-EDIT buf ℬ  │  │ write to          │
        │  E4 contradiction vs installed set                        │   │ (booster, not gate)  │  │   (soft, revivable)    │  │ skill_rule_effect │
        │  E5 accept/reject label → _candidate_rank calibration     │   └──────────────────────┘  │ • user-veto (hard)     │  │ [installed_at,    │
        └───────────────────────────────────────────────────────────┘                            └────────────────────────┘  │  retired_at)      │
                                                                                                                              └────────┬──────────┘
   ┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐              │
   │  SECONDARY B REPORTING  (directional only; MACHINE-CHECKABLE rules only; GATED behind a passing E2 placebo audit)   │              │
   │  replaces the unattributed snapshot headline at cli_skill.py:376-390                                                │              │
   │                                                                                                                    │              │
   │   scan of NEW session ─▶ Localizer (REUSE turns.py excerpts: correction / error-recovery / rejection, no LLM)       │              │
   │        │                   │                                                                                        │              ▼
   │        │                   ▼   Linker: LEXICAL + error_signature equality only (NO embeddings, NO LLM)             │      join on [installed_at, retired_at)
   │        │              rule_session_relevance  ◀── filter: error_signature/tool-family match (taxonomy → unknown) ───┼──────────────┘
   │        ▼                   │                                                                                        │
   │   E1 EXPOSURE GATE: keep only sessions where the skill was provably loaded (transcript grep) AND a skill-consuming  │
   │        │            agent source                                                                                    │
   │        ▼                                                                                                            │
   │   E3 DiD event study: each rule aligned to its own installed_at; never-installed modes = control series            │
   │        │            (exclude the rule's own SEEDING sessions from rate_before; pre-spike baseline, not the peak)    │
   │        ▼                                                                                                            │
   │   E2 placebo null: spike-matched shams through the SAME pipeline ──▶ bootstrap null (≥200 draws or SILENT)         │
   │        ▼                                                                                                            │
   │   M6 BH-FDR across the k rules tested this run ──▶ per-rule directional verdict + "association, not causation" +    │
   │        │            human-learning-confound footer + cohort-N disclosed                                            │
   │        └──▶ trailing retirement: a rule whose relevant cohort regresses beyond the null ⇒ demote ⇒ ℬ               │
   └──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
   ┌────────────────────────── P3 (OPT-IN, DISCLOSED 2nd EGRESS) ──────────────────────────┐
   │  C surrogate re-judge on HELD-OUT evidence (abstains if none), placebo-calibrated,      │◀── behind config flag
   │  ranker/tie-breaker only — never a gate                                                 │
   └─────────────────────────────────────────────────────────────────────────────────────────┘
```

Two structural facts the diagram encodes: (1) the **primary** value lands entirely in the candidate stage (A/D/E4/E5) and in the claim-neutral ledger — none of it depends on B being trustworthy. (2) The **B reporting box is fenced**: it runs only on machine-checkable rules, only for exposure-verified sessions, only after a placebo audit demonstrates the null is separable, and it *replaces the headline* of the cli_skill.py:376-390 trend (the coarse snapshots stay as append-only "corpus weather," demoted from "validation").

---

## 4. Mechanisms in detail

### 4.1 PRIMARY — A: held-out, burst-robust generalization (replaces the no-op session-jackknife)

- **What it does:** rejects candidates whose support does not survive independent resampling — i.e. the pattern rests on a single day's burst or a single project.
- **Why not leave-one-session-out (the earlier draft's "jackknife"):** support is a *distinct-session count* — each session contributes exactly 1 — so removing any one session drops the count by exactly 1 and `min_i support(S\{s_i}) = n−1` for **every** i. Leave-one-session-out is deterministic and adds nothing over the threshold `n ≥ 4`; it is a threshold bump wearing a resampling costume, and "one afternoon's burst" is *multiple* sessions clustered in time, which distinct-session jackknife passes as independent draws (pseudo-replication). We discard it.
- **What we do instead — resample over the axes that actually carry the burst/pseudo-replication risk:**
  - **Leave-one-time-bucket-out:** bucket the candidate's evidence sessions by calendar **day** (from `start_time`, index.py:74). Require support to clear `MIN_SIGNATURE_SESSIONS`=3 distinct sessions **after removing the single most-populous day**. A one-afternoon burst collapses to one bucket and fails.
  - **Leave-one-project-out:** bucket by project (the `excluded_projects`/project surface already threaded through `select_skill_candidates`, select.py:177). Require support to survive removal of the single most-populous project. A signature concentrated in one repo fails.
  - Both must pass. This directly implements the source brief's A while respecting its own warned-of confounds (power starvation, project-phase confound, pseudo-replication).
- **Where the real session ids are:** this runs at **candidate time, pre-distill**, where `turns.add_env_candidates`/`add_rejection_candidate` still hold **real** session ids (aliasing to `case-01…` happens only at distill.py:156-157). So A applies to **every** candidate, including those that become prose rules — it is not restricted to machine-checkable rules the way B is.
- **Seam:** candidate stage. Add `daybucket_survives`/`projectbucket_survives` alongside `support` in the `SkillCandidate` the pool ranks (`select.py` `_candidate_rank`, `turns.add_env_candidates`/`add_rejection_candidate`). Deterministic SQL/Python over the day- and project-bucketed distinct-session sets.
- **Cost:** zero egress; O(n) per candidate.
- **Currency (prioritization heuristic, not a gate, no E-label):** a still-live / flat-or-rising signature is ranked above a decayed-to-zero one, using the existing `_decayed_support` (cli_skill.py:52). **Never used to reject** — rejecting a decaying failure would punish a problem the human already fixed (the temporal-holdout backwards confound). Prioritize-only.

### 4.2 PRIMARY — D: recovery-delta grounding (booster, honest about survivorship)

- **What it does:** the strongest *local* efficacy evidence — where `extract_error_recoveries` (turns.py:249, `EnvExcerpt{action,error}`) yields a failed→recovered pair, test whether the candidate's guidance matches the corrective delta between the failing and succeeding attempt. Deterministic at the signature level.
- **Honest bound:** proves the encoded action worked **at least once** in the user's own history — a natural experiment, not a hypothetical. It does **not** generalize forward, and it suffers **survivorship**: we only see failures that *recovered*, so D validates best exactly where the agent already self-corrects — the cases least in need of a standing rule. Therefore D is a **grounding booster on `_candidate_rank`, capped in weight, never a gate.** Guard the budget so it doesn't fill with self-fixing cases.
- **Localizer inputs D reuses (no LLM):** `extract_correction_turns` (turns.py:123, `TurnExcerpt{before,correction,after}`) and `add_rejection_candidate` (turns.py:427) supply the *before/wrong-action* and *improvement-principle* fields. (These are **strong heuristic localizers**, not ground truth: a human redirect marks a fault *step*, not the fault's *cause* or the correct fix — labeled accordingly.)
- **Seam:** upgrade the existing `grounding` term in `_candidate_rank` from "has evidence sessions" to "encodes an observed fix"; add recovery-delta extraction + a signature match test. Reuses `extract_error_recoveries` and the excerpt_loader seam that already feeds distill.
- **Cost:** zero egress (signature match); any semantic match folds into the *existing* distill call, adding no egress.

### 4.3 The forward in-effect ledger — claim-neutral plumbing (the one genuinely-missing record)

- **What it does:** at `mark_installed` (store.py:143), write/close `[installed_at, retired_at)` intervals in a new `skill_rule_effect` table so a later session can ask "which rules were live at my `start_time`." **This asserts nothing about efficacy** — it is the substrate B needs and the record Q1 confirmed is entirely absent (grep for `rule_session`/`in_effect` returns nothing).
- **Buildability note (addresses B4):** `mark_installed(conn, rules)` receives only `SkillRule` objects. Writing the *interval* (fingerprint, installed_at, retired_at) needs nothing new — those are rule-level fields that already exist (store.py:42-58). Writing a `trigger_signature` onto the interval **does** need new plumbing and is only possible for machine-checkable rules; see §4.5 and §5.1 for the exact schema/thread, and the scope limit it forces.
- **Seam:** `mark_installed` (store.py:143), call site cli_skill.py:696; append-only, never mutate old rows (same discipline as the "two tables, not an ALTER" snapshot comment, store.py:174-176). Keep the "store write failure only warns" guard.
- **Cost:** zero egress; 1 table.

### 4.4 Acceptance + hygiene: E4 contradiction, edit budget, two buffers, E5 label

Transfer of SkillOpt's `ℬ` and bounded-edit "textual learning rate," **re-engineered as a trailing retirement gate** (§1.2) rather than a pre-deploy strict-inequality gate, plus Trace2Skill's conflict guardrails.

- **E4 contradiction gate (safety, always-on):** before admitting an edit, reject a candidate that contradicts a kept rule or a known-good recovery delta (a rule saying "avoid X" when a delta shows "X fixed it"). Port Trace2Skill Stage 3 deterministically: confirm the target section exists, reject edits whose rendered region contradicts an installed rule, re-validate the rendered `SKILL.md` parses (`render.py`/`schema.py`). On contradiction, synthesize rather than append a duplicate. Zero egress.
- **Bounded edits (`L_t`):** cap net rule churn per run. We cap active installed lessons at `MAX_INSTALLED_RULES=5` and proposed lessons at `MAX_RULES=5`; add an explicit **edit budget** — at most `L_t` add/replace/drop operations per run (default `L_t=3`, floor 2), slotting into `merge_rules` (cli_skill.py:180). Tighter than existing caps, never looser.
- **Two buffers, deliberately different strengths** (SkillOpt has no human channel):
  - **Rejected-EDIT buffer ℬ (soft, self-generated, revivable):** when an installed rule's trailing relevant cohort (§4.5) *failed to improve or regressed above the placebo null*, record `(fingerprint, guidance, trigger-signature, measured DiD estimate, null band, cohort_n, observed failure pattern)` and demote to `state='dropped'`. On the next merge, ℬ is **soft guidance** ("this edit was tried and didn't earn its keep") — a materially-changed guidance (new fingerprint, store.py:6-8) may still be proposed. Half-life decayed like support (30-day, cli_skill.py:49/52) so an old demotion doesn't suppress forever.
  - **User-rejection buffer (hard veto):** the existing `reject()`/`rejected_fingerprints()` path (store.py:229/:83). **Invariant: a favorable objective trend must never override an explicit user veto.** `upsert_seen` won't revive a rejected fingerprint unless guidance materially changes; kept exactly. `dropped` is revivable, `rejected` is not — they never cross-contaminate.
- **E5 accept/reject calibration:** the human preview decision is the *only* signal with human ground truth (already captured via `reject()`/`rejected_fingerprints`). Use it to calibrate which `_candidate_rank` features predict acceptance, and as the honest top-of-funnel proxy. Zero egress; half-built already.
- **Acceptance stance:** install optimistically **behind the existing human preview** (hard-deny/PII/TruffleHog gate + preview stays). The *validation* decision is trailing: demote-and-buffer rules whose cohort regresses. Never strict-inequality on noise.
- **Seam:** `merge_rules` (cli_skill.py:180) for edit budget + E4 + ℬ-suppression; a new retirement pass reads §4.5's DiD estimate and calls a `demote`/`state='dropped'` path (not `reject`). Deterministic.

### 4.5 SECONDARY — B: trigger-relevance × outcome, as a directional DiD event study

**Read this whole subsection as bounded.** B is the *only* place in the layer that reports anything efficacy-flavored, and it is confounded. It is demoted from the earlier draft's "primary." What follows fixes the earlier draft's four B-specific errors head-on.

**B0. The corrected justification (fixes B1).** Conditioning on trigger-relevance removes the **floating-denominator / compositional** confound only — it makes the pre/post populations comparable (sessions where failure X could occur). It **does NOT** control calendar-time drift, regression-to-the-mean, or the user independently getting better at X — those live *inside the relevant cohort*, not in the denominator. So relevance-conditioning is necessary but nowhere near sufficient, and B is **never** described as "drift-controlled" on that basis. The only drift control we add is the DiD design (B3 below), and it controls *common* trend only.

**B1. Scope (fixes B4 — state loudly).** B runs on **machine-checkable-trigger rules only**: env-signature (`error_signature`, turns.py:208), rejection-derived, and 1-A rules. Three code facts force this:
1. `SkillRule` (schema.py:42-52) has **no** `trigger_signature` — only prose `trigger: str`. `error_signature()` lives on the *candidate* (turns.py) and is gone by the time a distilled rule exists.
2. `mark_installed(conn, rules)` receives only `SkillRule`s. To attach a signature to a `skill_rule_effect` interval we must thread a `trigger_signature` field **onto `SkillRule` at candidate time** and carry it through distill+merge. This is buildable **only for synthetic/1-A candidates** (which have a signature); it is **not** recoverable for distilled prose.
3. `evidence_session_ids` are stored as anonymized aliases (`case-01…`, distill.py:156-157), real ids not recoverable from the stored rule — so distilled prose rules can be neither B-attributed nor back-jackknifed. We therefore **drop the earlier draft's "jackknife the distilled rule's evidence ids" bullet** as unbuildable, and rely on A running at candidate time (§4.1) for prose-rule generalization instead.

Consequently: **distilled prose rules — the main output of the single distill call — are excluded from B.** The attribution surface prints `relevance: unknown` for them. This is a scope truth, not a caveat.

**B2. Relevance predicate (fixes M8).** Commit to **`error_signature` / tool-family equality** as the relevance predicate. **Taxonomy is explicitly rejected** as a predicate: a 12-value enum makes a rule "relevant" to essentially the entire failure pool for that mode — barely narrower than today's unattributed trend. `error_signature` equality is the genuinely narrow signal; it recurs rarely, so **the relevant cohort collapses to small N and B goes silent for most rules. That is the honest outcome, and B publishes the per-rule cohort-N on every line so silence is visible.** Taxonomy-only rules are bucketed with prose as `relevance: unknown`.

**B3. Design = difference-in-differences event study (fixes B1's drift gap, B2's baseline contamination, M9's undefined "matched").**
1. **Event alignment:** each rule aligned to its own `installed_at` (event-time 0), read from the `skill_rule_effect` interval (§4.3). Each rule is its own timeline — this is what controls **calendar-time** corpus drift, and *only* that.
2. **Never-installed control series:** the comparison is `Δ_treated − Δ_control`, where the control is the recurrence trajectory of **failure modes/signatures for which no rule was ever installed**, over the same calendar span. Common drift (the user's whole workflow improving, tooling changes) is differenced out.
3. **Baseline definition — exclude the seeding sessions, do not baseline on the spike (fixes B2/M9):** `rate_before` is computed on relevant sessions in a **pre-spike trailing window**, **excluding the rule's own seeding/evidence sessions** (which are by construction failures of X sitting in the pre-install spike). "Matched" is now concrete: matched on (i) the same `error_signature` relevance predicate, (ii) event-time alignment, (iii) the never-installed control comparison. We **never** use the immediately-preceding window (the spike peak) as the level — that maximizes RTM rather than blunting it.
4. **What DiD still cannot remove (the honest cap):** the install-after-spike RTM is **treatment-specific by construction** (we install *because* the treated mode spiked; control modes did not), so DiD does **not** difference it out. And the §1.3 human-learning confound is likewise treatment-specific and invisible here. So even the DiD estimate is **directional, confounded association** — never causal, never "attributed" in the clean sense.

**B4. Exposure gating E1 (fixes M5 — the highest-value single addition).** A rule in `SKILL.md`/`AGENTS.md` is not proof the agent loaded or attended to it, and ClawJournal scans **many** agents (Claude, Codex, Gemini, opencode, Kimi, Cursor, Copilot, aider…), only some of which consume the `clawjournal-lessons` skill at all. So B's treated cohort is restricted to sessions where **(a) the source agent is one that consumes the skill**, and **(b) the skill was provably loaded** — deterministically detectable by grepping the later transcript for the skill-load / guidance tokens (zero egress). Residual caveat, stated on the surface: **presence ≠ attended-to.** Without E1, B's after-install cohort is polluted with sessions the rule could not have influenced and the placebo comparison is meaningless.

**B5. Placebo null E2 (fixes B3).** Inject **sham rules**: a *real* trigger signature (drawn from actual session `error_signature`s) paired with **scrambled/irrelevant guidance**, run through the *exact same* exposure-gated DiD pipeline. Crucial specification the earlier draft omitted:
- **Sham install-time placement mirrors real selection:** each sham is "installed" **immediately after a spike in its own trigger**, exactly as a real rule is. Without this the sham null is centered near zero and any RTM-driven real rule trivially "beats" it. With it, the null captures **drift + RTM**.
- **What beating the null does and does NOT prove:** a real rule's estimate exceeding the null proves it exceeds **drift + RTM only**. It does **NOT** address the §1.3 human-learning confound — a sham is *never shown to the user*, so it has **zero** human-learning component. We **delete** the earlier draft's claim that beating the sham bounds "human-learning." The correct sentence, printed verbatim on the surface: *"exceeds a null capturing drift + RTM; does NOT capture the human-learning confound, which is unremovable (§1.3)."*
- **Null resolution (fixes B3's N inconsistency):** build the null by bootstrapping over (spike events × guidance permutations) to target **≥200 draws**. Report the real estimate's position in the null as a bootstrap p-value **only when ≥200 draws exist**; below that print `insufficient null resolution — B suppressed`. We **drop** the fixed "95th percentile of ~20 draws" framing entirely (with 20 draws the 95th percentile is essentially the unstable max). `skill_placebo_min_draws` default 200.

**B6. Multiple comparisons M6.** Up to `MAX_INSTALLED_RULES=5` active rules tested per run against per-rule nulls still creates repeated-comparison risk. Apply **Benjamini-Hochberg FDR at q=0.1** across the k machine-checkable rules tested in a run (× signature, if a rule targets several), and **disclose k and q on the surface**. A rule "counts" only after FDR correction.

**B7. Cold-start / thin-corpus (explicit "insufficient data," never a number).** If exposure-gated relevant post-install sessions `< N_min` (default 8), or `< N_min` pre-spike baseline sessions after excluding seeding, or `< 200` null draws, B prints `insufficient data (n=…, need …)` for that rule — **not** a Δ. Most early rules will be silent by design. This is correct, not a bug.

**B8. Numerator honesty (MINOR).** The recurrence predicate — `ai_failure_value_score>=3 OR ai_outcome_badge IN ('failed','abandoned')` (select.py:236-241) plus `json_each(ai_failure_modes)=X` (index.py:2166) — is a **judge opinion**, high-variance and confounded (as §1.2 concedes for the input scores). The placebo absorbs this **only if judge noise is guidance-independent**; we state that assumption on the surface. Wilson/binomial intervals **assume iid Bernoulli**, which personal-corpus sessions violate (same project/day autocorrelation); we therefore report **cluster-robust (by day/project) intervals** or flag the independence violation explicitly, and never let a raw Wilson band overstate confidence.

- **Localizer + Linker mechanics (deterministic, no embeddings — fixes M10):** the Localizer is `turns.py`'s three excerpt flavors (correction / error-recovery / rejection), already implemented, no LLM. The Linker scores each **in-effect** machine-checkable rule against the excerpt using **lexical/token overlap + `error_signature`/tool/file equality only**. **Embedding cosine is removed** — there is no embedding capability in `skill/`, and an embedding model is either a heavy new local dependency or a *second egress* the cost table can't absorb; given the local-first invariant the honest default is to drop it. Any semantic mid-band adjudication is deferred to the opt-in C stage (§4.6) or to Mode B — it never feeds the zero-egress B number.
- **What replaces the old surface:** the `mode_rates()`/`objective_rates()` snapshot diff (cli_skill.py:376-390) stays computed and stored (cheap, append-only, still useful as a coarse **corpus-health** line) but is demoted from "validation" to "corpus weather." The per-rule headline becomes the FDR-corrected DiD estimate + null verdict + cohort-N + the association-not-causation + human-learning footer, and `relevance: unknown` for prose/taxonomy-only rules.
- **Seam:** a new deterministic scan-time (or lazy-at-`generate_skill`) pass reading `skill_rule_effect` + writing `rule_session_relevance`; the attribution compute lands where the trend is emitted (cli_skill.py:374-390) and where snapshots are written (cli_skill.py:701-703, same `eligible_scored > 0` guard). Zero egress; SQL over `idx_sessions_start_time` (index.py:151) + `json_each`.

### 4.6 OPTIONAL — C surrogate re-judge (opt-in, disclosed SECOND egress)

- **What it does:** one LLM call (routed through the free distill egress path) sees a redacted **held-out** failure-evidence session + the candidate rule and answers **adversarially** ("give the strongest reason this rule would NOT have helped this session — then a yes/partial/no"). Aggregate → hit-rate, placebo-calibrated by E2 shams. Ranker/tie-breaker only, **never a gate.**
- **Held-out inapplicability (fixes M11):** a rule's entire support is often 3–4 sessions, all of which are its seeding evidence. If no **non-seeding, trigger-relevant** evidence exists, C **abstains** (prints `no held-out evidence`) — it does not silently reuse seeding sessions (tautological) and does not guess. Gate on a minimum held-out count (`skill_surrogate_min_heldout`, default 2).
- **Why opt-in and disclosed:** it is the **first architectural break of the single-default-egress invariant** (§6). Even free through the user's CLI it is a *second* egress and *N* calls (batchable→1), grows the privacy surface, and is opinion not ground truth (sycophancy/hindsight — the model that distilled grades the distillation).
- **Guardrails if enabled:** re-thread the distill isolation flags verbatim — `claude_safe_mode=True`, `claude_permission_mode="default"`, `claude_tools=""`, `codex_sandbox="read-only"` (distill.py:239-247); anonymize + `_scrub` BEFORE the call (distill.py:142-153); feed only already-gated sessions (`release_gate_blockers`, index.py:2495); reuse `run_agent_json_call` (benchmark/generate.py:143) → `run_default_agent_task` (scoring/backends.py:493).
- **Cost:** N calls per run (batchable to 1), behind `config --skill-surrogate-verify` (default off), disclosed in preview.

---

## 5. Data model + pipeline changes

### 5.1 New tables (store.py, `_ensure_*` pattern; append-only, gated migration — never rewrite historical migrations)

**`skill_rule_effect`** — the forward in-effect interval log (closes GAP Q1). Written in `mark_installed` (store.py:143).
```
skill_rule_effect(
  id INTEGER PK, fingerprint TEXT, taxonomy TEXT,
  trigger_signature TEXT NULL,   -- machine-checkable feature set; NULL for distilled prose rules
  installed_at TEXT, retired_at TEXT NULL   -- append a close row on demote/reject/replace
)
```
Interval semantics: "in effect at t" iff `installed_at <= t AND (retired_at IS NULL OR t < retired_at)`. On re-install after demotion, open a new interval (append-only; never mutate old rows). **`trigger_signature` is NULL for prose rules** — the honest encoding of B's scope limit (§4.5 B1). Populating it for machine-checkable rules requires threading a new `trigger_signature` field onto `SkillRule` (schema.py) from the candidate (§5.3).

**`rule_session_relevance`** — the Linker output (closes GAP Q2/Q5's missing join).
```
rule_session_relevance(
  session_id TEXT, fingerprint TEXT, effect_id INTEGER,   -- FK to the live interval
  weight REAL, band TEXT,                    -- owns/partial/weak/unrelated/unknown
  fault_type TEXT,                           -- skill_wrong | skill_missing | none
  exposed INTEGER,                           -- E1: 1 iff skill provably loaded in this session's transcript
  targeted_recurred INTEGER,                 -- 0/1 did signature X recur in this session
  computed_at TEXT,
  PRIMARY KEY(session_id, fingerprint, effect_id)
)
```
Only `error_signature`/tool-family matches feed the B metric; taxonomy-only/prose rows may exist with `band='unknown'` and are excluded from Δ. B further restricts to `exposed=1` rows (§4.5 B4).

**`skill_rejected_edits`** — the soft ℬ buffer (distinct from `state='rejected'`, the hard user veto).
```
skill_rejected_edits(
  id INTEGER PK, fingerprint TEXT, guidance TEXT, trigger_signature TEXT,
  measured_did REAL, null_p REAL, cohort_n INTEGER,
  observed_failure_pattern TEXT, buffered_at TEXT
)
```
Half-life decayed on read (reuse the 30-day pattern, cli_skill.py:49/52). Read in `merge_rules` as soft suppression; never overrides a materially-changed fingerprint.

**Optional `skill_attribution_snapshots`** — per-run DiD estimate + null p + cohort-N + FDR-k per machine-checkable rule (parallels the two existing snapshot tables; separate table so old DBs don't break).

### 5.2 Reused fields (no schema change — already queryable, GAP Q4/Q5)

- Split/alignment axes available today: `start_time` (index.py:74) for event-time and day-bucketing; `ai_scored_at` (:114); project for project-bucketing; `sha256(session_id)%k` in reserve. B uses `start_time` intervals + never-installed control; A uses day/project buckets.
- Targeted-recurrence predicate: `json_each(ai_failure_modes)=X` (index.py:2166) + `ai_failure_value_score>=3 OR ai_outcome_badge IN ('failed','abandoned')` (select.py:236-241) — **a judge output, not ground truth** (§4.5 B8). Recovery labels via `json_each(ai_recovery_labels)` (index.py:2158) for D.
- Durable per-rule `trigger` + `taxonomy` (store.py:47, schema.py:45/:50). `evidence_json` stays backward-looking, alias-obscured (`case-01…`, distill.py:156) — **not** repurposed as a forward link (consistent with §4.5 B1's dropping of the prose-jackknife bullet).

### 5.3 Pipeline / function changes

- **`select_skill_candidates` (select.py:177):** add optional day/project bucket params threaded through `_in_window` (:212) for A's leave-one-bucket-out and E2 sham placement. Add `daybucket_survives`/`projectbucket_survives` to the returned `SkillCandidate`; fold into `_candidate_rank` (with D's upgraded grounding term and E5-calibrated weights).
- **`turns.add_env_candidates`/`add_rejection_candidate`:** emit `trigger_signature` (already computed internally) so it can be threaded onto `SkillRule` and persisted to `skill_rule_effect` **for machine-checkable rules only**.
- **`schema.py` `SkillRule`:** add optional `trigger_signature: str | None = None` (default None for distilled prose). Carried through merge; consumed by `mark_installed`.
- **`distill.py`:** unchanged in the default path (still the sole 1 LLM call, distill.py:280). Only the opt-in C verifier (Phase 3) adds a call.
- **`merge_rules` (cli_skill.py:180):** enforce edit budget `L_t`; consult `skill_rejected_edits` (soft) and `rejected_fingerprints` (hard); run E4 contradiction vs installed set before admitting.
- **`mark_installed` (store.py:143):** ALSO append `skill_rule_effect` intervals (open new, close replaced/dropped), with `trigger_signature` where present. Call site cli_skill.py:696; keep the "store write failure only warns" guard.
- **cli_skill.py:374-390 / 701-703:** compute + display the FDR-corrected DiD headline for machine-checkable rules (replacing the unattributed trend), `relevance: unknown` for the rest; still write the coarse snapshots under the `eligible_scored > 0` guard; also write `skill_attribution_snapshots`.
- **`config.py`:** `skill_surrogate_verify` (bool, default false), `skill_edit_budget` (int, default 3), `skill_placebo_min_draws` (int, default 200), `skill_attr_min_cohort` (int, default 8), `skill_surrogate_min_heldout` (int, default 2), `skill_fdr_q` (float, default 0.1). Append-semantics of existing list flags (`--exclude`/`--redact`/`--redact-usernames`) untouched.
- **`cli_skill.py` new subcommands (read-only):** `clawjournal skill attribution` (per-rule DiD + null + cohort-N + honesty footer; `relevance: unknown` for prose), `clawjournal skill buffer` (ℬ vs user-rejections). No new egress.

---

## 6. Invariants preserved

**Locked invariants (this layer must not violate any):**

1. **Local-first:** no service upload in Mode A. Nothing here uploads; all new state is local SQLite.
2. **ONE distill call by default.** The default path (A/D/E1/E2/E3/E4/E5 + the ledger + the whole B DiD loop) is deterministic SQL/Python — **zero** added AI calls. The surrogate verifier (C) is the *only* AI addition and is **opt-in, off by default, disclosed** as a second egress. **No embedding model** is introduced (M10).
3. **Privacy gates unchanged and re-asserted:** anonymize + `_scrub` before any egress (distill.py:142-153); only `release_gate_blockers`-passing sessions feed any AI; the mandatory TruffleHog post-redaction gate in the share path is untouched. The C verifier (if enabled) re-threads the exact isolation flags (§4.6).
4. **Caps:** ≤5 proposed (`MAX_RULES`), ≤5 active installed (`MAX_INSTALLED_RULES`), recency-decayed (`_decayed_support`, 30-day). New edit budget `L_t≤3` is *tighter*, never looser.
5. **User veto is supreme:** the hard user-rejection buffer always outranks any favorable objective trend (§4.4). `upsert_seen` won't revive a rejected fingerprint.
6. **Honesty:** every reported number carries its claim level (§2.4); no surface prints "caused"; B prints its confound footer and cohort-N; prose rules print `relevance: unknown`.

**Cost / egress budget table:**

| Component | Phase | AI calls added (default) | AI calls (if opt-in on) | New local state | Privacy surface change |
|---|---|---|---|---|---|
| A held-out generalization | P1 | 0 | 0 | none (compute) | none |
| D recovery-delta grounding | P1 | 0 | 0 | none | none |
| E4 contradiction gate | P1 | 0 | 0 | none | none |
| E5 accept/reject calibration | P1 | 0 | 0 | reuse states | none |
| `skill_rule_effect` ledger | P1 | 0 | 0 | 1 table | none (local) |
| Rejected-edit buffer ℬ | P1 | 0 | 0 | 1 table | none (local) |
| Linker relevance (lexical/signature) | P2 | 0 | 0 | 1 table | none |
| E1 exposure gating | P2 | 0 | 0 | reuse relevance row | none (transcript grep, local) |
| B DiD attribution + snapshots | P2 | 0 | 0 | 1 table | none |
| E2 placebo audit | P2 | 0 | 0 | ephemeral | none (shams never shipped) |
| **Default-path total** | **P1–P2** | **0** | — | 4 tables | **none** |
| C surrogate verifier | P3 | **0 (off)** | N (batchable→1) per run | verdict cache | **increases** (more session content re-sent) — disclosed, opt-in |

**Locked decision:** default Mode A remains exactly one AI egress (the distill call). Turning on C is the single, explicit, disclosed exception. **No embedding-model egress is ever introduced by this layer.**

---

## 7. Phasing

### Phase 1 — deterministic candidate validation + the claim-neutral ledger (ship first, NO new AI call)
Build the **primary** value: A (leave-one-day-out + leave-one-project-out generalization) + D (recovery-delta grounding booster on `_candidate_rank`) + E4 contradiction gate + E5 accept/reject calibration; the `skill_rule_effect` interval ledger at `mark_installed` (with `trigger_signature` for machine-checkable rules, NULL for prose); rejected-EDIT buffer ℬ + edit budget `L_t` in `merge_rules`; hard user-rejection buffer kept strictly separate and supreme.
- **Done when:** a candidate resting on one day's burst or one project fails A; a rule whose guidance matches an observed recovery delta ranks higher; a contradictory edit is rejected; installing a rule writes an in-effect interval; ℬ soft-suppresses a re-proposed losing edit while a user veto hard-blocks; edit budget caps churn at `L_t`. Covered by `tests/skill/` (fake backend, TruffleHog bypassed).
- **Can honestly claim:** proposed rules target real, out-of-sample-stable, historically-grounded, non-contradictory patterns; we now *record* which rules were live when. **Cannot claim:** any efficacy.

### Phase 2 — the honest (directional) attribution story — GATED behind a placebo audit (still NO new AI call)
Build: Linker (lexical + `error_signature`/tool-family only, **no embeddings**) → `rule_session_relevance`; E1 exposure gating; B as the DiD event study (never-installed control, seeding sessions excluded, pre-spike baseline); E2 spike-matched placebo null; M6 BH-FDR across tested rules; cold-start silence; wire the trailing retirement gate.
- **Gating rule (fixes the scope-size concern):** the B reporting surface **does not ship** until the **E2 placebo audit passes on this corpus** — i.e. shams and real rules are demonstrably separable at ≥200 null draws. Until then, `clawjournal skill attribution` prints only `insufficient null resolution`. Building placebo/attribution machinery does not, by itself, earn a B number.
- **Done when:** for each machine-checkable, exposure-gated rule with sufficient cohort, the run prints "DiD estimate vs never-installed control = …; placebo null p = …; cohort n=…; FDR q=0.1 across k=…; verdict: exceeds/within null" with the **association-not-causation + human-learning-confound** footer; prose/taxonomy-only rules print `relevance: unknown`; thin cohorts print `insufficient data (n=…, need …)`; a rule whose cohort regresses beyond the null is demoted into ℬ.
- **Can honestly claim:** among exposure-verified, `error_signature`-relevant later sessions, the targeted failure trended down relative to a never-installed control and above a drift+RTM placebo null — **directional, personal, non-causal, machine-checkable rules only.** **Cannot claim:** causation (treatment-specific RTM + human-learning remain), coverage of distilled prose rules, or cross-user generalization.

### Phase 3 — opt-in surrogate verifier (disclosed SECOND egress)
Build: C adversarial re-judge on held-out evidence (abstains if none), placebo-calibrated, ranker/tie-breaker only, behind `config --skill-surrogate-verify` (default off), re-threading distill isolation flags; verdict cache.
- **Done when:** with the flag on, each candidate with ≥`skill_surrogate_min_heldout` non-seeding trigger-relevant sessions gets an LLM plausibility score shown as a tie-breaker and disclosed as a second egress; with no held-out evidence it abstains; with the flag off, behavior is byte-identical to Phase 2.
- **Can honestly claim:** an LLM plausibility estimate above the sham band. **Cannot claim:** a standalone efficacy proof; it is never a gate.

**What NO phase can honestly claim:** that an installed rule *caused* better future outcomes. The human-learns-the-same-lesson-from-the-same-sessions confound (§1.3) is treatment-specific, structural, and uncontrollable without re-runnable tasks. **Top-line for the whole system:** *"Rules target real, out-of-sample-stable, historically-grounded failure patterns (A/D); and among exposure-verified, trigger-relevant later sessions the targeted failure recurs less than a drift-controlled, placebo-audited baseline (B/E1/E2/E3) — directional, personal, non-causal, and, for B, machine-checkable rules only."*

---

## 8. Test plan (mirror `tests/skill/`; fake backend; TruffleHog bypassed)

Conventions to reuse: autouse `CLAWJOURNAL_SKIP_TRUFFLEHOG=1` (tests/conftest.py:16), autouse tmp-config (never clobber `~/.clawjournal/config.json`), in-memory/tmp SQLite via `test_store.py`/`test_select.py` fixtures, fake caller for any AI (`test_distill.py`/`test_generate.py` — no real subprocess). Extend `tests/skill/test_partition.py` for split/bucket logic.

- **`test_rule_effect.py` (new):** `mark_installed` opens intervals for the kept set, closes intervals for replaced/dropped rules, never mutates historical rows; `trigger_signature` present for a synthetic candidate, NULL for a distilled prose rule; "in effect at t" returns the right set across install→demote→reinstall.
- **`test_generalization.py` (new):** a candidate whose evidence is 5 sessions all on one **day** fails leave-one-day-out; one all in one **project** fails leave-one-project-out; a candidate spread across ≥3 days and ≥2 projects survives; assert that leave-one-*session*-out is **not** used (regression guard against the no-op).
- **`test_recovery_grounding.py` (new):** a candidate whose guidance matches an `extract_error_recoveries` delta gets the boosted grounding term; a candidate with no recovered pair gets the base term; D is capped and never rejects.
- **`test_currency.py`:** a flat/rising signature is prioritized; a decaying-to-zero one is de-prioritized but **not rejected** (assert it can still be proposed).
- **`test_contradiction.py` (new):** an edit contradicting a kept rule or a known-good recovery delta is rejected; the rendered `SKILL.md` still parses.
- **`test_linker.py` (new):** a fabricated session with a known `error_signature` and an in-effect machine-checkable rule whose `trigger_signature` matches → `band='owns'`, `fault_type='skill_wrong'`; no match → `skill_missing`; **taxonomy-only or prose trigger → `band='unknown'`, excluded from Δ**; assert **no embedding call** is made.
- **`test_exposure.py` (new):** a session whose transcript lacks the skill-load tokens is `exposed=0` and excluded from B's cohort; a wrong-agent source is excluded; only provably-loaded skill-consuming sessions remain.
- **`test_attribution_did.py` (new):** construct treated + never-installed-control trajectories with known recurrence; assert the DiD estimate, that seeding sessions are excluded from `rate_before`, that the baseline is **not** the immediately-preceding spike window, that cohort-N is reported, and that collinearity (two rules sharing a relevant session) is flagged, never summed.
- **`test_placebo.py` (new):** shams get **spike-matched** install placement; with `<200` null draws B prints `insufficient null resolution`; with `≥200`, a real rule within the null reports "within null"; assert the surface string does **not** claim to bound human-learning; shams never appear in the installed set or preview.
- **`test_multiplicity.py` (new):** BH-FDR at q=0.1 across k tested rules; assert k and q are disclosed; a rule that would pass an uncorrected per-rule threshold but fails FDR is reported as "within null."
- **`test_coldstart.py` (new):** below `skill_attr_min_cohort` post-install exposure-gated sessions, B prints `insufficient data (n=…, need …)`, never a number.
- **`test_merge.py` (extend):** edit budget `L_t` caps net edits; ℬ soft-suppresses a re-proposed losing fingerprint but a materially-changed guidance (new fingerprint) still passes; **hard** user-rejection blocks even when the objective trend is favorable (supremacy invariant).
- **`test_buffer_separation.py` (new):** ℬ (`skill_rejected_edits`, `state='dropped'`, revivable) vs user veto (`state='rejected'`, not revivable) never cross-contaminate.
- **`test_surrogate_heldout.py` (new):** with all support sessions being seeding evidence, C **abstains** (`no held-out evidence`); with ≥`skill_surrogate_min_heldout` non-seeding relevant sessions, C runs; isolation flags asserted.
- **`test_orchestration.py` (extend):** end-to-end `generate_skill` with the fake backend makes exactly ONE AI call with the flag off; with `skill_surrogate_verify` on, exactly the expected additional (batched) calls; snapshots written only when `eligible_scored > 0`.
- **`test_cli_skill.py` (extend):** `skill attribution` renders the association-not-causation + human-learning footer, cohort-N, FDR-k, and `relevance: unknown` for prose; `skill buffer` renders ℬ vs user-rejections.

---

## 9. Open questions + risks + epistemic ceiling

**Epistemic ceiling (restated, load-bearing).** No local, no-rerun, no-ground-truth signal can establish causal efficacy. The maximum honest claim is A/D generalization-and-grounding on the *input*, and a *directional, confounded, machine-checkable-only* B trend on the *output*. Every number carries its bounded framing; nothing prints "caused."

1. **Human-learning confound (structural, unremovable).** §1.3. Treatment-specific; invisible to DiD, placebo, and exposure gating (a sham/control has no human-learning component). No local mitigation exists; the top-line is capped at association at every surface.
2. **Regression to the mean (treatment-specific).** Rules install right after a spike; DiD's never-installed control does **not** difference this out (control modes didn't spike). Mitigations reduce but do not remove it: exclude seeding sessions, use a pre-spike baseline (not the peak), and the spike-matched placebo null quantifies the residual. Disclose that B ≠ causal because of it.
3. **B covers a minority of rules.** By construction (§4.5 B1) B does not touch distilled prose rules — the main output of the distill call. This is stated in the TL;DR, §2.4, §4.5, and on the surface (`relevance: unknown`). If prose coverage is ever wanted, it requires persisting a real `trigger_signature` and pre-alias `evidence_session_ids` on the rule at candidate time (new schema + plumbing) — deliberately **out of scope** here per "weaken, don't build scaffolding."
4. **Relevance predicate granularity (M8).** `error_signature` equality is narrow → B silent for most rules (accepted, cohort-N disclosed); taxonomy is too coarse → rejected as a predicate (bucketed as `unknown`). We do not paper over the silence.
5. **Exposure gap residual (M5/E1).** Transcript-provable load ≠ attended-to; multi-agent corpus filtered to skill-consuming sources. Stated on the surface.
6. **Multiple comparisons (M6).** BH-FDR across the k rules tested per run, k and q disclosed. Weekly runs still accumulate look-back multiplicity across runs — flagged as a residual; we do not claim per-run FDR controls the cross-run family.
7. **Judge-opinion numerator + autocorrelation (§4.5 B8).** Recurrence is a judge output, not ground truth; sessions are autocorrelated. Use cluster-robust intervals; placebo absorbs judge noise only if that noise is guidance-independent — stated as an assumption.
8. **Cold start / thin corpus (B7).** Explicit `insufficient data (n=…, need …)` verdict, never a fabricated number; most early rules are silent by design.
9. **Collinearity.** Multiple in-effect rules relevant to one session cannot be separated; report per rule, flag co-relevance, never sum credit.
10. **D's low-marginal-value trap (survivorship).** Recovery-match validates best where the agent already self-corrects. D is a capped grounding booster, never a gate; guard the budget against self-fixing cases.
11. **Retirement thrash.** ℬ half-life decay + edit budget `L_t` + require ≥`skill_attr_min_cohort` relevant sessions before demotion; `dropped` (revivable) not `rejected`.
12. **1-A / 1-B ordering.** **Locked build order:** 1-B Phase 1 candidate gates land first; 1-A candidates route through A/E4/D/ledger before any install, so 1-A never ships an ungated deterministic rule.
13. **Cost of C.** Even free via the user's CLI it is a second egress + N calls + larger privacy surface. Off by default, batched, disclosed, held-out-only (abstains if none), calibrated.
14. **Scope size (MINOR, addressed by phasing).** Phase 1 (candidate gates + ledger) delivers the primary, uncontested value alone. The entire B reporting surface is fenced behind a passing placebo audit (§7) so we never spend the reporting machinery on a corpus where the null isn't separable. Snapshot/attribution tables are append-only, small, local, never pruned (no daemon-racing pruner).

---

## Credit carried forward from the prior draft (kept intact)
- The floating-denominator diagnosis of the current trend (§0, §4.5) — correct and the one genuine improvement; we keep it and stop calling it "drift control."
- C as an explicitly opt-in, disclosed **second** egress with re-threaded isolation flags — respects the single-egress invariant.
- The soft ℬ (revivable `dropped`) vs hard user-veto (`rejected`) two-buffer distinction, and "user veto is supreme" — the right adaptation of SkillOpt's `ℬ` (which has no human channel).
- Real code anchors throughout, and the §1.3/§2.4/§9 honesty framing — the fixes above make the *mechanisms* live up to that framing rather than loosening it.
