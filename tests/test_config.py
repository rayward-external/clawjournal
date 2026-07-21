"""Tests for clawjournal.config — config persistence."""

import json

import pytest

from clawjournal.config import (
    _migrate_excluded_projects,
    _migrate_findings_engines,
    load_config,
    normalize_excluded_project_names,
    save_config,
)


class TestLoadConfig:
    def test_no_file_returns_defaults(self, tmp_config):
        config = load_config()
        assert config["repo"] is None
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
