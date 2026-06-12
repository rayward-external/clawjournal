"""Tests for the managed TruffleHog install (`clawjournal trufflehog install`).

Everything runs offline: the download is served from an in-memory fake
response, the platform key and vendored checksum table are patched per
test, and the post-install `--version` sanity check is stubbed.
"""

import hashlib
import io
import os
import tarfile
import urllib.error
import urllib.request
from argparse import Namespace
from pathlib import Path

import pytest

from clawjournal.redaction import trufflehog, trufflehog_install


def _tar_gz(members: dict[str, bytes]) -> bytes:
    """Build a .tar.gz archive holding ``members`` (name → content)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class _FakeResponse:
    """Minimal stand-in for the urlopen response context manager."""

    def __init__(self, payload: bytes):
        self._stream = io.BytesIO(payload)

    def read(self, n: int = -1) -> bytes:
        return self._stream.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


BINARY_CONTENT = b"#!/bin/sh\necho fake-trufflehog\n"


@pytest.fixture
def install_env(tmp_path, monkeypatch):
    """Isolated CONFIG_DIR + deterministic platform + stubbed verify.

    Returns a dict the test can tweak: ``serve(payload)`` swaps what the
    fake network returns; ``checksum_of(payload)`` pins the vendored
    table to that payload.
    """
    config_dir = tmp_path / ".clawjournal"
    monkeypatch.setattr("clawjournal.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr(trufflehog_install, "platform_key", lambda: "testos_amd64")
    monkeypatch.setattr(
        trufflehog_install, "_verify_installed_binary", lambda path: "3.95.5"
    )

    state = {"payload": b"", "urls": []}

    def fake_urlopen(request, timeout=None):
        state["urls"].append(request.full_url)
        return _FakeResponse(state["payload"])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    def serve(payload: bytes, *, pin_checksum: bool = True):
        state["payload"] = payload
        if pin_checksum:
            monkeypatch.setattr(
                trufflehog_install,
                "_ARCHIVE_SHA256",
                {"testos_amd64": hashlib.sha256(payload).hexdigest()},
            )

    def pin_checksum(payload: bytes):
        monkeypatch.setattr(
            trufflehog_install,
            "_ARCHIVE_SHA256",
            {"testos_amd64": hashlib.sha256(payload).hexdigest()},
        )

    state["serve"] = serve
    state["pin_checksum"] = pin_checksum
    state["config_dir"] = config_dir
    state["target"] = config_dir / "bin" / trufflehog.managed_binary_path().name
    return state


class TestInstall:
    def test_happy_path_installs_executable_binary(self, install_env):
        archive = _tar_gz({"trufflehog": BINARY_CONTENT, "LICENSE": b"AGPL"})
        install_env["serve"](archive)

        result = trufflehog_install.install()

        assert result["status"] == "installed"
        assert result["version"] == "3.95.5"
        target = Path(result["path"])
        assert target == install_env["target"]
        assert target.read_bytes() == BINARY_CONTENT
        if os.name != "nt":
            assert os.access(target, os.X_OK)
        # The pinned release URL was requested.
        assert install_env["urls"] == [
            trufflehog_install.download_url("testos_amd64")
        ]

    def test_no_temp_files_left_behind(self, install_env):
        install_env["serve"](_tar_gz({"trufflehog": BINARY_CONTENT}))
        trufflehog_install.install()
        leftovers = [
            p for p in install_env["target"].parent.iterdir()
            if p.name != install_env["target"].name
        ]
        assert leftovers == []

    def test_checksum_mismatch_fails_closed(self, install_env):
        archive = _tar_gz({"trufflehog": BINARY_CONTENT})
        install_env["serve"](archive)
        # Pin the table to different bytes than what the network serves.
        install_env["pin_checksum"](b"something else entirely")

        result = trufflehog_install.install()

        assert result["status"] == "checksum-mismatch"
        # Remediation guidance rides along for the CLI to print.
        assert "proxy" in result["hint"]
        assert not install_env["target"].exists()
        # Nothing left behind in the bin dir either.
        bin_dir = install_env["target"].parent
        assert not bin_dir.exists() or list(bin_dir.iterdir()) == []

    def test_already_installed_without_force(self, install_env):
        target = install_env["target"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"existing")
        target.chmod(0o755)  # fixture's verify stub reports the pinned version

        result = trufflehog_install.install()

        assert result["status"] == "already-installed"
        assert result["version"] == trufflehog_install.PINNED_VERSION
        assert target.read_bytes() == b"existing"
        assert install_env["urls"] == []  # no network touch

    @pytest.mark.skipif(os.name == "nt", reason="POSIX exec-bit semantics")
    def test_non_executable_existing_file_is_reinstalled(self, install_env):
        # A managed file the share gate would refuse (no exec bit) must not
        # report already-installed — that combination previously claimed
        # success while resolve_binary() ignored the file.
        target = install_env["target"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"existing")
        target.chmod(0o644)
        install_env["serve"](_tar_gz({"trufflehog": BINARY_CONTENT}))

        result = trufflehog_install.install()

        assert result["status"] == "installed"
        assert target.read_bytes() == BINARY_CONTENT
        assert os.access(target, os.X_OK)

    def test_corrupt_executable_existing_file_is_reinstalled(self, install_env, monkeypatch):
        # A truncated/corrupt managed binary that kept its exec bit fails
        # the --version probe; already-installed must NOT be reported (that
        # would exit 0 while the share gate keeps using a broken binary) —
        # the install falls through and repairs it.
        target = install_env["target"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"\x7fELF truncated garbage")
        target.chmod(0o755)
        monkeypatch.setattr(
            trufflehog_install,
            "_verify_installed_binary",
            lambda path: None if Path(path) == target else "3.95.5",
        )
        install_env["serve"](_tar_gz({"trufflehog": BINARY_CONTENT}))

        result = trufflehog_install.install()

        assert result["status"] == "installed"
        assert result["version"] == "3.95.5"
        assert target.read_bytes() == BINARY_CONTENT

    def test_off_pin_existing_binary_is_upgraded(self, install_env, monkeypatch):
        # A managed binary from an older clawjournal pin is upgraded, not
        # reported as already-installed.
        target = install_env["target"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old pinned version")
        target.chmod(0o755)
        monkeypatch.setattr(
            trufflehog_install,
            "_verify_installed_binary",
            lambda path: "3.90.0" if Path(path) == target else "3.95.5",
        )
        install_env["serve"](_tar_gz({"trufflehog": BINARY_CONTENT}))

        result = trufflehog_install.install()

        assert result["status"] == "installed"
        assert result["version"] == "3.95.5"
        assert target.read_bytes() == BINARY_CONTENT

    def test_force_reinstalls(self, install_env):
        target = install_env["target"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"existing")
        install_env["serve"](_tar_gz({"trufflehog": BINARY_CONTENT}))

        result = trufflehog_install.install(force=True)

        assert result["status"] == "installed"
        assert target.read_bytes() == BINARY_CONTENT

    def test_progress_callback_reports_download(self, install_env):
        install_env["serve"](_tar_gz({"trufflehog": BINARY_CONTENT}))
        messages: list[str] = []

        result = trufflehog_install.install(progress=messages.append)

        assert result["status"] == "installed"
        assert messages and "Downloading TruffleHog" in messages[0]

    def test_unsupported_platform(self, install_env, monkeypatch):
        monkeypatch.setattr(trufflehog_install, "platform_key", lambda: None)
        result = trufflehog_install.install()
        assert result["status"] == "unsupported-platform"
        assert "manually" in result["error"]

    def test_download_failure_reported(self, install_env, monkeypatch):
        def boom(request, timeout=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        install_env["pin_checksum"](b"irrelevant")

        result = trufflehog_install.install()

        assert result["status"] == "download-failed"
        assert "proxy" in result["hint"]
        assert not install_env["target"].exists()

    def test_oversized_download_aborts(self, install_env, monkeypatch):
        monkeypatch.setattr(trufflehog_install, "_MAX_DOWNLOAD_BYTES", 10)
        install_env["serve"](b"x" * 64)

        result = trufflehog_install.install()

        assert result["status"] == "download-failed"
        assert "cap" in result["error"]
        assert not install_env["target"].exists()

    def test_archive_without_binary_member(self, install_env):
        install_env["serve"](_tar_gz({"README.md": b"not a binary"}))
        result = trufflehog_install.install()
        assert result["status"] == "archive-invalid"
        assert not install_env["target"].exists()

    def test_archive_with_traversal_member_is_rejected(self, install_env, tmp_path):
        # A member trying to escape the bin dir must never be written; the
        # exact-name match means it is simply not found.
        install_env["serve"](_tar_gz({"../evil": BINARY_CONTENT}))
        result = trufflehog_install.install()
        assert result["status"] == "archive-invalid"
        assert not (install_env["config_dir"] / "evil").exists()
        assert not (tmp_path / "evil").exists()

    def test_corrupt_archive(self, install_env):
        install_env["serve"](b"this is not a tarball")
        result = trufflehog_install.install()
        assert result["status"] == "archive-invalid"
        assert not install_env["target"].exists()

    def test_verify_failure_never_publishes(self, install_env, monkeypatch):
        install_env["serve"](_tar_gz({"trufflehog": BINARY_CONTENT}))
        monkeypatch.setattr(
            trufflehog_install, "_verify_installed_binary", lambda path: None
        )

        result = trufflehog_install.install()

        assert result["status"] == "verify-failed"
        assert not install_env["target"].exists()
        # The staged temp and the downloaded archive are both cleaned up.
        bin_dir = install_env["target"].parent
        assert not bin_dir.exists() or list(bin_dir.iterdir()) == []

    def test_verify_failure_leaves_existing_binary_untouched(self, install_env, monkeypatch):
        # Verification happens BEFORE publish: a bad download (even under
        # --force) must never displace a working managed binary.
        target = install_env["target"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"known good binary")
        target.chmod(0o755)
        install_env["serve"](_tar_gz({"trufflehog": BINARY_CONTENT}))
        monkeypatch.setattr(
            trufflehog_install, "_verify_installed_binary", lambda path: None
        )

        result = trufflehog_install.install(force=True)

        assert result["status"] == "verify-failed"
        assert "left untouched" in result["error"]
        assert target.read_bytes() == b"known good binary"

    def test_replace_failure_is_install_failed_not_archive_invalid(self, install_env):
        # A directory squatting on the target path fails at publish time —
        # a LOCAL error. Reporting it as "archive-invalid" (with the GitHub
        # URL) would misdirect the user at a re-download remediation right
        # after the checksum proved the archive is byte-identical to
        # upstream.
        target = install_env["target"]
        (target / "occupied").mkdir(parents=True)
        install_env["serve"](_tar_gz({"trufflehog": BINARY_CONTENT}))

        result = trufflehog_install.install(force=True)

        assert result["status"] == "install-failed"
        assert "into place" in result["error"]
        assert target.is_dir()  # nothing destroyed
        # Staged temp and archive cleaned; only the squatting dir remains.
        leftovers = [p.name for p in target.parent.iterdir()]
        assert leftovers == [target.name]


class TestInstallNeverRaises:
    """install() must return a status dict on every failure, never raise."""

    def test_bin_dir_blocked_by_regular_file(self, install_env):
        # ~/.clawjournal/bin exists as a *file* → mkdir raises
        # FileExistsError despite exist_ok=True; the backstop converts it.
        install_env["config_dir"].mkdir(parents=True)
        (install_env["config_dir"] / "bin").write_bytes(b"not a directory")
        install_env["serve"](_tar_gz({"trufflehog": BINARY_CONTENT}))

        result = trufflehog_install.install()

        assert result["status"] == "install-failed"
        assert "FileExistsError" in result["error"]

    def test_truncated_chunked_download_is_download_failed(self, install_env, monkeypatch):
        import http.client

        class _TruncatedResponse(_FakeResponse):
            def read(self, n=-1):
                raise http.client.IncompleteRead(b"partial")

        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda request, timeout=None: _TruncatedResponse(b""),
        )
        install_env["pin_checksum"](b"irrelevant")

        result = trufflehog_install.install()

        assert result["status"] == "download-failed"
        assert not install_env["target"].exists()

    def test_truncated_gzip_archive_is_archive_invalid(self, install_env):
        archive = _tar_gz({"trufflehog": BINARY_CONTENT * 50})
        install_env["serve"](archive[: len(archive) // 2])

        result = trufflehog_install.install()

        assert result["status"] == "archive-invalid"
        assert not install_env["target"].exists()
        bin_dir = install_env["target"].parent
        assert not bin_dir.exists() or list(bin_dir.iterdir()) == []

    def test_oversized_decompressed_member_is_rejected(self, install_env, monkeypatch):
        monkeypatch.setattr(trufflehog_install, "_MAX_BINARY_BYTES", 4)
        install_env["serve"](_tar_gz({"trufflehog": BINARY_CONTENT}))

        result = trufflehog_install.install()

        assert result["status"] == "archive-invalid"
        assert "size cap" in result["error"]
        assert not install_env["target"].exists()


class TestVerifyInstalledBinary:
    """The real _verify_installed_binary implementation (stubbed elsewhere)."""

    def _run(self, monkeypatch, *, returncode=0, stdout="", stderr="", raises=None):
        import subprocess

        seen = {}

        def fake_run(cmd, **kwargs):
            seen["argv"] = cmd
            seen["env"] = kwargs.get("env")
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = trufflehog_install._verify_installed_binary(Path("/x/trufflehog"))
        return result, seen

    def test_parses_version_banner(self, monkeypatch):
        version, seen = self._run(monkeypatch, stdout="trufflehog 3.95.5\n")
        assert version == "3.95.5"
        assert seen["argv"] == ["/x/trufflehog", "--version"]
        # Scrubbed env, not the parent's (None would inherit API keys).
        assert isinstance(seen["env"], dict)

    def test_version_on_stderr_is_accepted(self, monkeypatch):
        version, _ = self._run(monkeypatch, stderr="trufflehog 3.95.5")
        assert version == "3.95.5"

    def test_nonzero_exit_fails(self, monkeypatch):
        version, _ = self._run(monkeypatch, returncode=1, stdout="trufflehog 3.95.5")
        assert version is None

    def test_unparseable_banner_fails(self, monkeypatch):
        version, _ = self._run(monkeypatch, stdout="something unexpected")
        assert version is None

    def test_timeout_fails(self, monkeypatch):
        import subprocess

        version, _ = self._run(
            monkeypatch, raises=subprocess.TimeoutExpired(cmd="x", timeout=15)
        )
        assert version is None

    def test_exec_failure_fails(self, monkeypatch):
        version, _ = self._run(monkeypatch, raises=OSError("wrong arch"))
        assert version is None


class TestPlatformKey:
    def test_real_platform_resolves_or_is_unsupported(self):
        key = trufflehog_install.platform_key()
        assert key is None or key in trufflehog_install._ARCHIVE_SHA256

    @pytest.mark.parametrize(
        ("sys_platform", "os_name", "machine", "expected"),
        [
            ("darwin", "posix", "arm64", "darwin_arm64"),
            ("darwin", "posix", "x86_64", "darwin_amd64"),
            ("linux", "posix", "x86_64", "linux_amd64"),
            ("linux", "posix", "aarch64", "linux_arm64"),
            ("win32", "nt", "AMD64", "windows_amd64"),
            ("win32", "nt", "ARM64", "windows_arm64"),
            ("linux", "posix", "riscv64", None),
            ("sunos5", "posix", "x86_64", None),
        ],
    )
    def test_os_arch_matrix(self, monkeypatch, sys_platform, os_name, machine, expected):
        import os as os_mod
        import sys as sys_mod

        monkeypatch.setattr(sys_mod, "platform", sys_platform)
        monkeypatch.setattr(os_mod, "name", os_name)
        monkeypatch.setattr(
            trufflehog_install.platform, "machine", lambda: machine
        )
        assert trufflehog_install.platform_key() == expected

    def test_checksum_table_covers_release_matrix(self):
        assert set(trufflehog_install._ARCHIVE_SHA256) == {
            "darwin_amd64", "darwin_arm64",
            "linux_amd64", "linux_arm64",
            "windows_amd64", "windows_arm64",
        }
        for value in trufflehog_install._ARCHIVE_SHA256.values():
            assert len(value) == 64
            int(value, 16)  # valid hex

    def test_download_url_shape(self):
        url = trufflehog_install.download_url("linux_amd64")
        assert url == (
            "https://github.com/trufflesecurity/trufflehog/releases/download/"
            f"v{trufflehog_install.PINNED_VERSION}/"
            f"trufflehog_{trufflehog_install.PINNED_VERSION}_linux_amd64.tar.gz"
        )


class TestBinaryResolution:
    """resolve_binary(): managed install wins over PATH."""

    def test_managed_binary_preferred_over_path(self, tmp_path, monkeypatch):
        config_dir = tmp_path / ".clawjournal"
        monkeypatch.setattr("clawjournal.config.CONFIG_DIR", config_dir)
        managed = trufflehog.managed_binary_path()
        managed.parent.mkdir(parents=True)
        managed.write_bytes(BINARY_CONTENT)
        managed.chmod(0o755)
        monkeypatch.setattr(
            "clawjournal.redaction.trufflehog.shutil.which",
            lambda name: "/usr/local/bin/trufflehog",
        )

        assert trufflehog.resolve_binary() == str(managed)
        assert trufflehog.is_available()

    def test_falls_back_to_path_when_no_managed_binary(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")
        monkeypatch.setattr(
            "clawjournal.redaction.trufflehog.shutil.which",
            lambda name: "/usr/local/bin/trufflehog",
        )
        assert trufflehog.resolve_binary() == "/usr/local/bin/trufflehog"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX exec-bit semantics")
    def test_non_executable_managed_file_is_skipped(self, tmp_path, monkeypatch):
        config_dir = tmp_path / ".clawjournal"
        monkeypatch.setattr("clawjournal.config.CONFIG_DIR", config_dir)
        managed = trufflehog.managed_binary_path()
        managed.parent.mkdir(parents=True)
        managed.write_bytes(BINARY_CONTENT)
        managed.chmod(0o644)
        monkeypatch.setattr(
            "clawjournal.redaction.trufflehog.shutil.which", lambda name: None
        )
        assert trufflehog.resolve_binary() is None
        assert not trufflehog.is_available()

    def test_nothing_resolves(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")
        monkeypatch.setattr(
            "clawjournal.redaction.trufflehog.shutil.which", lambda name: None
        )
        assert trufflehog.resolve_binary() is None

    def test_scan_file_invokes_managed_binary(self, tmp_path, monkeypatch):
        import subprocess

        monkeypatch.delenv("CLAWJOURNAL_SKIP_TRUFFLEHOG", raising=False)
        config_dir = tmp_path / ".clawjournal"
        monkeypatch.setattr("clawjournal.config.CONFIG_DIR", config_dir)
        managed = trufflehog.managed_binary_path()
        managed.parent.mkdir(parents=True)
        managed.write_bytes(BINARY_CONTENT)
        managed.chmod(0o755)

        seen = {}

        def fake_run(cmd, **kwargs):
            seen["argv"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        scan_target = tmp_path / "sessions.jsonl"
        scan_target.write_text("{}\n")

        report = trufflehog.scan_file(scan_target)

        assert not report.blocking
        assert seen["argv"][0] == str(managed)

    def test_install_hint_mentions_managed_install(self):
        assert "clawjournal trufflehog install" in trufflehog.INSTALL_HINT


class TestManagedOffPin:
    """managed_off_pin(): the drift signal between the installed managed
    binary and the source pin (selfupdate moves the pin; nothing
    re-installs the binary)."""

    def _managed(self, tmp_path, monkeypatch):
        config_dir = tmp_path / ".clawjournal"
        monkeypatch.setattr("clawjournal.config.CONFIG_DIR", config_dir)
        managed = trufflehog.managed_binary_path()
        managed.parent.mkdir(parents=True)
        managed.write_bytes(BINARY_CONTENT)
        managed.chmod(0o755)
        return managed

    def test_none_for_path_resolved_binary(self, tmp_path, monkeypatch):
        # No managed copy; PATH provides the binary — its freshness is the
        # package manager's business, never flagged.
        monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")
        monkeypatch.setattr(
            "clawjournal.redaction.trufflehog.shutil.which",
            lambda name: "/usr/local/bin/trufflehog",
        )
        monkeypatch.setattr(
            trufflehog, "engine_fingerprint", lambda: "trufflehog 1.0.0"
        )
        assert trufflehog.managed_off_pin() is None

    def test_none_when_managed_matches_pin(self, tmp_path, monkeypatch):
        self._managed(tmp_path, monkeypatch)
        monkeypatch.setattr(
            trufflehog,
            "engine_fingerprint",
            lambda: f"trufflehog {trufflehog_install.PINNED_VERSION}",
        )
        assert trufflehog.managed_off_pin() is None

    def test_reports_drift_for_off_pin_managed_copy(self, tmp_path, monkeypatch):
        self._managed(tmp_path, monkeypatch)
        monkeypatch.setattr(
            trufflehog, "engine_fingerprint", lambda: "trufflehog 3.90.0"
        )
        assert trufflehog.managed_off_pin() == (
            "3.90.0", trufflehog_install.PINNED_VERSION
        )

    def test_none_when_fingerprint_unparseable(self, tmp_path, monkeypatch):
        self._managed(tmp_path, monkeypatch)
        monkeypatch.setattr(trufflehog, "engine_fingerprint", lambda: "unknown")
        assert trufflehog.managed_off_pin() is None


class TestCliCommand:
    def test_status_json_when_missing_exits_nonzero(self, tmp_path, monkeypatch, capsys):
        import json as json_mod

        from clawjournal.cli import _run_trufflehog_command

        monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")
        monkeypatch.setattr(
            "clawjournal.redaction.trufflehog.shutil.which", lambda name: None
        )
        args = Namespace(trufflehog_command="status", json=True)

        # Exit code matches human mode — a script gets both the JSON on
        # stdout and a non-zero exit when the gate binary is missing.
        with pytest.raises(SystemExit) as excinfo:
            _run_trufflehog_command(args)

        assert excinfo.value.code == 1
        payload = json_mod.loads(capsys.readouterr().out)
        assert payload["available"] is False
        assert payload["resolved_path"] is None
        assert payload["version"] is None
        assert payload["fingerprint"] == "missing"
        assert payload["pinned_version"] == trufflehog_install.PINNED_VERSION

    def test_status_human_when_missing_exits_nonzero(self, tmp_path, monkeypatch, capsys):
        from clawjournal.cli import _run_trufflehog_command

        monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")
        monkeypatch.setattr(
            "clawjournal.redaction.trufflehog.shutil.which", lambda name: None
        )
        args = Namespace(trufflehog_command="status", json=False)

        with pytest.raises(SystemExit) as excinfo:
            _run_trufflehog_command(args)

        assert excinfo.value.code == 1
        assert "clawjournal trufflehog install" in capsys.readouterr().out

    def test_install_dispatch_passes_force_and_exits_zero(self, monkeypatch, capsys):
        from clawjournal import cli

        calls = {}

        def fake_install(*, force=False, progress=None):
            calls["force"] = force
            calls["progress"] = progress
            return {"status": "installed", "path": "/x/trufflehog", "version": "3.95.5"}

        monkeypatch.setattr(
            "clawjournal.redaction.trufflehog_install.install", fake_install
        )
        args = Namespace(trufflehog_command="install", force=True, json=False)

        cli._run_trufflehog_command(args)

        assert calls["force"] is True
        assert calls["progress"] is not None  # human mode streams progress
        assert "Installed TruffleHog 3.95.5" in capsys.readouterr().out

    def test_install_failure_exits_nonzero(self, monkeypatch, capsys):
        from clawjournal import cli

        monkeypatch.setattr(
            "clawjournal.redaction.trufflehog_install.install",
            lambda *, force=False, progress=None: {
                "status": "download-failed",
                "error": "offline",
                "url": "https://example.invalid/archive.tar.gz",
                "hint": "Check your network.",
            },
        )
        args = Namespace(trufflehog_command="install", force=False, json=False)

        with pytest.raises(SystemExit) as excinfo:
            cli._run_trufflehog_command(args)

        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert "download-failed" in out
        # Failure output carries the URL and the remediation hint.
        assert "https://example.invalid/archive.tar.gz" in out
        assert "Check your network." in out

    def _managed_setup(self, tmp_path, monkeypatch):
        config_dir = tmp_path / ".clawjournal"
        monkeypatch.setattr("clawjournal.config.CONFIG_DIR", config_dir)
        managed = trufflehog.managed_binary_path()
        managed.parent.mkdir(parents=True)
        managed.write_bytes(BINARY_CONTENT)
        managed.chmod(0o755)
        return managed

    def test_status_warns_when_managed_copy_off_pin(self, tmp_path, monkeypatch, capsys):
        from clawjournal.cli import _run_trufflehog_command

        self._managed_setup(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "clawjournal.redaction.trufflehog.engine_fingerprint",
            lambda: "trufflehog 3.90.0",
        )
        monkeypatch.setattr("shutil.which", lambda name: None)
        args = Namespace(trufflehog_command="status", json=False)

        # Warn, never block: off-pin status still exits 0.
        _run_trufflehog_command(args)

        out = capsys.readouterr().out
        assert "Warning" in out
        assert "v3.90.0" in out
        assert f"pins v{trufflehog_install.PINNED_VERSION}" in out
        assert "clawjournal trufflehog install" in out

    def test_status_json_reports_off_pin_flag(self, tmp_path, monkeypatch, capsys):
        import json as json_mod

        from clawjournal.cli import _run_trufflehog_command

        self._managed_setup(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "clawjournal.redaction.trufflehog.engine_fingerprint",
            lambda: "trufflehog 3.90.0",
        )
        monkeypatch.setattr("shutil.which", lambda name: None)
        args = Namespace(trufflehog_command="status", json=True)

        _run_trufflehog_command(args)

        payload = json_mod.loads(capsys.readouterr().out)
        assert payload["managed_off_pin"] is True
        assert payload["version"] == "3.90.0"

    def test_status_notes_shadowed_path_binary(self, tmp_path, monkeypatch, capsys):
        from clawjournal.cli import _run_trufflehog_command

        self._managed_setup(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "clawjournal.redaction.trufflehog.engine_fingerprint",
            lambda: f"trufflehog {trufflehog_install.PINNED_VERSION}",
        )
        monkeypatch.setattr("shutil.which", lambda name: "/opt/homebrew/bin/trufflehog")
        args = Namespace(trufflehog_command="status", json=False)

        _run_trufflehog_command(args)

        out = capsys.readouterr().out
        assert "precedence over the PATH copy at /opt/homebrew/bin/trufflehog" in out
        # Pin matched — no drift warning.
        assert "Warning" not in out

    def test_parser_roundtrip_via_main(self, tmp_path, monkeypatch, capsys):
        import sys as sys_mod

        from clawjournal.cli import main

        monkeypatch.setenv("CLAWJOURNAL_NO_AUTO_UPDATE", "1")
        monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")
        monkeypatch.setattr(
            "clawjournal.redaction.trufflehog.shutil.which", lambda name: None
        )
        monkeypatch.setattr(
            sys_mod, "argv", ["clawjournal", "trufflehog", "status", "--json"]
        )

        with pytest.raises(SystemExit) as excinfo:
            main()

        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert '"available": false' in out
