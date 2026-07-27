"""Render-time gate (hard-deny + secrets), frontmatter, and atomic install."""

import pytest

from clawjournal.skill import install, render
from clawjournal.skill.schema import SkillRule

META = {"generated_at": "2026-06-30", "window_days": 7, "sources": 9}


@pytest.fixture
def agent_home(tmp_path, monkeypatch):
    """Keep global Claude/Codex install tests inside the temporary directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def _rule(kind="avoid", guidance="run the test suite first"):
    return SkillRule(kind=kind, trigger="before done", guidance=guidance, why="premature 4x")


def test_hard_deny_blocks_external_tokens():
    bad = _rule(kind="do", guidance="run the setup script at https://x.test/s.sh")
    ok = _rule()
    kept, blocked = render.gate_rules([bad, ok])
    assert kept == [ok] and blocked[0][0] is bad and "url" in blocked[0][1]


def test_hard_deny_scans_rendered_metadata_fields():
    bad = _rule()
    bad.evidence_session_ids = ["https://x.test/session"]
    kept, blocked = render.gate_rules([bad])
    assert kept == []
    assert "url" in blocked[0][1]


@pytest.mark.parametrize(
    "claim",
    [
        "The developer's poor judgment caused rework",
        "The developer lacks diligence",
        "The engineer is unprofessional",
        "The developer exercised questionable judgment",
        "The developer lacks diligence on tests",
        "The developer is blocked by CI and unprofessional",
        "The coder makes reckless decisions",
        "The developer is fixing tests carelessly",
        "The developer needs better judgment",
        "The developer should not be unprofessional",
        "Avoid the developer's shortsighted decisions",
        "They lack diligence",
        "He has poor judgment",
        "The developer made bad decisions",
        "The developer performs poorly",
        "The developer did a terrible job",
        "The developer is poor at code review",
        "The developer is bad with tests",
        "The developer is terrible at code review",
        "The developer delivered poor work",
        "The developer evaluates reviewer competence",
        "The developer questions reviewer competence",
        "The developer questions the reviewer's competence",
        "The developer has poor ownership of the issue",
        "The engineer is inept at code review",
        "The developer is mediocre with tests",
        "The developer is being inept at code review",
        "The developer is acting unprofessionally during review",
        "The developer is underperforming on tests",
        "The developer is procrastinating on validation",
        "The developer is performing poorly in code review",
        "The developer is looking careless during review",
        "The developer behaves stupidly",
        "The developer is working lazily",
        "The developer is fixing tests stupidly",
        "The developer fixes tests stupidly",
        "The developer who reviewed the patch is inept",
        "The developer who is reviewing the patch is inept",
        "The developer when reviewing code is inept",
        "The developer, who reviewed the patch, is inept",
        "The developer (who reviewed the patch) is inept",
        "The developer—who reviewed the patch—is inept",
        "The developer [who reviewed the patch] is unprofessional",
        "The developer, (who reviewed the patch), is inept",
        "The developer,\nwho reviewed the patch,\nis unprofessional",
        "The developer that reviewed the patch is inept",
        "The developer whose patch passed is inept",
        "The developer whom the reviewer approved is inept",
        "The developer who reviewed the patch remains inept",
        "The developer who is inept is fixing tests",
        "The developer who was careless is blocked by CI",
        "The developer while behaving unprofessionally is fixing tests",
        "The developer after acting carelessly is testing code",
        "The developer who, while careless, is fixing tests",
        "The developer who, despite being inept, is fixing tests",
        "The developer whom reviewers called unprofessional is fixing tests",
        (
            "The developer who reviewed the patch, checked the tests, inspected the "
            "logs, compared the results, verified the output, reran the suite, and "
            "documented the findings is unprofessional"
        ),
        "The developer who changed code is carefully stupidly reviewing code",
        "The agent carelessly configured the service",
        "The agent recklessly generated the output",
        "The agent reviewed code but carelessly configured the service",
        "The agent reviewed code and carelessly configured the service",
        "The agent either reviewed or carelessly configured the service",
        "The agent reviewed yet carelessly configured the service",
        "The agent reviewed poorly formatted code and carelessly configured the service",
        "The agent inspected badly generated output and recklessly configured the service",
        "The developer who reported the tests is unprofessional",
        "The developers who reported the test are careless",
        "The developer who confirmed the implementation remains unprofessional",
        "The developer who found issues is inept",
        "The developer who noted errors remains careless",
        "The developer, who reported the test, is unprofessional",
        "The developers, who reported the tests, are careless",
        "The developer who reported the test is unprofessional",
        "The developers who reported the tests are careless",
        "The developer, who reviewed the patch and changed the code, is unprofessional",
        "The developers, who reviewed the patch and changed the tests, are careless",
        "The developer, who reviewed the patch and fixed the test, remains careless",
        "After he and she review the tests, they, when tired, are careless",
        "When he reviews tests with her, they, who skip validation, are inept",
        "The developer reviewed tests with the reviewer. They were inept.",
        "He reviewed tests with the developer. They were inept.",
        "The developer discussed tests with the user; they seem inept.",
        "The developer keeps making foolish choices",
        "The developer is making careless decisions",
        "The developer repeatedly makes unsound decisions",
        "The developer chooses badly",
    ],
)
def test_hard_deny_blocks_unsupported_personal_claims(claim):
    bad = _rule(guidance=claim)

    kept, blocked = render.gate_rules([bad])

    assert kept == []
    assert blocked == [(bad, ["unsupported_personal_claim"])]


@pytest.mark.parametrize(
    "guidance",
    [
        "avoid unreliable integration tests by quarantining flaky cases",
        "the coding agent is blocked by CI",
        "the developer lacks database access",
        "the engineer showed weak test coverage",
        "the developer is unable to access the database",
        "the user is missing permissions",
        "the agent is uncertain which command to run",
        "the reviewer is requesting changes",
        "the user is trying to authenticate",
        "the developer is on the main branch",
        "the developer fixes tests that perform poorly",
        "the developer is fixing tests, then rerunning validation",
        "the developer verifies data integrity before migration",
        "the agent checks API capabilities before use",
        "the developer documents component responsibility",
        "the developer fixes badly formatted code",
        "the developer waits while the service behaves badly",
        "the developer reviews bad decisions made by the model",
        "the developer records the reviewer judgment in the report",
        "the report records the judgment by the reviewer",
        "a poor test by the developer caused the CI failure",
        "a weak implementation by the developer failed validation",
        "a questionable patch by the developer required rework",
        "the developer documents component ownership",
        "the developer is fixing code and tests",
        "the developer is reviewing API responses and test results",
        "the developer is fixing code and integration tests",
        "the developer is fixing code and flaky tests",
        "the developer is fixing code and failing tests",
        "the developer is fixing code and tests in CI",
        "the developer is fixing code and unit tests",
        "the developer is reviewing API responses and failing test results",
        "the developer is refactoring code",
        "the developer is resolving merge conflicts",
        "the developer is the code owner",
        "the developer is a project maintainer",
        "the user is the account owner",
        "the agent is a test service",
        "the agent is planning a database migration",
        "you are profiling a slow service",
        "the agent is measuring test coverage",
        "the agent is performing tests",
        "the agent is triaging deployment issues",
        "the agent is carefully reviewing code",
        "the agent is proactively testing code",
        "the agent is systematically validating API responses",
        "the developer is migrating a production database",
        "the developer is bootstrapping a fresh database",
        "the developer is traversing a dependency graph",
        "when integration tests fail, they are flaky until quarantined",
        "the developer has enough time to run the suite",
        "when you have explicit user approval",
        "when you have confirmation from the user",
        "when the developer has consent to publish",
        "the developer who reviewed the patch is fixing tests",
        "the developer when reviewing code is blocked by CI",
        "the developer who reviewed the patch found the service is unreliable",
        "the developer when reviewing code saw that the test is flaky",
        "the developer who reviewed the patch said the model appears incapable of parsing the input",
        "the developer who reviewed the patch reported the tests are unreliable",
        "the developer who reviewed the patch confirmed the implementation is weak",
        "the developer who reviewed the patch explained that the test is flaky",
        "the developer when reviewing code verified that the implementation was weak",
        "the developer who reviewed the patch said this test is flaky",
        "the developer who reviewed the patch reported their tests are unreliable",
        "the developer who reviewed the patch said the test itself is flaky",
        "the developer who reviewed the patch verified the service is unreliable",
        "the developer who reviewed the patch discovered the test is flaky",
        "the developer who reviews the patch determines that the service is unreliable",
        "the developer who checks the tests concludes that the implementation is weak",
        "the developer who reported the data are unreliable",
        "when integration tests fail, they when retried are flaky",
        "when integration tests fail, they which run under CI are flaky",
        "integration tests fail. They when retried are flaky",
        "when integration tests fail for the developer, they, when retried, are flaky",
        "the developer marked the integration tests as failed, and they, when retried, are flaky",
        "the developer reviews poorly formatted code",
        "the developer inspects badly configured services",
        "the agent reviews the poorly formatted code",
        "the agent reviewed a badly configured service",
        "the agent is reviewing our poorly generated output",
        "the agent checks results from poorly configured services",
    ],
)
def test_hard_deny_allows_technical_trait_wording(guidance):
    safe = _rule(kind="do", guidance=guidance)

    assert render.gate_rules([safe]) == ([safe], [])


@pytest.mark.parametrize(
    "rule",
    [
        SkillRule(
            kind="avoid",
            title="Pair Flags With Fixes",
            trigger=(
                "You detect inaccuracies or problems in a user-supplied plan, spec, "
                "or cleanup proposal."
            ),
            guidance=(
                "Don't just flag the issues and wait; in the same turn attach a concrete "
                "correction or proposed next step for each problem so the loop isn't left "
                "in an unresolved state depending on a user reply that may never come."
            ),
            why=(
                "The agent correctly diagnosed inaccuracies in a proposed cleanup plan "
                "but offered no fix, leaving an unresolved state the user never returned to."
            ),
        ),
        SkillRule(
            kind="do",
            title="Fix Root Cause",
            trigger=(
                "When a manual or temporary workaround makes a bug's symptom disappear "
                "(e.g., chmod on a mounted volume, patching runtime state)"
            ),
            guidance=(
                "Distinguish diagnostic workarounds from permanent fixes: locate and fix "
                "the defect at its source layer, then re-verify with the same harness."
            ),
            why=(
                "Agent verified a volume-permission workaround but had to be told to fix "
                "it properly at image-build."
            ),
        ),
        SkillRule(
            kind="avoid",
            title="Close With Verdict",
            trigger=(
                "When asked to review a PR and you have finished the investigation steps"
            ),
            guidance=(
                "Don't end on test logs; synthesize a final answer to the exact question."
            ),
            why=(
                "Validation traces ended without a recommendation, leaving reviews "
                "unusable for the decision."
            ),
        ),
    ],
    ids=["user-reply", "operational-adverbs", "completed-technical-action"],
)
def test_hard_deny_preserves_existing_operational_lessons(rule):
    assert render.gate_rules([rule]) == ([rule], [])


@pytest.mark.parametrize("adverb", ["hurriedly", "hastily", "thoughtlessly", "blindly"])
def test_negative_task_adverbs_remain_personal_evaluations(adverb):
    unsafe = _rule(
        guidance=f"The agent fixed it properly but {adverb} changed the config."
    )

    assert render.gate_rules([unsafe]) == (
        [],
        [(unsafe, ["unsupported_personal_claim"])],
    )


def test_fallback_title_uses_full_guidance_context_for_policy():
    from clawjournal.skill.schema import parse_rules

    safe, unsafe = parse_rules({"rules": [
        {
            "kind": "do",
            "trigger": "when reviewing coverage",
            "guidance": "the engineer showed weak test coverage",
            "why": "the report omitted an edge case",
        },
        {
            "kind": "avoid",
            "trigger": "before review",
            "guidance": "the developer lacks diligence on tests",
            "why": "the review required rework",
        },
    ]})

    assert safe.title == "the engineer showed weak"
    assert render.gate_rules([safe, unsafe]) == (
        [safe],
        [(unsafe, ["unsupported_personal_claim"])],
    )


def test_render_frontmatter_and_sections():
    md = render.render_skill_md([_rule(), _rule(kind="do", guidance="read source first")], META)
    assert md.startswith("---\nname: clawjournal-lessons")
    assert "## Avoid" in md and "## Do" in md
    assert "<!-- clawjournal-lessons:" in md


def test_gate_secret_pii_per_rule_drops_only_the_dirty_rule(monkeypatch):
    # #1: a secret in one rule must drop THAT rule, not dead-end the whole install.
    monkeypatch.setenv("CLAWJOURNAL_SKIP_TRUFFLEHOG", "1")
    clean = _rule(kind="do", guidance="run the test suite before merging")
    dirty = _rule(kind="do", guidance="set key=AKIAIOSFODNN7EXAMPLE in the env")
    kept, blocked = render.gate_secret_pii_per_rule([clean, dirty])
    assert [r.guidance for r in kept] == ["run the test suite before merging"]
    assert len(blocked) == 1 and blocked[0][0] is dirty


def test_gate_rendered_catches_planted_secret(monkeypatch):
    monkeypatch.setenv("CLAWJOURNAL_SKIP_TRUFFLEHOG", "1")  # autouse already does; explicit here
    assert render.gate_rendered("nothing sensitive here, run tests") == []
    assert render.gate_rendered("key=AKIAIOSFODNN7EXAMPLE")  # secrets gate fires


def test_gate_rendered_catches_planted_pii(monkeypatch):
    monkeypatch.setenv("CLAWJOURNAL_SKIP_TRUFFLEHOG", "1")
    monkeypatch.setattr(render.secrets, "scan_text", lambda text: [])
    issues = render.gate_rendered("contact person@example.com")
    assert any(issue.startswith("pii:") for issue in issues)


def test_gate_rendered_blocks_trufflehog_scan_errors(monkeypatch):
    class Report:
        blocking = True
        block_reason = "trufflehog-error"
        findings = []

    monkeypatch.delenv("CLAWJOURNAL_SKIP_TRUFFLEHOG", raising=False)
    monkeypatch.setattr(render.trufflehog, "is_bypassed", lambda: False)
    monkeypatch.setattr(render.trufflehog, "scan_text", lambda text: Report())
    assert render.gate_rendered("ordinary text") == ["trufflehog: trufflehog-error"]


def test_install_writes_and_overwrites(agent_home):
    md = render.render_skill_md([_rule()], META)
    p = install.install_claude(md)
    assert p == agent_home / ".claude" / "skills" / "clawjournal-lessons" / "SKILL.md"
    assert p.read_text().startswith("---\nname: clawjournal-lessons")
    assert install.INTEGRITY_PREFIX in p.read_text()          # self-verifying, no sidecar
    assert not install.claude_skill_hash_path(p).exists()     # legacy sidecar retired
    # weekly re-run overwrites cleanly (atomic)
    install.install_claude(render.render_skill_md([_rule(guidance="updated rule")], META))
    assert "updated rule" in p.read_text()


def test_install_claude_backs_up_external_edit_and_regenerates(agent_home):
    # #8: a weekly-regenerated artifact must not brick on an external touch — the edit
    # is preserved in a .bak and the file is regenerated (not a hard refusal).
    p = install.install_claude(render.render_skill_md([_rule()], META))
    p.write_text(p.read_text() + "\nmanual edit\n", encoding="utf-8")     # external touch (append)
    install.install_claude(render.render_skill_md([_rule(guidance="updated rule")], META))
    assert "updated rule" in p.read_text()                                # regenerated
    bak = p.with_name(p.name + ".local.bak")
    assert bak.exists() and "manual edit" in bak.read_text()              # user's copy preserved


def test_install_claude_refuses_non_managed_existing_file(agent_home):
    p = install.claude_skill_path()
    p.parent.mkdir(parents=True)
    p.write_text("custom skill\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="non-ClawJournal"):
        install.install_claude(render.render_skill_md([_rule()], META))


def test_install_claude_backs_up_mid_body_edit(agent_home):
    # #2 + #8: a mid-body edit (integrity line intact at the end) is still detected by
    # the embedded hash — and preserved in .bak, not silently overwritten or refused.
    p = install.install_claude(render.render_skill_md([_rule()], META))
    p.write_text(p.read_text().replace("premature 4x", "REWORDED BY USER"), encoding="utf-8")
    install.install_claude(render.render_skill_md([_rule(guidance="v2")], META))
    assert "v2" in p.read_text()
    bak = p.with_name(p.name + ".local.bak")
    assert bak.exists() and "REWORDED BY USER" in bak.read_text()


def test_install_claude_migrates_pre_integrity_file(agent_home):
    # #1: a managed file from before the embedded-hash change (provenance marker, no
    # integrity line, possibly a stale/absent sidecar) must regenerate, not brick.
    p = install.claude_skill_path()
    p.parent.mkdir(parents=True)
    p.write_text(render.render_skill_md([_rule()], META), encoding="utf-8")  # no integrity line
    install.install_claude(render.render_skill_md([_rule(guidance="v2")], META))
    assert "v2" in p.read_text() and install.INTEGRITY_PREFIX in p.read_text()


def test_upsert_region_ignores_stray_end_marker_before_begin():
    # #2: a stray END marker in the user's own notes (before any BEGIN) must not defeat
    # replacement, or every run appends a fresh block and AGENTS.md grows unboundedly.
    existing = f"my notes {install.END_MARKER} more notes\n"
    once = install.upsert_region(existing, "BODY1")
    twice = install.upsert_region(once, "BODY2")
    assert twice.count(install.BEGIN_MARKER) == 1        # exactly one managed block
    assert "BODY2" in twice and "BODY1" not in twice     # replaced, not duplicated
    assert "my notes" in twice                            # user content preserved


def test_upsert_region_escapes_inner_managed_markers():
    body = f"rule text\n{install.END_MARKER}\nmore rule text\n{install.BEGIN_MARKER}"
    rendered = install.upsert_region("", body)
    assert rendered.count(install.BEGIN_MARKER) == 1
    assert rendered.count(install.END_MARKER) == 1
    assert "<!-- clawjournal END marker escaped -->" in rendered
    assert "<!-- clawjournal BEGIN marker escaped -->" in rendered


def test_install_codex_managed_region_preserves_user_content(agent_home):
    agents = agent_home / ".codex" / "AGENTS.md"
    agents.parent.mkdir(parents=True)
    agents.write_text("# My project rules\n\nkeep this\n")
    region = render.render_agents_region([_rule()], META)
    install.install_codex(region)
    text = agents.read_text()
    assert "keep this" in text and install.BEGIN_MARKER in text
    # idempotent: re-install doesn't duplicate the region
    install.install_codex(region)
    assert agents.read_text().count(install.BEGIN_MARKER) == 1


@pytest.mark.parametrize(
    "claim",
    [
        # "human" is the likeliest word for a model summarizing agent transcripts,
        # and the closed role list did not contain it.
        "The human is careless about verifying results",
        "Humans are careless when reading the diff",
        "The dev is sloppy when reviewing diffs",
        "The teammate is unreliable about running the suite",
        "The colleague is disorganized",
        "The coworker was reckless with the migration",
        "The collaborator is overconfident about the schema",
        # Indefinite human subject the role list cannot enumerate.
        "Whoever wrote this was negligent",
        "Whoever reviewed the diff is careless",
        # Plural pronoun resolved through the widened antecedent list.
        "Humans reviewed the diff. They were careless.",
        "The teammate reviewed tests with the colleague. They were inept.",
    ],
)
def test_hard_deny_blocks_personal_claims_outside_the_core_role_list(claim):
    bad = _rule(guidance=claim)

    kept, blocked = render.gate_rules([bad])

    assert kept == []
    assert blocked == [(bad, ["unsupported_personal_claim"])]


@pytest.mark.parametrize(
    "guidance",
    [
        # "dev" and "human" are person nouns only in subject position; as modifiers
        # they are ordinary technical vocabulary and must not be gated.
        "the dev server is unreachable during the run",
        "deploy to the dev branch before promoting",
        "unreliable dev server masked the failure",
        "emit human-readable output for the report",
        "human-readable logs were truncated at the cap",
    ],
)
def test_hard_deny_allows_subject_only_role_nouns_used_as_modifiers(guidance):
    safe = _rule(kind="do", guidance=guidance)

    assert render.gate_rules([safe]) == ([safe], [])


@pytest.mark.parametrize(
    "guidance",
    [
        # A subordinate clause closes at its comma; the main instruction after it
        # is not part of the "you are ..." complement.
        "when you are unsure, ask before proceeding",
        "when you are about to rename a symbol, search for callers",
        "if you are blocked, leave the branch untouched and say so",
        "when you are stuck, re-read the failing assertion first",
        # Possessed subjects that are ordinary infrastructure nouns.
        "clear the cache when your cache is stale",
        "re-read the file when your symbol lookup misses",
    ],
)
def test_hard_deny_allows_operational_state_and_infrastructure_lessons(guidance):
    safe = _rule(kind="do", guidance=guidance)

    assert render.gate_rules([safe]) == ([safe], [])


@pytest.mark.parametrize(
    "why",
    [
        # "user"/"agent" modifying a technical head noun is not a person judgment.
        "unreliable user tests masked the failure",
        "unreliable agent responses were retried without a cap",
    ],
)
def test_hard_deny_allows_role_nouns_modifying_a_technical_head(why):
    safe = SkillRule(kind="avoid", trigger="before done", guidance="cap the retries", why=why)

    assert render.gate_rules([safe]) == ([safe], [])


@pytest.mark.parametrize(
    "claim",
    [
        # The clause bound must not become an escape hatch: a judgment that really
        # does follow the comma still gets its own linking match.
        "when the tests pass, the developer is careless",
        "if the build is green, the reviewer is sloppy",
        "the engineer is lazy, so reviews slip",
        # A trailing possessive still evaluates the person, not the artifact.
        "the careless user's patch broke the build",
    ],
)
def test_hard_deny_still_blocks_judgments_after_a_clause_boundary(claim):
    bad = _rule(guidance=claim)

    kept, blocked = render.gate_rules([bad])

    assert kept == []
    assert blocked == [(bad, ["unsupported_personal_claim"])]


@pytest.mark.parametrize(
    "why",
    [
        # The attributive exemption must not become a bypass: a person-trait stays
        # a judgment however technical the following noun is. Only "unreliable"
        # can describe an artifact; a config cannot be negligent.
        "careless developer commits broke the build",
        "sloppy engineer code shipped without review",
        "lazy reviewer comments missed the bug",
        "negligent user configs leaked the token",
        "careless user commits broke the build",
        # And the bare head-noun reading is still an evaluation.
        "the unreliable user broke the build",
    ],
)
def test_hard_deny_blocks_person_traits_before_a_technical_noun(why):
    bad = SkillRule(kind="avoid", trigger="before done", guidance="cap the retries", why=why)

    kept, blocked = render.gate_rules([bad])

    assert kept == []
    assert blocked == [(bad, ["unsupported_personal_claim"])]


@pytest.mark.parametrize(
    "why",
    [
        # "has a history/habit/pattern of ..." claims a standing disposition, so
        # it stays personal however technical the rest is. The component test only
        # asks whether SOME technical noun appears, which a wider noun list makes
        # easy to satisfy — hence the explicit check.
        "the reviewer has a history of ignoring the endpoint",
        "the developer has a habit of skipping the tests",
        "the user has a tendency to force the merge",
        "the engineer has a track record of breaking the build",
    ],
)
def test_hard_deny_blocks_standing_disposition_claims_about_people(why):
    bad = SkillRule(kind="avoid", trigger="before done", guidance="cap the retries", why=why)

    kept, blocked = render.gate_rules([bad])

    assert kept == []
    assert blocked == [(bad, ["unsupported_personal_claim"])]


def test_hard_deny_allows_a_standing_pattern_about_the_coding_agent():
    """Describing what the agent repeatedly does is the whole point of an avoid rule."""
    safe = SkillRule(
        kind="avoid",
        trigger="before reporting a final status",
        guidance="re-run verification last",
        why="the agent has a history of skipping the final verification",
    )

    assert render.gate_rules([safe]) == ([safe], [])


@pytest.mark.parametrize(
    "guidance",
    [
        # Found by adversarially attacking the loosenings above. Each one passed an
        # intermediate version of this gate; they are kept as the standing proof
        # that widening the safe vocabulary did not reopen the deny path.
        #
        # A personal head noun with a technical prepositional phrase hanging off it.
        "The user's memory of the folder layout is unreliable. Confirm paths with a listing first.",
        "The reviewer's grasp of the module is shallow. Do not treat approval as verification.",
        "This developer lacks the attention to detail a rebase demands. Prefer a merge commit.",
        "The user's excuses about the cache led to another outage. Reproduce with it disabled.",
        "The developer's refusal to read the comments caused the regression. Read them first.",
        # A judgment coordinated onto a subordinate clause, after the comma.
        "When CI stays red for a day, the reviewer is waiting, and has a long history of "
        "approving diffs without reading them.",
        "Keep in mind that the reviewer is busy, and has a history of merging without reading.",
        "When you are unsure about a schema change, that is usually overconfidence rather "
        "than a real ambiguity.",
        # A bare state used to evaluate a third party rather than the work in hand.
        "the maintainer is unclear on the basics of this codebase",
        "the reviewer is unsure about nearly every change they approve",
        "the user is unclear about what they want in every single session",
        # Trait + role where the following word is a verb, not a technical head noun.
        "an unreliable user commits secrets and never notices",
        "unreliable users review nothing and approve everything",
    ],
)
def test_hard_deny_survives_adversarial_probes_of_the_safe_vocabulary(guidance):
    bad = _rule(guidance=guidance)

    kept, blocked = render.gate_rules([bad])

    assert kept == []
    assert blocked == [(bad, ["unsupported_personal_claim"])]


@pytest.mark.parametrize(
    "guidance",
    [
        # The distiller can write in the user's own voice; first person was absent
        # from the actor alternation entirely, and "am" from every copula list.
        "I was careless and caused rework",
        "I am sloppy about running tests",
        "I am unreliable at reviewing diffs",
        "when I am incompetent at review, ask for help",
        "I have a history of skipping tests",
        "My poor judgment delayed the fix",
        "My carelessness broke the build",
        "Our carelessness broke the build",
    ],
)
def test_hard_deny_blocks_first_person_personal_claims(guidance):
    bad = _rule(guidance=guidance)

    kept, blocked = render.gate_rules([bad])

    assert kept == []
    assert blocked == [(bad, ["unsupported_personal_claim"])]


@pytest.mark.parametrize(
    "guidance",
    [
        # "I" is matched case-sensitively on purpose: a case-insensitive \bi\b
        # matches the loop variable that appears throughout ordinary lessons.
        "reset i before the loop and re-run the suite",
        "for i in range(n), validate the schema",
        "increment i is wrong when the list is empty",
        # First-person possessives of technical objects are not evaluations.
        "rebase my branch onto main before pushing",
        "my config is stale; regenerate it",
        "check my test suite output before reporting",
        # And a bare state about oneself is as benign as one about "you".
        "when I am unsure, ask before proceeding",
        "when I am blocked, say so and stop",
    ],
)
def test_hard_deny_allows_loop_variables_and_first_person_technical_context(guidance):
    safe = _rule(kind="do", guidance=guidance)

    assert render.gate_rules([safe]) == ([safe], [])
