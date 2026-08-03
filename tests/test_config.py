"""Tests for clawjournal.config — config persistence."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from clawjournal.config import (
    _auto_upload_profile_projection,
    _migrate_default_source_scope,
    _migrate_excluded_projects,
    _migrate_findings_engines,
    _migrate_remove_auto_upload_ui_flag,
    _resolve_config_dir,
    load_config,
    normalize_excluded_project_names,
    save_config,
)


def test_clawjournal_home_relocates_the_complete_state_root(tmp_path, monkeypatch):
    state_root = tmp_path / "private local state"
    monkeypatch.setenv("CLAWJOURNAL_HOME", str(state_root))

    assert _resolve_config_dir() == state_root.resolve()


def test_clawjournal_home_routes_parser_state_without_moving_agent_sources(tmp_path):
    state_root = tmp_path / "private local state"
    probe = (
        "import json\n"
        "from pathlib import Path\n"
        "from clawjournal.parsing import parser\n"
        "print(json.dumps({\n"
        "    'home': str(Path.home()),\n"
        "    'workbuddy_import': str(parser.WORKBUDDY_IMPORT_DIR),\n"
        "    'custom': str(parser.CUSTOM_DIR),\n"
        "    'claude': str(parser.CLAUDE_DIR),\n"
        "    'codex': str(parser.CODEX_DIR),\n"
        "    'workbuddy': str(parser.WORKBUDDY_DIR),\n"
        "}))\n"
    )
    env = os.environ.copy()
    env["CLAWJOURNAL_HOME"] = str(state_root)
    repo_root = Path(__file__).resolve().parents[1]
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(repo_root)
        if not existing_pythonpath
        else os.pathsep.join((str(repo_root), existing_pythonpath))
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    paths = json.loads(result.stdout)
    home = Path(paths["home"])

    assert Path(paths["workbuddy_import"]) == state_root.resolve() / "workbuddy"
    assert Path(paths["custom"]) == state_root.resolve() / "custom"
    assert Path(paths["claude"]) == home / ".claude"
    assert Path(paths["codex"]) == home / ".codex"
    assert Path(paths["workbuddy"]) == home / "WorkBuddy"


@pytest.mark.parametrize(
    "arguments",
    (
        ("trufflehog", "--help"),
        ("betterleaks", "--help"),
        ("events", "export", "--help"),
    ),
)
def test_clawjournal_home_percent_is_safe_in_argparse_help(tmp_path, arguments):
    state_root = tmp_path / "100%safe-state"
    env = os.environ.copy()
    env["CLAWJOURNAL_HOME"] = str(state_root)
    repo_root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(repo_root), env.get("PYTHONPATH")))
    )

    result = subprocess.run(
        [sys.executable, "-m", "clawjournal.cli", *arguments],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )

    compact_help = "".join(result.stdout.split())
    assert "100%safe-state" in compact_help
    assert "'option_strings'" not in result.stdout


class TestAutoUploadProfileProjection:
    def test_manual_export_scope_does_not_change_recurring_profile(self):
        baseline = _auto_upload_profile_projection({
            "source": "claude",
            "projects_confirmed": True,
            "excluded_projects": [],
        })
        changed_manual_scope = _auto_upload_profile_projection({
            "source": "workbuddy",
            "projects_confirmed": False,
            "excluded_projects": [],
        })

        assert changed_manual_scope == baseline


class TestLoadConfig:
    def test_no_file_returns_defaults(self, tmp_config):
        config = load_config()
        assert config["repo"] is None
        assert config["source"] == "all"
        assert config["excluded_projects"] == []
        assert config["redact_strings"] == []

    def test_valid_file_merged(self, tmp_config):
        tmp_config.parent.mkdir(parents=True, exist_ok=True)
        tmp_config.write_text(json.dumps({"repo": "alice/data", "custom_key": "val"}))
        config = load_config()
        assert config["repo"] == "alice/data"
        assert config["custom_key"] == "val"
        # Defaults still present
        assert "excluded_projects" in config
        assert config["source"] == "all"

    def test_unconfigured_source_migrates_to_all(self, tmp_config):
        tmp_config.parent.mkdir(parents=True, exist_ok=True)
        tmp_config.write_text(json.dumps({"source": None}))

        config = load_config()

        assert config["source"] == "all"
        assert json.loads(tmp_config.read_text())["source"] == "all"

    def test_corrupt_json_returns_defaults(self, tmp_config, capsys):
        tmp_config.parent.mkdir(parents=True, exist_ok=True)
        tmp_config.write_text("not valid json {{{")
        config = load_config()
        assert config["repo"] is None
        captured = capsys.readouterr()
        assert "Warning" in captured.err

    def test_extra_keys_preserved(self, tmp_config):
        tmp_config.parent.mkdir(parents=True, exist_ok=True)
        tmp_config.write_text(json.dumps({"repo": None, "my_extra": [1, 2, 3]}))
        config = load_config()
        assert config["my_extra"] == [1, 2, 3]

    def test_migrates_excluded_projects_on_load(self, tmp_config):
        tmp_config.parent.mkdir(parents=True, exist_ok=True)
        tmp_config.write_text(json.dumps({"excluded_projects": ["myapp", "other"]}))
        config = load_config()
        assert config["excluded_projects"] == ["claude:myapp", "claude:other"]
        # Should have been persisted to disk
        data = json.loads(tmp_config.read_text())
        assert data["excluded_projects"] == ["claude:myapp", "claude:other"]

    def test_migration_skips_already_prefixed(self, tmp_config):
        tmp_config.parent.mkdir(parents=True, exist_ok=True)
        tmp_config.write_text(json.dumps({
            "excluded_projects": ["claude:myapp", "codex:proj", "old-proj"]
        }))
        config = load_config()
        assert config["excluded_projects"] == ["claude:myapp", "codex:proj", "claude:old-proj"]


class TestMigrateExcludedProjects:
    def test_empty_list(self):
        config = {"excluded_projects": []}
        assert _migrate_excluded_projects(config) is False

    def test_no_key(self):
        config = {}
        assert _migrate_excluded_projects(config) is False

    def test_bare_names_get_claude_prefix(self):
        config = {"excluded_projects": ["myapp", "work-repo"]}
        assert _migrate_excluded_projects(config) is True
        assert config["excluded_projects"] == ["claude:myapp", "claude:work-repo"]

    def test_prefixed_names_unchanged(self):
        config = {"excluded_projects": [
            "codex:proj", "gemini:proj", "opencode:proj",
            "openclaw:proj", "kimi:proj", "cline:proj", "custom:proj",
        ]}
        assert _migrate_excluded_projects(config) is False

    def test_mixed(self):
        config = {"excluded_projects": ["claude:already", "bare-name", "gemini:hash"]}
        assert _migrate_excluded_projects(config) is True
        assert config["excluded_projects"] == ["claude:already", "claude:bare-name", "gemini:hash"]


class TestMigrateDefaultSourceScope:
    @pytest.mark.parametrize("source", [None, "", "auto", " AUTO "])
    def test_unconfigured_values_default_to_all(self, source):
        config = {"source": source}

        assert _migrate_default_source_scope(config) is True
        assert config["source"] == "all"

    @pytest.mark.parametrize("source", ["all", "claude", "codex", "workbuddy"])
    def test_explicit_scope_is_preserved(self, source):
        config = {"source": source}

        assert _migrate_default_source_scope(config) is False
        assert config["source"] == source


class TestNormalizeExcludedProjectNames:
    def test_bare_names_get_claude_prefix(self):
        assert normalize_excluded_project_names(["myapp", "work-repo"]) == [
            "claude:myapp",
            "claude:work-repo",
        ]

    def test_prefixed_names_stay_as_is(self):
        assert normalize_excluded_project_names(["codex:proj", "custom:data"]) == [
            "codex:proj",
            "custom:data",
        ]


class TestSaveConfig:
    def test_creates_dir_and_writes(self, tmp_config):
        save_config({"repo": "alice/data", "excluded_projects": []})
        assert tmp_config.exists()
        data = json.loads(tmp_config.read_text())
        assert data["repo"] == "alice/data"

    def test_overwrites_existing(self, tmp_config):
        tmp_config.parent.mkdir(parents=True, exist_ok=True)
        tmp_config.write_text(json.dumps({"repo": "old"}))
        save_config({"repo": "new"})
        data = json.loads(tmp_config.read_text())
        assert data["repo"] == "new"

    def test_oserror_prints_warning(self, tmp_config, monkeypatch, capsys):
        # Make the directory unwritable
        monkeypatch.setattr(
            "clawjournal.config.CONFIG_DIR",
            tmp_config.parent / "nonexistent" / "deep" / "dir",
        )
        # Actually mock mkdir to raise
        import clawjournal.config as config_mod
        original_mkdir = type(tmp_config.parent).mkdir

        def failing_mkdir(self, *a, **kw):
            raise OSError("Permission denied")

        monkeypatch.setattr(type(tmp_config.parent), "mkdir", failing_mkdir)
        result = save_config({"repo": "test"})
        captured = capsys.readouterr()
        assert result is False
        assert "Warning" in captured.err


class TestMigrateFindingsEngines:
    def test_missing_key_rides_the_default(self):
        # No explicit list → get_enabled_engines' default applies; the
        # migration must not materialize the key.
        config = {}
        assert _migrate_findings_engines(config) is False
        assert "enabled_findings_engines" not in config

    def test_explicit_list_gains_betterleaks(self):
        config = {"enabled_findings_engines": ["regex_secrets", "trufflehog"]}
        assert _migrate_findings_engines(config) is True
        assert config["enabled_findings_engines"] == [
            "regex_secrets", "trufflehog", "betterleaks",
        ]

    def test_already_present_is_untouched(self):
        config = {"enabled_findings_engines": ["betterleaks", "regex_pii"]}
        assert _migrate_findings_engines(config) is False

    def test_malformed_values_are_left_alone(self):
        assert _migrate_findings_engines({"enabled_findings_engines": "nope"}) is False
        assert _migrate_findings_engines({"enabled_findings_engines": [1, 2]}) is False

    def test_load_config_persists_the_migration(self, tmp_config):
        tmp_config.parent.mkdir(parents=True, exist_ok=True)
        tmp_config.write_text(json.dumps({
            "enabled_findings_engines": ["regex_secrets", "regex_pii"],
        }))
        config = load_config()
        assert "betterleaks" in config["enabled_findings_engines"]
        data = json.loads(tmp_config.read_text())
        assert "betterleaks" in data["enabled_findings_engines"]


class TestRemoveAutoUploadUiFlag:
    def test_missing_key_is_unchanged(self):
        config = {}
        assert _migrate_remove_auto_upload_ui_flag(config) is False

    def test_retired_rollout_flag_is_removed(self):
        config = {"auto_upload_ui_enabled": False}
        assert _migrate_remove_auto_upload_ui_flag(config) is True
        assert "auto_upload_ui_enabled" not in config

    def test_load_config_persists_removal(self, tmp_config):
        tmp_config.parent.mkdir(parents=True, exist_ok=True)
        tmp_config.write_text(json.dumps({"auto_upload_ui_enabled": True}))

        config = load_config()

        assert "auto_upload_ui_enabled" not in config
        assert "auto_upload_ui_enabled" not in json.loads(tmp_config.read_text())
