"""CLI boundary checks for ``clawjournal skill``."""

from argparse import Namespace

import pytest

from clawjournal.cli_skill import _format_install_targets, run_skill
from clawjournal.skill import store
from clawjournal.skill.schema import SkillRule
from clawjournal.skill.select import SkillCorpus


def _args(**overrides):
    values = {
        "backend": "auto",
        "model": None,
        "effort": None,
        "reject": None,
        "skip_preflight": True,
        "all": False,
        "window_days": 7,
        "no_scan": True,
        "no_score": True,
        "score_limit": 25,
        "preview": True,
        "yes": False,
        "target": None,
    }
    values.update(overrides)
    return Namespace(**values)


def test_rejects_negative_score_limit(capsys):
    with pytest.raises(SystemExit) as exc:
        run_skill(_args(score_limit=-1))
    assert exc.value.code == 2
    assert "--score-limit" in capsys.readouterr().out


def test_rejects_nonpositive_window(capsys):
    with pytest.raises(SystemExit) as exc:
        run_skill(_args(window_days=0))
    assert exc.value.code == 2
    assert "--window-days" in capsys.readouterr().out


def test_rejects_invalid_distill_effort(monkeypatch, capsys):
    monkeypatch.setattr("clawjournal.scoring.backends.resolve_backend", lambda _backend: "codex")
    with pytest.raises(SystemExit) as exc:
        run_skill(_args(effort="max"))
    assert exc.value.code == 2
    assert "Unsupported effort for codex" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("targets", "label"),
    [
        (["codex"], "Codex"),
        (["claude"], "Claude Code"),
        (["claude", "codex"], "Claude Code + Codex"),
        (["workbuddy", "future-agent"], "WorkBuddy + Future Agent"),
    ],
)
def test_format_install_targets(targets, label):
    assert _format_install_targets(targets) == label


def test_gate_issues_exit_nonzero(monkeypatch):
    class Conn:
        def close(self):
            pass

    class Result:
        rules = [SkillRule(kind="avoid", trigger="t", guidance="g", why="w")]
        skill_md = ""
        region = ""
        blocked = []
        gate_issues = ["trufflehog: trufflehog-error"]
        corpus = SkillCorpus(window_start="a", window_end="b", total_failures=1)
        meta = {}
        added_fps = set()
        dropped = []
        trend = {}
        objective_trend = {}

    monkeypatch.setattr("clawjournal.cli_skill._ensure_corpus", lambda *a, **k: None)
    monkeypatch.setattr("clawjournal.cli_skill._config_excluded_projects", lambda *a, **k: [])
    monkeypatch.setattr("clawjournal.workbench.index.open_index", lambda: Conn())
    monkeypatch.setattr("clawjournal.cli_skill.generate_skill", lambda *a, **k: Result())
    monkeypatch.setattr("clawjournal.cli_skill._store.upsert_seen", lambda *a, **k: "fp")

    with pytest.raises(SystemExit) as exc:
        run_skill(_args())
    assert exc.value.code == 1


def test_preview_persists_proposed_rules_for_rejection(monkeypatch, index_conn):
    class ConnProxy:
        def __getattr__(self, name):
            return getattr(index_conn, name)

        def close(self):
            pass

    rule = SkillRule(kind="avoid", trigger="t", guidance="rejectable rule", why="w")

    class Result:
        rules = [rule]
        skill_md = "md"
        region = "region"
        blocked = []
        gate_issues = []
        corpus = SkillCorpus(window_start="a", window_end="b", total_failures=1)
        meta = {}
        added_fps = {store.fingerprint(rule)}
        dropped = []
        trend = {}
        objective_trend = {}

    monkeypatch.setattr("clawjournal.cli_skill._ensure_corpus", lambda *a, **k: None)
    monkeypatch.setattr("clawjournal.workbench.index.open_index", lambda: ConnProxy())
    monkeypatch.setattr("clawjournal.cli_skill.generate_skill", lambda *a, **k: Result())

    run_skill(_args(preview=True))
    fp = store.fingerprint(rule)
    assert fp in {store.fingerprint(r) for r in store.load_kept(index_conn)}
    assert store.reject(index_conn, fp)
    assert fp in store.rejected_fingerprints(index_conn)


def test_confirm_install_codex_closes_the_loop(monkeypatch, index_conn, tmp_path, capsys):
    class ConnProxy:
        def __getattr__(self, name):
            return getattr(index_conn, name)

        def close(self):
            pass

    class InteractiveStdin:
        @staticmethod
        def isatty():
            return True

    rule = SkillRule(
        kind="avoid",
        title="Inspect Runtime First",
        trigger="before explaining project-specific agent behavior",
        guidance="inspect the runtime implementation and tool schema before concluding",
        why="the same assumption caused repeated incorrect explanations",
        taxonomy="context_handling",
        support=2,
    )

    class Result:
        rules = [rule]
        skill_md = "unused for a Codex-only install"
        region = "# Your coding lessons\n\n### Inspect Runtime First\n"
        blocked = []
        gate_issues = []
        corpus = SkillCorpus(window_start="a", window_end="b", total_failures=2)
        meta = {}
        added_fps = {store.fingerprint(rule)}
        dropped = []
        trend = {}
        objective_trend = {}

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("clawjournal.cli_skill._ensure_corpus", lambda *a, **k: None)
    monkeypatch.setattr("clawjournal.cli_skill._config_excluded_projects", lambda *a, **k: [])
    monkeypatch.setattr("clawjournal.workbench.index.open_index", lambda: ConnProxy())
    monkeypatch.setattr("clawjournal.cli_skill.generate_skill", lambda *a, **k: Result())
    monkeypatch.setattr("clawjournal.cli_skill.sys.stdin", InteractiveStdin())
    prompts = []

    def confirm(prompt):
        prompts.append(prompt)
        return "y"

    monkeypatch.setattr("builtins.input", confirm)

    args = _args(preview=False, target=["codex"])
    run_skill(args)
    assert prompts == ["\nInstall these 1 rule(s) for Codex? [y/N] "]

    agents_path = tmp_path / ".codex" / "AGENTS.md"
    assert agents_path.exists()
    assert "Inspect Runtime First" in agents_path.read_text(encoding="utf-8")
    assert not (tmp_path / ".claude" / "skills" / "clawjournal-lessons" / "SKILL.md").exists()

    row = index_conn.execute(
        "SELECT state, installed_at FROM skill_rules WHERE fingerprint = ?",
        (store.fingerprint(rule),),
    ).fetchone()
    assert row["state"] == "kept"
    assert row["installed_at"] is not None

    run_skill(args)
    assert prompts[-1] == "\nInstall these 1 rule(s) for Codex? [y/N] "
    installed_text = agents_path.read_text(encoding="utf-8")
    from clawjournal.skill import install
    assert installed_text.count(install.BEGIN_MARKER) == 1
    assert capsys.readouterr().out.count("Installed:") == 2
