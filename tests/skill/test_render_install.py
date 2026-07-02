"""Render-time gate (hard-deny + secrets), frontmatter, and atomic install."""

import pytest

from clawjournal.skill import install, render
from clawjournal.skill.schema import SkillRule

META = {"generated_at": "2026-06-30", "window_days": 7, "sources": 9}


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


def test_install_writes_and_overwrites(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    md = render.render_skill_md([_rule()], META)
    p = install.install_claude(md)
    assert p == tmp_path / ".claude" / "skills" / "clawjournal-lessons" / "SKILL.md"
    assert p.read_text().startswith("---\nname: clawjournal-lessons")
    assert install.INTEGRITY_PREFIX in p.read_text()          # self-verifying, no sidecar
    assert not install.claude_skill_hash_path(p).exists()     # legacy sidecar retired
    # weekly re-run overwrites cleanly (atomic)
    install.install_claude(render.render_skill_md([_rule(guidance="updated rule")], META))
    assert "updated rule" in p.read_text()


def test_install_claude_backs_up_external_edit_and_regenerates(tmp_path, monkeypatch):
    # #8: a weekly-regenerated artifact must not brick on an external touch — the edit
    # is preserved in a .bak and the file is regenerated (not a hard refusal).
    monkeypatch.setenv("HOME", str(tmp_path))
    p = install.install_claude(render.render_skill_md([_rule()], META))
    p.write_text(p.read_text() + "\nmanual edit\n", encoding="utf-8")     # external touch (append)
    install.install_claude(render.render_skill_md([_rule(guidance="updated rule")], META))
    assert "updated rule" in p.read_text()                                # regenerated
    bak = p.with_name(p.name + ".local.bak")
    assert bak.exists() and "manual edit" in bak.read_text()              # user's copy preserved


def test_install_claude_refuses_non_managed_existing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = install.claude_skill_path()
    p.parent.mkdir(parents=True)
    p.write_text("custom skill\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="non-ClawJournal"):
        install.install_claude(render.render_skill_md([_rule()], META))


def test_install_claude_backs_up_mid_body_edit(tmp_path, monkeypatch):
    # #2 + #8: a mid-body edit (integrity line intact at the end) is still detected by
    # the embedded hash — and preserved in .bak, not silently overwritten or refused.
    monkeypatch.setenv("HOME", str(tmp_path))
    p = install.install_claude(render.render_skill_md([_rule()], META))
    p.write_text(p.read_text().replace("premature 4x", "REWORDED BY USER"), encoding="utf-8")
    install.install_claude(render.render_skill_md([_rule(guidance="v2")], META))
    assert "v2" in p.read_text()
    bak = p.with_name(p.name + ".local.bak")
    assert bak.exists() and "REWORDED BY USER" in bak.read_text()


def test_install_claude_migrates_pre_integrity_file(tmp_path, monkeypatch):
    # #1: a managed file from before the embedded-hash change (provenance marker, no
    # integrity line, possibly a stale/absent sidecar) must regenerate, not brick.
    monkeypatch.setenv("HOME", str(tmp_path))
    p = install.claude_skill_path()
    p.parent.mkdir(parents=True)
    p.write_text(render.render_skill_md([_rule()], META), encoding="utf-8")  # no integrity line
    install.install_claude(render.render_skill_md([_rule(guidance="v2")], META))
    assert "v2" in p.read_text() and install.INTEGRITY_PREFIX in p.read_text()


def test_install_codex_managed_region_preserves_user_content(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    agents = tmp_path / ".codex" / "AGENTS.md"
    agents.parent.mkdir(parents=True)
    agents.write_text("# My project rules\n\nkeep this\n")
    region = render.render_agents_region([_rule()], META)
    install.install_codex(region)
    text = agents.read_text()
    assert "keep this" in text and install.BEGIN_MARKER in text
    # idempotent: re-install doesn't duplicate the region
    install.install_codex(region)
    assert agents.read_text().count(install.BEGIN_MARKER) == 1
