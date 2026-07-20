# Adversarial Critique — Validation & Attribution Layer draft

> Staff-level adversarial review of the first draft of `validation-design.md` (2026-07-09).
> Four BLOCKING + eight MAJOR findings. The final design doc retracts the overclaims named here;
> the recurring verdict was: **weaken the claim rather than build machinery to rescue it.**
> Kept because it records *why* the design says what it says.

# Adversarial review — Validation & Attribution Layer (Posture 1-B)

Reviewer stance: staff-level, hostile. The doc is unusually honest in its framing prose
(§1.3, §2.4, §9.3–9.4) and the code anchors it cites are almost all real (spot-checked:
`_SUPPORT_HALFLIFE_DAYS=30.0`, `mark_installed` sig, distill aliasing, `error_signature`).
That earned honesty is exactly why the remaining slips are dangerous: they hide inside a
document that reads as rigorous. The core problem is that the **primary signal (B) is
promoted on a claim that is overstated, and its central measurement is mechanically biased
toward showing "improvement" whether or not any rule works.** Fix those before building.

Severity order: BLOCKING (design is wrong or unbuildable as written) → MAJOR → MINOR.

---

## BLOCKING

### B1. "Conditioning on relevance controls corpus drift" is false as stated — and it is the load-bearing justification for making B primary
**WHERE:** §0 ("Conditioning on relevance is what controls for corpus drift — the exact
confound the current trend ignores"); §4.d step 4 ("'relevant sessions,' like-with-like,
which is what controls corpus drift"); §2.3 rationale (ii).

**WHY IT'S WRONG:** Relevance-conditioning fixes the *floating-denominator / compositional*
confound — the current trend's denominator is "whatever `eligible_scored` counted this run,"
a different population each time. Conditioning on trigger-relevance makes the pre/post
populations comparable (sessions where failure X could occur). That is real and worth
keeping. But "corpus drift" as the options brief and §9.3 use it is **calendar-time drift +
regression-to-the-mean + the user independently improving on X**, and none of those live in
the denominator — they live *inside the relevant cohort itself*. A user who simply gets
better at X over the weeks after install produces a declining within-cohort rate with zero
rule effect. Relevance-conditioning does nothing about that. The options brief B entry says
this explicitly ("regression-to-the-mean is the dominant threat… common-cause confound…
remain"). The doc has quietly upgraded "removes the population-comparability confound" into
"controls corpus drift," and then rested the entire architectural choice of B-as-primary on
the upgrade.

**FIX:** Weaken the claim everywhere to exactly: *"relevance-conditioning removes the
between-failure-mode compositional drift from the denominator; it does NOT remove
within-cohort temporal drift, RTM, or the user's own improvement."* Then either (a) actually
adopt the drift control the doc dropped — the options-brief **E3 staggered-adoption /
difference-in-differences**, using never-installed failure modes as a contemporaneous control
series (each rule aligned to its own `installed_at`, event-time 0) — or (b) accept that B
cannot separate rule-effect from time and cap its headline at "targeted failure trended down
after install among relevant sessions; we cannot attribute the trend to the rule." Note the
doc's own §2.2 table lists an "E3" but redefines it as *currency* — the rigorous event-study
E3 from the source brief was silently deleted. That deletion is the single biggest
substantive regression from the analysis the doc claims to implement.

### B2. The before-install baseline is contaminated by the seeding sessions, so B mechanically manufactures "improvement"
**WHERE:** §4.d step 3 ("recurrence of X among relevant sessions **before** `installed_at`
vs **after**… with the matched pre-install baseline chosen to blunt regression-to-the-mean
(the pre-window immediately preceding install, same relevance predicate)").

**WHY IT'S WRONG:** A rule is minted *because* failure X recurred — the seeding sessions are
by construction failures of X, and they sit in the window immediately before `installed_at`
(that is when the signature crossed threshold). B's "before" relevant cohort is therefore
dominated by the very sessions that defined the failure, so `rate_before` is pushed toward 1
by selection. Any post-install rate is lower almost regardless of efficacy. Worse, the doc
picks **the pre-window immediately preceding install** as the baseline — that is the *peak*
of the spike that triggered installation. Comparing post-install to the peak is the textbook
way to *maximize* regression-to-the-mean, not "blunt" it. The mitigation does the opposite of
what it claims. This alone can produce a "targeted failure declined, beats the sham null"
verdict for a rule with no effect.

**FIX:** (1) Exclude the rule's own seeding/evidence sessions from `rate_before`. (2) Do not
use the immediately-preceding window as baseline; use a pre-spike or long trailing baseline,
or better, the E3 never-installed-control approach so the baseline is not a function of the
installation trigger. (3) Define "matched" concretely (matched on what — time distance?
project? none of it is specified today; see M8). Until (1)+(2) exist, B's Δ is not
reportable.

### B3. Placebo calibration is claimed to bound "human-learning," which it cannot — directly contradicting §1.3
**WHERE:** §4.e/E1 ("a number that provably exceeds what noise + human-learning alone
produce"); §0 ("A real rule 'counts' only if it beats the sham band"); §9.5 ("the sham band
absorbs part of it [RTM]").

**WHY IT'S WRONG:** A sham has a real trigger but scrambled/irrelevant guidance and is
**never shown to the user**. So the sham's Δ has *no human-learning component* — nobody
internalized the sham lesson. The dominant real-rule confound named in §1.3 ("the human
learns the same lesson… from the same sessions used to mint the rule") is exactly the thing a
never-shown sham cannot reproduce. Beating the sham band therefore proves the real Δ exceeds
**noise + drift + RTM**, NOT "noise + human-learning." §4.e's sentence flatly contradicts the
doc's own locked epistemic ceiling in §1.3. Separately: the sham's Δ is only meaningful if
shams inherit the *install-after-spike selection* of real rules — i.e. each sham must be
"installed" right after a spike in its own trigger. The doc never specifies sham install-time
placement, so as written the sham band is centered near zero and any RTM-driven real rule
"beats" it. §9.5's "absorbs part of RTM" is unsupported without that spec.

**FIX:** Delete "human-learning" from the E1 claim; the correct sentence is "exceeds a null
that captures drift + RTM (but NOT the human-learning confound, which is unremovable — §1.3)."
Specify sham install-time placement to mirror real install selection, or the RTM claim is
void. And fix the internal N inconsistency: §9.1 says "≥3 sham draws," §5.3 sets
`skill_placebo_n=20`; a 95th-percentile null needs far more than either — with 20 draws the
"95th percentile" is essentially the max and wildly unstable (see M6).

### B4. B validates only the deterministic *minority* of rules, and the ledger it needs cannot be populated from `mark_installed` as speced
**WHERE:** §4.d/§5.1 (`skill_rule_effect.trigger_signature` "written in `mark_installed`");
§5.3 ("emit the `trigger_signature` (already have the signature internally)"); §4.a
("for LLM-distilled prose rules, back-attribute to the seeding candidate's
`evidence_session_ids` and jackknife *those*"); footnote §2.2 (B "restricted to
machine-checkable triggers").

**WHY IT'S WRONG (three compounding buildability facts, verified in code):**
1. `SkillRule` (schema.py) has **no** `trigger_signature` field — only prose `trigger: str`.
   `error_signature()` lives on the *candidate* (turns.py), and distill collapses candidates
   into prose rules. So the machine-checkable signature is gone by the time a rule exists.
2. `mark_installed(conn, rules)` receives only `SkillRule` objects — no candidate pool, no
   signature. It **cannot** write `skill_rule_effect.trigger_signature` without new plumbing
   that threads a signature field onto `SkillRule` and carries it through distill+merge. The
   "(already have the signature internally)" hand-wave is false at the install seam.
3. Distilled prose rules — the *main output of the pipeline, the whole point of the single
   distill call* — have no machine-checkable signature at all, so by the doc's own restriction
   they get `relevance: unknown` and are **excluded from B**. B (the PRIMARY signal) therefore
   only covers env-signature/rejection-derived rules — i.e. exactly the deterministic-1-A
   minority. The doc never states plainly that its primary validation does not touch the
   distilled majority.
4. §4.a's fallback ("jackknife the distilled rule's `evidence_session_ids`") contradicts §5.2
   and the code: `evidence_session_ids` are stored as anonymized aliases (`case-01`…, verified
   in distill.py `_candidate_aliases`), real ids not recoverable from the stored rule. §5.2
   itself says evidence_json is "**not** repurposed as a forward link." So distilled rules can
   be neither B-attributed nor jackknifed as written.

**FIX:** State up front and in the phasing that **B/E2 cover only machine-checkable
(synthetic/1-A) rules; distilled prose rules are unvalidated by this layer** — that is a
scope truth, not a footnote. If distilled-rule coverage is wanted, persist the candidate's
`trigger_signature` and real (pre-alias) `evidence_session_ids` on the rule at candidate time
(new schema + plumbing), and reconcile §4.a with §5.2. Otherwise drop the §4.a distilled-rule
jackknife bullet — it is unbuildable against the current store.

---

## MAJOR

### M5. The exposure gap ("installed" ≠ "in context") is entirely unaddressed — and the corpus is multi-agent
**WHERE:** whole of §4.d; the dropped options-brief **E1 exposure-gating**.

**WHY:** B counts sessions "after `installed_at`" as the treated cohort. But a rule in
`SKILL.md`/`AGENTS.md` is not proof the agent loaded, read, or attended to it. ClawJournal
scans *many* agents (Claude, Codex, Gemini, opencode, Kimi, Cursor, Copilot, aider…); the
`clawjournal-lessons` skill is only ever in context for some of them. So the after-install
cohort is polluted with sessions the rule *could not* have influenced (wrong agent, skill not
loaded), biasing Δ toward null-or-noise and making the sham comparison meaningless. The
options brief calls exposure-gating "the highest-value single addition to B" and notes it is
closeable locally (grep the later transcript for the skill-load / guidance tokens). Dropping
it guts B.

**FIX:** Adopt exposure-gating: restrict B's treated cohort to sessions where the skill was
*provably loaded* (transcript evidence), and to agents that consume the skill at all. State
the residual caveat (presence ≠ attended-to). At minimum, filter the cohort by agent source.

### M6. Multiple comparisons across ~10 rules is unhandled — guaranteed false "validated" verdicts
**WHERE:** §4.e ("counts only if… > 95th percentile of sham Δ"); absent from §9.

**WHY:** Up to `MAX_INSTALLED_RULES=10` rules, each tested per run against a per-rule 95th
percentile band, yields family-wise false-positive ≈ 1−0.95¹⁰ ≈ 40% *per run*, before even
counting rule×failure-mode combinations. Run weekly and you will "validate" noise routinely.
The doc's own hunt list names "multiple-comparisons across rules"; §9 omits it entirely.

**FIX:** Apply an explicit multiplicity correction (Bonferroni/BH-FDR across the rules tested)
or raise the per-rule threshold to hold family-wise error at a stated level, and disclose the
number of simultaneous comparisons on the surface. Add it to §9.

### M7. "E2 jackknife" over distinct-session counts is a no-op beyond n≥4 — it cannot detect the "one dominant session / one afternoon's burst" it claims to
**WHERE:** §4.a ("rejects candidates whose entire support rests on one dominant session or one
afternoon's burst… `min_i support(S \ {si}) ≥ 3`… needs ≥4 real distinct sessions").

**WHY:** Support is a *distinct-session count* (each session contributes exactly 1). Removing
any one session drops the count by exactly 1, so `min_i support(S\{s_i}) = n−1` for every i —
leave-one-out is deterministic and adds nothing over the threshold `n ≥ 4`. There is no
"dominant session" possible in a distinct-session count; concentration within one session is
already collapsed to 1. And "one afternoon's burst" is *multiple* sessions clustered in time —
distinct-session jackknife treats them as independent and passes them, the exact
pseudo-replication the options brief (A, risk 3) warns about. So E2 as speced neither does
what it advertises nor addresses the burst confound; it is a threshold bump wearing a
resampling costume. (The "top session must not carry more than support−3 of the weight" clause
only makes sense against `_decayed_support` floats, which the algorithm doesn't use —
internally inconsistent.)

**FIX:** If the goal is burst-robustness, jackknife over **time buckets (days/projects)**, not
sessions — require support to survive leave-one-day-out or leave-one-project-out. If the goal
is just "≥4 distinct sessions," say that and delete the jackknife framing.

### M8. Relevance predicate is either too coarse (taxonomy) or near-empty (error_signature); the doc doesn't resolve the tension
**WHERE:** §4.b/§4.d/§9.2 ("equality on `error_signature`/tool/file/taxonomy").

**WHY:** Taxonomy is a 12-value enum. "Relevant = same taxonomy" makes a rule relevant to
essentially the entire failure pool for that mode — that is barely narrower than today's
unattributed trend, and reintroduces the coarse-trigger false positives §9.2 claims to avoid.
`error_signature` equality is the genuinely narrow signal, but exact tool-error signatures
recur rarely, so the relevant cohort collapses to n≈1–2 and B goes silent. The doc asserts
"machine-checkable" as if it resolves this, but the two machine-checkable granularities fail
in opposite directions and it never picks one or shows the cohort sizes.

**FIX:** Commit to `error_signature`/tool-family equality as the relevance predicate (not
taxonomy), publish the resulting cohort-N per rule, and treat taxonomy-only rules as
`relevance: unknown` (same bucket as prose). Accept that this makes B silent for most rules —
that is the honest outcome, not a bug to paper over.

### M9. "Matched pre-install baseline" is undefined where the whole signal depends on it
**WHERE:** §4.d step 3, §9.5 ("matched baseline chosen to blunt RTM").

**WHY:** "Matched" is doing all the statistical work and is never defined — matched on time
distance? project? session volume? propensity? Without a definition this is a placeholder, and
(per B2) the one concrete choice offered — "immediately preceding window" — is the worst
possible one.

**FIX:** Specify the matching precisely, and prefer a design (event-study/DiD, M-B1) where the
baseline is not a function of the installation trigger.

### M10. "Embedding cosine" hooks infrastructure that does not exist, and may itself be egress
**WHERE:** §3 diagram ("embedding/lexical"); §4.b ("Embedding cosine… Phase-3/opt-in").

**WHY:** There is no embedding capability anywhere in `skill/` (verified — the only
"embedding" references are SkillAdaptor's Qwen model in the source brief, not ClawJournal). An
embedding model is either a new heavy local dependency or an API call — and if API, it is a
*second egress* that the cost table (§6) does not account for. The doc treats embeddings as a
free Phase-3 knob.

**FIX:** Either name the concrete local embedding provider and add it to the egress/cost
accounting, or drop embeddings entirely and keep the Linker lexical/signature-only. Given the
local-first invariant, dropping them is the honest default.

### M11. C's "held-out evidence" is frequently the empty set on a thin corpus
**WHERE:** §4.e ("Judged only on evidence *held out* from the rule's own seeding sessions").

**WHY:** A rule's entire support is often 3–4 sessions, all of which are its seeding evidence.
Hold those out and there is nothing trigger-relevant left to judge on. C then either has no
evidence (silent) or silently reuses seeding sessions (tautological — the failure mode §4.e
warns about). Cold-start for C is unaddressed while §9.1 handles it only for B/E2.

**FIX:** State that C is inapplicable when no non-seeding trigger-relevant evidence exists, and
gate it on a minimum held-out evidence count; otherwise it must abstain, not guess.

### M12. Options-brief ranking is inverted (A/D-primary → B-primary) on the strength of the B1 overclaim
**WHERE:** §2.2/§2.3 (B = PRIMARY; A "superseded by E2"; D demoted to "grounding booster,
never a gate").

**WHY:** The source analysis ranks the deterministic candidate-side gates **A (held-out
generalization) + D (recovery-delta grounding)** as *Primary* (High correctness, zero egress,
highest buildability) and B as *Phase-2, Medium, confounded*. The doc flips this and centers
everything on B — the confounded observational estimate — justified mainly by the "controls
drift" claim shown false in B1. Meanwhile the cheap, honest, high-buildability work (validate
the *input* to distill) is demoted. This is scope inversion: the most machinery is spent on
the least defensible signal.

**FIX:** Re-justify or re-order. If B stays primary, the rationale must survive the B1
correction (it currently does not). Strongly consider leading with A+D candidate-side gates
(deterministic, uncontested claims) and framing B/E1 as the explicitly-directional secondary
story the source brief describes.

---

## MINOR

- **Outcome numerator is judge opinion, not ground truth.** §4.d treats
  `ai_failure_value_score>=3 OR badge∈(failed,abandoned)` as a clean recurrence binary, but it
  is a 1–5/enum *judge* output — high-variance and confounded, as §1.2 itself concedes for the
  input scores. The measurement side inherits the same noise; say so, and note the placebo
  only absorbs it if judge noise is guidance-independent.
- **"ground-truth localizer"** (§4.b table) overstates a human correction. A human redirect is
  strong evidence of a fault *step*, not ground truth for the fault's *cause* or for what would
  have fixed it. Soften to "strongest available heuristic localizer."
- **Wilson interval assumes iid Bernoulli** (§4.c/§4.d); personal-corpus sessions are
  autocorrelated (same project/day), so Wilson understates the interval and overstates
  confidence. Flag the independence violation or use a clustered estimate.
- **E-label divergence from the source brief** hurts traceability: draft E1=placebo,
  E2=jackknife, E3=currency, E4=dedup, E5=revealed-pref; source brief E1=exposure,
  E2=placebo, E3=event-study, E4=contradiction, E5=accept/reject. Two source signals
  (exposure-gating, event-study) were dropped *and* their labels reused for other things. Add a
  crosswalk and explicitly state what was cut and why (esp. exposure + event-study — see M5,
  B1).
- **Scope size.** 4 new tables + Linker + jackknife + currency + dedup + placebo harness +
  two buffers + retirement gate + 2 CLI subcommands + opt-in C, to deliver a primary signal
  that (per B4) covers a minority of rules and (per B1–B3) cannot claim more than "directional,
  confounded." Consider shipping Phase-1 hygiene + the `skill_rule_effect` ledger only, and
  gating the entire B attribution surface behind a demonstrated-non-null placebo audit before
  building the reporting.

---

## Credit where due (so the rewrite keeps it)
- The floating-denominator diagnosis of the current trend (§0, §4.d step 4) is correct and is
  the one genuine, defensible improvement — keep it, just stop calling it "drift control."
- Keeping C as an explicitly opt-in, disclosed **second** egress with re-threaded isolation
  flags (§4.e/§6) respects the single-egress invariant correctly.
- The soft ℬ (revivable `dropped`) vs hard user-veto (`rejected`) two-buffer distinction, and
  "user veto is supreme," is the right adaptation of SkillOpt's ℬ (which has no human channel).
- Code anchors are real (verified `mark_installed`, aliasing, `_SUPPORT_HALFLIFE_DAYS=30.0`,
  `error_signature`), and the §1.3/§2.4/§9 honesty framing is the correct instinct — the fixes
  above are about making the *mechanisms* live up to that framing, not loosening it.

## The one-line honest move
Where B1–B3 push you toward more machinery to "rescue" B, the honest move is the opposite:
**weaken B's claim to "directional, confounded association among relevance-conditioned,
exposure-gated sessions"** and lead with the deterministic A+D candidate-side gates the source
analysis already ranked first. Building more placebo/attribution scaffolding does not turn an
observational, install-after-spike, seeding-contaminated, exposure-blind estimate into an
efficacy number — and the doc's own §1.3 already knows that.
