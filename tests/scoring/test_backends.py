"""Tests for clawjournal.scoring.backends — shared backend detection and resolution."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from clawjournal.scoring.backends import (
    AgentResult,
    BACKEND_CHOICES,
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_CODEX_MODEL,
    SUPPORTED_BACKENDS,
    _agent_subprocess_env,
    _build_claude_cmd,
    _build_codex_cmd,
    _build_hermes_cmd,
    _build_openclaw_cmd,
    _classify_process_command,
    _detect_current_agent_from_env,
    _detect_current_agent_from_process_tree,
    _get_process_field,
    check_backend_runtime,
    default_model_for_backend,
    detect_available_backend,
    format_codex_runtime_error,
    require_backend_command,
    resolve_backend,
    resolve_model_for_backend,
    run_default_agent_task,
    summarize_process_error,
)


class TestConstants:
    def test_backend_choices_include_auto(self):
        assert BACKEND_CHOICES == ("auto", "claude", "codex", "hermes", "openclaw")

    def test_supported_backends(self):
        assert set(SUPPORTED_BACKENDS) == {"claude", "codex", "hermes", "openclaw"}

    def test_default_backend_models(self):
        assert DEFAULT_CLAUDE_MODEL == "claude-haiku-4-5"
        assert DEFAULT_CODEX_MODEL == "gpt-5.3-codex-spark"
        assert default_model_for_backend("claude") == DEFAULT_CLAUDE_MODEL
        assert default_model_for_backend("codex") == DEFAULT_CODEX_MODEL
        assert default_model_for_backend("hermes") is None
        assert default_model_for_backend("openclaw") is None
        assert resolve_model_for_backend("claude", None) == DEFAULT_CLAUDE_MODEL
        assert resolve_model_for_backend("codex", None) == DEFAULT_CODEX_MODEL
        assert resolve_model_for_backend("claude", "opus") == "opus"


class TestDetection:
    def test_detect_from_env_claude(self):
        assert _detect_current_agent_from_env({"CLAUDECODE": "1"}) == "claude"

    def test_detect_from_env_codex(self):
        assert _detect_current_agent_from_env({"CODEX_THREAD_ID": "t-1"}) == "codex"

    def test_detect_from_env_openclaw(self):
        assert _detect_current_agent_from_env({"OPENCLAW_STATE_DIR": "/tmp"}) == "openclaw"

    def test_detect_from_env_hermes(self):
        assert _detect_current_agent_from_env({"HERMES_HOME": "/tmp/hermes"}) == "hermes"

    def test_detect_from_env_empty(self):
        assert _detect_current_agent_from_env({}) is None

    def test_classify_process_command_claude(self):
        assert _classify_process_command("claude", "") == "claude"
        assert _classify_process_command("", "/usr/local/bin/claude -p") == "claude"

    def test_classify_process_command_codex(self):
        assert _classify_process_command("codex", "") == "codex"
        assert _classify_process_command("", "/opt/homebrew/bin/codex exec") == "codex"

    def test_classify_process_command_openclaw(self):
        assert _classify_process_command("openclaw", "") == "openclaw"

    def test_classify_process_command_hermes(self):
        assert _classify_process_command("hermes", "") == "hermes"

    def test_classify_process_command_unknown(self):
        assert _classify_process_command("bash", "/bin/bash") is None


class TestResolveBackend:
    def test_explicit_backend(self):
        assert resolve_backend("codex", {}) == "codex"
        assert resolve_backend("claude", {}) == "claude"
        assert resolve_backend("hermes", {}) == "hermes"
        assert resolve_backend("openclaw", {}) == "openclaw"

    def test_explicit_unsupported_raises(self):
        with pytest.raises(RuntimeError, match="Unsupported backend"):
            resolve_backend("gemini", {})

    def test_env_override(self):
        env = {"CLAWJOURNAL_SCORER_BACKEND": "openclaw"}
        assert resolve_backend("auto", env) == "openclaw"

    def test_env_override_invalid_raises(self):
        env = {"CLAWJOURNAL_SCORER_BACKEND": "invalid"}
        with pytest.raises(RuntimeError, match="Unsupported CLAWJOURNAL_SCORER_BACKEND"):
            resolve_backend("auto", env)

    def test_auto_detects_from_env(self):
        env = {"CODEX_THREAD_ID": "thread-123"}
        with patch("clawjournal.scoring.backends.shutil.which", return_value="/usr/bin/codex"):
            assert resolve_backend("auto", env) == "codex"


class TestErrorFormatting:
    def test_codex_network_error(self):
        msg = format_codex_runtime_error(1, "failed to lookup address information")
        assert "codex exec" in msg
        assert "host shell" in msg

    def test_codex_auth_error(self):
        msg = format_codex_runtime_error(1, "401 Unauthorized")
        assert "CODEX_API_KEY" in msg
        assert "codex login" in msg

    def test_codex_generic_error(self):
        msg = format_codex_runtime_error(1, "error: something broke")
        assert "codex exited 1" in msg
        assert "something broke" in msg

    def test_summarize_process_error_finds_error_line(self):
        stderr = "info: starting\nerror: connection refused\n"
        assert "connection refused" in summarize_process_error(stderr)

    def test_summarize_process_error_empty(self):
        assert summarize_process_error("") == ""


class TestCheckBackendRuntime:
    def test_is_noop(self):
        assert check_backend_runtime("codex") is None
        assert check_backend_runtime("claude") is None


class TestRequireBackendCommand:
    def test_found(self, monkeypatch):
        monkeypatch.setattr("clawjournal.scoring.backends.shutil.which", lambda cmd: "/usr/bin/" + cmd)
        assert require_backend_command("claude") == "claude"

    def test_missing_raises(self, monkeypatch):
        monkeypatch.setattr("clawjournal.scoring.backends.shutil.which", lambda cmd: None)
        with pytest.raises(RuntimeError, match="CLI not found"):
            require_backend_command("codex")


class TestResolveBackendAutoFallback:
    def test_falls_back_when_detected_agent_cli_is_missing(self, monkeypatch):
        monkeypatch.setattr(
            "clawjournal.scoring.backends.shutil.which",
            lambda cmd: "/usr/bin/claude" if cmd == "claude" else None,
        )
        assert resolve_backend("auto", {"CODEX_THREAD_ID": "thread-123"}) == "claude"

    def test_uses_installed_backend_when_no_agent(self, monkeypatch):
        monkeypatch.setattr("clawjournal.scoring.backends._detect_current_agent_from_process_tree", lambda **kw: None)
        monkeypatch.setattr(
            "clawjournal.scoring.backends.shutil.which",
            lambda cmd: "/usr/bin/hermes" if cmd == "hermes" else None,
        )
        assert resolve_backend("auto", {}) == "hermes"

    def test_raises_when_no_agent_or_installed_backend(self, monkeypatch):
        monkeypatch.setattr("clawjournal.scoring.backends._detect_current_agent_from_process_tree", lambda **kw: None)
        monkeypatch.setattr("clawjournal.scoring.backends.shutil.which", lambda cmd: None)
        with pytest.raises(RuntimeError, match="Could not detect a supported scoring backend"):
            resolve_backend("auto", {})


class TestProcessTreeDetection:
    def test_finds_claude_in_parent(self, monkeypatch):
        def fake_get_field(pid, field):
            if pid == 100 and field == "comm":
                return "claude"
            if pid == 100 and field == "command":
                return "/usr/local/bin/claude -p"
            if pid == 200 and field == "ppid":
                return "100"
            return ""

        monkeypatch.setattr("clawjournal.scoring.backends._get_process_field", fake_get_field)
        assert _detect_current_agent_from_process_tree(pid=200, max_depth=6) == "claude"

    def test_returns_none_at_max_depth(self, monkeypatch):
        def fake_get_field(pid, field):
            if field == "ppid":
                return str(pid + 1)
            return "bash"

        monkeypatch.setattr("clawjournal.scoring.backends._get_process_field", fake_get_field)
        assert _detect_current_agent_from_process_tree(pid=10, max_depth=2) is None

    def test_handles_cycle(self, monkeypatch):
        def fake_get_field(pid, field):
            if field == "ppid":
                return "10"
            return "bash"

        monkeypatch.setattr("clawjournal.scoring.backends._get_process_field", fake_get_field)
        assert _detect_current_agent_from_process_tree(pid=10, max_depth=10) is None


class TestGetProcessField:
    def test_timeout_returns_empty(self, monkeypatch):
        import subprocess
        def fake_run(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="ps", timeout=2)
        monkeypatch.setattr("clawjournal.scoring.backends.subprocess.run", fake_run)
        assert _get_process_field(1234, "comm") == ""

    def test_nonzero_returncode_returns_empty(self, monkeypatch):
        import subprocess
        monkeypatch.setattr(
            "clawjournal.scoring.backends.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
        )
        assert _get_process_field(99999, "comm") == ""


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------


class TestBuildClaudeCmd:
    def test_minimal(self):
        cmd = _build_claude_cmd("claude", system_prompt_file=None, model=None)
        assert cmd == [
            "claude", "-p",
            "--permission-mode", "bypassPermissions",
            "--no-session-persistence",
        ]

    def test_with_model(self):
        cmd = _build_claude_cmd("claude", system_prompt_file=None, model="opus")
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "opus"

    def test_with_existing_system_prompt(self, tmp_path):
        prompt_file = tmp_path / "sys.txt"
        prompt_file.write_text("you are helpful")
        cmd = _build_claude_cmd("claude", system_prompt_file=prompt_file, model=None)
        assert "--system-prompt-file" in cmd
        assert str(prompt_file) in cmd

    def test_nonexistent_system_prompt_raises(self, tmp_path):
        missing = tmp_path / "missing.txt"
        with pytest.raises(FileNotFoundError, match="System prompt file not found"):
            _build_claude_cmd("claude", system_prompt_file=missing, model=None)

    def test_bare_flag(self):
        cmd = _build_claude_cmd("claude", system_prompt_file=None, model=None, bare=True)
        assert "--bare" in cmd

    def test_bare_flag_off_by_default(self):
        cmd = _build_claude_cmd("claude", system_prompt_file=None, model=None)
        assert "--bare" not in cmd


class TestBuildCodexCmd:
    def test_minimal(self, tmp_path):
        cmd = _build_codex_cmd(
            "codex", cwd=tmp_path, model=None, sandbox=None,
            output_schema_path=None, output_file_path=None,
        )
        assert cmd[:2] == ["codex", "exec"]
        assert "-C" in cmd
        assert str(tmp_path) in cmd
        assert "--sandbox" not in cmd

    def test_with_all_options(self, tmp_path):
        schema = tmp_path / "schema.json"
        output = tmp_path / "output.json"
        cmd = _build_codex_cmd(
            "codex", cwd=tmp_path, model="gpt-4", sandbox="read-only",
            output_schema_path=schema, output_file_path=output,
        )
        assert cmd[cmd.index("--sandbox") + 1] == "read-only"
        assert cmd[cmd.index("--model") + 1] == "gpt-4"
        assert cmd[cmd.index("--output-schema") + 1] == str(schema)
        assert cmd[cmd.index("--output-last-message") + 1] == str(output)


class TestBuildOpenclawCmd:
    def test_basic(self):
        cmd = _build_openclaw_cmd("openclaw", message="do stuff", timeout_seconds=60)
        assert cmd == [
            "openclaw", "agent",
            "--message", "do stuff",
            "--local",
            "--json",
            "--timeout", "60",
        ]


class TestBuildHermesCmd:
    def test_basic(self):
        assert _build_hermes_cmd("hermes", message="score this", model=None) == [
            "hermes", "-z", "score this",
        ]

    def test_with_model(self):
        cmd = _build_hermes_cmd("hermes", message="score this", model="nous/hermes")
        assert cmd == ["hermes", "-z", "score this", "--model", "nous/hermes"]


# ---------------------------------------------------------------------------
# run_default_agent_task
# ---------------------------------------------------------------------------


def _stub_subprocess(monkeypatch, *, stdout="", stderr="", returncode=0):
    """Patch subprocess.run to return a canned CompletedProcess."""
    monkeypatch.setattr(
        "clawjournal.scoring.backends.subprocess.run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=a[0] if a else [], returncode=returncode,
            stdout=stdout, stderr=stderr,
        ),
    )


def _stub_which(monkeypatch):
    """Make shutil.which always succeed."""
    monkeypatch.setattr("clawjournal.scoring.backends.shutil.which", lambda cmd: f"/usr/bin/{cmd}")


class TestAgentSubprocessEnv:
    """The spawned agent CLI must not inherit an external ANTHROPIC_API_KEY,
    so it falls back to the user's subscription login (the "default agent")."""

    def test_strips_anthropic_api_key_by_default(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-invalid")
        monkeypatch.delenv("CLAWJOURNAL_KEEP_API_KEY", raising=False)
        env = _agent_subprocess_env()
        assert "ANTHROPIC_API_KEY" not in env
        # unrelated environment is preserved
        assert "PATH" in env

    def test_keep_api_key_opt_out(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-keepme")
        for val in ("1", "true", "yes", "on"):
            monkeypatch.setenv("CLAWJOURNAL_KEEP_API_KEY", val)
            assert _agent_subprocess_env()["ANTHROPIC_API_KEY"] == "sk-ant-keepme"

    def test_keep_api_key_falsey_still_strips(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-invalid")
        monkeypatch.setenv("CLAWJOURNAL_KEEP_API_KEY", "0")
        assert "ANTHROPIC_API_KEY" not in _agent_subprocess_env()

    def test_claude_subprocess_receives_sanitized_env(self, monkeypatch, tmp_path):
        """End-to-end: the claude subprocess gets env without the API key."""
        _stub_which(monkeypatch)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-invalid")
        monkeypatch.delenv("CLAWJOURNAL_KEEP_API_KEY", raising=False)
        captured = {}

        def spy_run(cmd, **kw):
            captured.update(kw)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("clawjournal.scoring.backends.subprocess.run", spy_run)
        run_default_agent_task(backend="claude", cwd=tmp_path, task_prompt="hello")
        assert "env" in captured
        assert "ANTHROPIC_API_KEY" not in captured["env"]


class TestRunDefaultAgentTaskClaude:
    def test_success(self, monkeypatch, tmp_path):
        _stub_which(monkeypatch)
        _stub_subprocess(monkeypatch, stdout='{"result": "ok"}')
        result = run_default_agent_task(
            backend="claude", cwd=tmp_path, task_prompt="score this",
        )
        assert isinstance(result, AgentResult)
        assert result.stdout == '{"result": "ok"}'
        assert result.returncode == 0
        assert result.cwd == tmp_path

    def test_default_model_forwarded(self, monkeypatch, tmp_path):
        _stub_which(monkeypatch)
        captured_cmd = []

        def spy_run(cmd, **kw):
            captured_cmd.extend(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("clawjournal.scoring.backends.subprocess.run", spy_run)
        run_default_agent_task(
            backend="claude", cwd=tmp_path, task_prompt="score this",
        )
        assert captured_cmd[captured_cmd.index("--model") + 1] == DEFAULT_CLAUDE_MODEL

    def test_explicit_model_overrides_default(self, monkeypatch, tmp_path):
        _stub_which(monkeypatch)
        captured_cmd = []

        def spy_run(cmd, **kw):
            captured_cmd.extend(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("clawjournal.scoring.backends.subprocess.run", spy_run)
        run_default_agent_task(
            backend="claude", cwd=tmp_path, task_prompt="score this", model="opus",
        )
        assert captured_cmd[captured_cmd.index("--model") + 1] == "opus"

    def test_nonzero_exit_raises(self, monkeypatch, tmp_path):
        _stub_which(monkeypatch)
        _stub_subprocess(monkeypatch, returncode=1, stderr="error: bad prompt")
        with pytest.raises(RuntimeError, match="claude exited 1"):
            run_default_agent_task(
                backend="claude", cwd=tmp_path, task_prompt="score this",
            )

    def test_timeout_raises(self, monkeypatch, tmp_path):
        _stub_which(monkeypatch)

        def timeout_run(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=30)

        monkeypatch.setattr("clawjournal.scoring.backends.subprocess.run", timeout_run)
        with pytest.raises(RuntimeError, match="Timed out waiting for claude"):
            run_default_agent_task(
                backend="claude", cwd=tmp_path, task_prompt="hi",
                timeout_seconds=30,
            )

    def test_passes_input_and_cwd(self, monkeypatch, tmp_path):
        """Verify task_prompt is passed as stdin input and cwd is forwarded."""
        _stub_which(monkeypatch)
        captured = {}

        def spy_run(cmd, **kw):
            captured.update(kw)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("clawjournal.scoring.backends.subprocess.run", spy_run)
        run_default_agent_task(backend="claude", cwd=tmp_path, task_prompt="hello")
        assert captured["input"] == "hello"
        assert captured["cwd"] == str(tmp_path)


class TestRunDefaultAgentTaskCodex:
    def test_success(self, monkeypatch, tmp_path):
        _stub_which(monkeypatch)
        _stub_subprocess(monkeypatch, stdout="done")
        result = run_default_agent_task(
            backend="codex", cwd=tmp_path, task_prompt="review",
        )
        assert result.stdout == "done"
        assert result.returncode == 0

    def test_default_model_forwarded(self, monkeypatch, tmp_path):
        _stub_which(monkeypatch)
        captured_cmd = []

        def spy_run(cmd, **kw):
            captured_cmd.extend(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("clawjournal.scoring.backends.subprocess.run", spy_run)
        run_default_agent_task(
            backend="codex", cwd=tmp_path, task_prompt="review",
        )
        assert captured_cmd[captured_cmd.index("--model") + 1] == DEFAULT_CODEX_MODEL

    def test_nonzero_exit_raises(self, monkeypatch, tmp_path):
        _stub_which(monkeypatch)
        _stub_subprocess(monkeypatch, returncode=1, stderr="401 Unauthorized")
        with pytest.raises(RuntimeError, match="CODEX_API_KEY"):
            run_default_agent_task(
                backend="codex", cwd=tmp_path, task_prompt="review",
            )

    def test_timeout_raises(self, monkeypatch, tmp_path):
        _stub_which(monkeypatch)

        def timeout_run(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="codex", timeout=60)

        monkeypatch.setattr("clawjournal.scoring.backends.subprocess.run", timeout_run)
        with pytest.raises(RuntimeError, match="Timed out waiting for codex"):
            run_default_agent_task(
                backend="codex", cwd=tmp_path, task_prompt="review",
                timeout_seconds=60,
            )

    def test_writes_output_schema(self, monkeypatch, tmp_path):
        """When codex_output_schema is provided, schema file is written to cwd."""
        _stub_which(monkeypatch)
        _stub_subprocess(monkeypatch, stdout="ok")
        schema = {"type": "object", "properties": {"score": {"type": "number"}}}
        run_default_agent_task(
            backend="codex", cwd=tmp_path, task_prompt="score",
            codex_output_schema=schema,
        )
        schema_path = tmp_path / "output_schema.json"
        assert schema_path.exists()
        assert json.loads(schema_path.read_text()) == schema

    def test_task_prompt_appended_to_cmd(self, monkeypatch, tmp_path):
        """Verify task_prompt is the last positional argument for codex exec."""
        _stub_which(monkeypatch)
        captured_cmd = []

        def spy_run(cmd, **kw):
            captured_cmd.extend(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("clawjournal.scoring.backends.subprocess.run", spy_run)
        run_default_agent_task(backend="codex", cwd=tmp_path, task_prompt="do stuff")
        assert captured_cmd[-1] == "do stuff"

    def test_output_file_path_traversal_rejected(self, monkeypatch, tmp_path):
        _stub_which(monkeypatch)
        with pytest.raises(ValueError, match="plain filename"):
            run_default_agent_task(
                backend="codex", cwd=tmp_path, task_prompt="hi",
                codex_output_file="../etc/passwd",
            )


class TestRunDefaultAgentTaskClaude_Bare:
    def test_bare_flag_forwarded(self, monkeypatch, tmp_path):
        """claude_bare=True adds --bare to the spawned command."""
        _stub_which(monkeypatch)
        captured_cmd = []

        def spy_run(cmd, **kw):
            captured_cmd.extend(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("clawjournal.scoring.backends.subprocess.run", spy_run)
        run_default_agent_task(
            backend="claude", cwd=tmp_path, task_prompt="hi",
            claude_bare=True,
        )
        assert "--bare" in captured_cmd


class TestRunDefaultAgentTaskOpenclaw:
    def test_success(self, monkeypatch, tmp_path):
        _stub_which(monkeypatch)
        _stub_subprocess(monkeypatch, stdout='{"quality": 4}')
        result = run_default_agent_task(
            backend="openclaw", cwd=tmp_path, task_prompt="score",
        )
        assert result.stdout == '{"quality": 4}'
        assert result.returncode == 0

    def test_model_override_rejected(self, monkeypatch, tmp_path):
        _stub_which(monkeypatch)
        _stub_subprocess(monkeypatch)
        with pytest.raises(RuntimeError, match="does not support --model"):
            run_default_agent_task(
                backend="openclaw", cwd=tmp_path, task_prompt="score",
                model="gpt-4",
            )

    def test_timeout_raises(self, monkeypatch, tmp_path):
        _stub_which(monkeypatch)

        def timeout_run(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="openclaw", timeout=130)

        monkeypatch.setattr("clawjournal.scoring.backends.subprocess.run", timeout_run)
        with pytest.raises(RuntimeError, match="Timed out waiting for openclaw"):
            run_default_agent_task(
                backend="openclaw", cwd=tmp_path, task_prompt="score",
                timeout_seconds=120,
            )

    def test_nonzero_exit_raises(self, monkeypatch, tmp_path):
        _stub_which(monkeypatch)
        _stub_subprocess(monkeypatch, returncode=1, stderr="error: failed to connect")
        with pytest.raises(RuntimeError, match="openclaw exited 1"):
            run_default_agent_task(
                backend="openclaw", cwd=tmp_path, task_prompt="score",
            )

    def test_custom_message(self, monkeypatch, tmp_path):
        """When openclaw_message is provided, it's used instead of task_prompt."""
        _stub_which(monkeypatch)
        captured_cmd = []

        def spy_run(cmd, **kw):
            captured_cmd.extend(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("clawjournal.scoring.backends.subprocess.run", spy_run)
        run_default_agent_task(
            backend="openclaw", cwd=tmp_path, task_prompt="fallback",
            openclaw_message="custom msg",
        )
        idx = captured_cmd.index("--message")
        assert captured_cmd[idx + 1] == "custom msg"

    def test_fallback_to_task_prompt(self, monkeypatch, tmp_path):
        """Without openclaw_message, task_prompt is used for --message."""
        _stub_which(monkeypatch)
        captured_cmd = []

        def spy_run(cmd, **kw):
            captured_cmd.extend(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("clawjournal.scoring.backends.subprocess.run", spy_run)
        run_default_agent_task(
            backend="openclaw", cwd=tmp_path, task_prompt="fallback prompt",
        )
        idx = captured_cmd.index("--message")
        assert captured_cmd[idx + 1] == "fallback prompt"


class TestRunDefaultAgentTaskHermes:
    def test_success(self, monkeypatch, tmp_path):
        _stub_which(monkeypatch)
        _stub_subprocess(monkeypatch, stdout='{"quality": 4}')
        result = run_default_agent_task(
            backend="hermes", cwd=tmp_path, task_prompt="score",
        )
        assert result.stdout == '{"quality": 4}'
        assert result.returncode == 0

    def test_nonzero_exit_raises(self, monkeypatch, tmp_path):
        _stub_which(monkeypatch)
        _stub_subprocess(monkeypatch, returncode=1, stderr="error: failed")
        with pytest.raises(RuntimeError, match="hermes exited 1"):
            run_default_agent_task(
                backend="hermes", cwd=tmp_path, task_prompt="score",
            )

    def test_model_override_forwarded(self, monkeypatch, tmp_path):
        _stub_which(monkeypatch)
        captured_cmd = []

        def spy_run(cmd, **kw):
            captured_cmd.extend(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("clawjournal.scoring.backends.subprocess.run", spy_run)
        run_default_agent_task(
            backend="hermes", cwd=tmp_path, task_prompt="score", model="nous/hermes",
        )
        assert captured_cmd == ["hermes", "-z", "score", "--model", "nous/hermes"]


class TestRunDefaultAgentTaskUnsupportedBackend:
    def test_unsupported_raises(self, monkeypatch, tmp_path):
        """If resolve_backend somehow returns an unknown backend, RuntimeError is raised."""
        _stub_which(monkeypatch)
        monkeypatch.setattr("clawjournal.scoring.backends.resolve_backend", lambda backend, **kw: "gemini")
        monkeypatch.setattr("clawjournal.scoring.backends.check_backend_runtime", lambda *a: None)
        monkeypatch.setattr("clawjournal.scoring.backends.require_backend_command", lambda b: "gemini")
        with pytest.raises(RuntimeError, match="Unsupported backend"):
            run_default_agent_task(
                backend="gemini", cwd=tmp_path, task_prompt="hi",
            )
