"""Tests for the managed Betterleaks install (`clawjournal betterleaks install`).

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
import zipfile
from argparse import Namespace
from pathlib import Path

import pytest

from clawjournal.redaction import betterleaks, betterleaks_install


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


def _zip(members: dict[str, bytes]) -> bytes:
    """Build a .zip archive holding ``members`` (name → content)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
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


BINARY_CONTENT = b"#!/bin/sh\necho fake-betterleaks\n"

PINNED = betterleaks_install.PINNED_VERSION


@pytest.fixture
def install_env(tmp_path, monkeypatch):
    """Isolated CONFIG_DIR + deterministic platform + stubbed verify.

    Returns a dict the test can tweak: ``serve(payload)`` swaps what the
    fake network returns; ``pin_checksum(payload)`` pins the vendored
    table to that payload; ``set_platform(key)`` switches the archive
    flavor (zip for windows keys).
    """
    config_dir = tmp_path / ".clawjournal"
    monkeypatch.setattr("clawjournal.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr(betterleaks_install, "platform_key", lambda: "testos_x64")
    monkeypatch.setattr(
        betterleaks_install, "_verify_installed_binary", lambda path: PINNED
    )

    state = {"payload": b"", "urls": [], "key": "testos_x64"}

    def fake_urlopen(request, timeout=None):
        state["urls"].append(request.full_url)
        return _FakeResponse(state["payload"])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    def set_platform(key: str):
        state["key"] = key
        monkeypatch.setattr(betterleaks_install, "platform_key", lambda: key)

    def pin_checksum(payload: bytes):
        monkeypatch.setattr(
            betterleaks_install,
            "_ARCHIVE_SHA256",
            {state["key"]: hashlib.sha256(payload).hexdigest()},
        )

    def serve(payload: bytes, *, pin: bool = True):
        state["payload"] = payload
        if pin:
            pin_checksum(payload)

    state["serve"] = serve
    state["pin_checksum"] = pin_checksum
    state["set_platform"] = set_platform
    state["config_dir"] = config_dir
    state["target"] = config_dir / "bin" / betterleaks.managed_binary_path().name
    return state


class TestInstall:
    def test_happy_path_installs_executable_binary(self, install_env):
        archive = _tar_gz({"betterleaks": BINARY_CONTENT, "LICENSE": b"MIT"})
        install_env["serve"](archive)

        result = betterleaks_install.install()

        assert result["status"] == "installed"
        assert result["version"] == PINNED
        target = Path(result["path"])
        assert target == install_env["target"]
        assert target.read_bytes() == BINARY_CONTENT
        if os.name != "nt":
            assert os.access(target, os.X_OK)
        # The pinned release URL was requested.
        assert install_env["urls"] == [betterleaks_install.download_url("testos_x64")]

    def test_zip_archive_for_windows_platform_key(self, install_env):
        # Upstream ships .zip on Windows; the extractor must handle both
        # flavors. (Member name stays this host's binary name — the flavor
        # switch is what's under test.)
        install_env["set_platform"]("windows_x64")
        binary_name = install_env["target"].name
        archive = _zip({binary_name: BINARY_CONTENT, "README.md": b"readme"})
        install_env["serve"](archive)

        result = betterleaks_install.install()

        assert result["status"] == "installed"
        assert install_env["target"].read_bytes() == BINARY_CONTENT
        assert install_env["urls"] == [betterleaks_install.download_url("windows_x64")]
        assert install_env["urls"][0].endswith(".zip")

    def test_zip_without_binary_member_is_archive_invalid(self, install_env):
        install_env["set_platform"]("windows_x64")
        install_env["serve"](_zip({"README.md": b"not a binary"}))
        result = betterleaks_install.install()
        assert result["status"] == "archive-invalid"
        assert not install_env["target"].exists()

    def test_corrupt_zip_is_archive_invalid(self, install_env):
        install_env["set_platform"]("windows_x64")
        install_env["serve"](b"this is not a zip file")
        result = betterleaks_install.install()
        assert result["status"] == "archive-invalid"
        assert not install_env["target"].exists()

    def test_no_temp_files_left_behind(self, install_env):
        install_env["serve"](_tar_gz({"betterleaks": BINARY_CONTENT}))
        betterleaks_install.install()
        leftovers = [
            p for p in install_env["target"].parent.iterdir()
            if p.name != install_env["target"].name
        ]
        assert leftovers == []

    def test_checksum_mismatch_fails_closed(self, install_env):
        archive = _tar_gz({"betterleaks": BINARY_CONTENT})
        install_env["serve"](archive)
        # Pin the table to different bytes than what the network serves.
        install_env["pin_checksum"](b"something else entirely")

        result = betterleaks_install.install()

        assert result["status"] == "checksum-mismatch"
        # Remediation guidance rides along for the CLI to print.
        assert "proxy" in result["hint"]
        assert not install_env["target"].exists()
        bin_dir = install_env["target"].parent
        assert not bin_dir.exists() or list(bin_dir.iterdir()) == []

    def test_already_installed_without_force(self, install_env):
        target = install_env["target"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"existing")
        target.chmod(0o755)  # fixture's verify stub reports the pinned version

        result = betterleaks_install.install()

        assert result["status"] == "already-installed"
        assert result["version"] == PINNED
        assert target.read_bytes() == b"existing"
        assert install_env["urls"] == []  # no network touch

    def test_off_pin_existing_binary_is_upgraded(self, install_env, monkeypatch):
        target = install_env["target"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old pinned version")
        target.chmod(0o755)
        monkeypatch.setattr(
            betterleaks_install,
            "_verify_installed_binary",
            lambda path: "1.0.0" if Path(path) == target else PINNED,
        )
        install_env["serve"](_tar_gz({"betterleaks": BINARY_CONTENT}))

        result = betterleaks_install.install()

        assert result["status"] == "installed"
        assert result["version"] == PINNED
        assert target.read_bytes() == BINARY_CONTENT

    def test_force_reinstalls(self, install_env):
        target = install_env["target"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"existing")
        install_env["serve"](_tar_gz({"betterleaks": BINARY_CONTENT}))

        result = betterleaks_install.install(force=True)

        assert result["status"] == "installed"
        assert target.read_bytes() == BINARY_CONTENT

    def test_unsupported_platform(self, install_env, monkeypatch):
        monkeypatch.setattr(betterleaks_install, "platform_key", lambda: None)
        result = betterleaks_install.install()
        assert result["status"] == "unsupported-platform"
        assert "manually" in result["error"]

    def test_download_failure_reported(self, install_env, monkeypatch):
        def boom(request, timeout=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        install_env["pin_checksum"](b"irrelevant")

        result = betterleaks_install.install()

        assert result["status"] == "download-failed"
        assert "proxy" in result["hint"]
        assert not install_env["target"].exists()

    def test_archive_without_binary_member(self, install_env):
        install_env["serve"](_tar_gz({"README.md": b"not a binary"}))
        result = betterleaks_install.install()
        assert result["status"] == "archive-invalid"
        assert not install_env["target"].exists()

    def test_archive_with_traversal_member_is_rejected(self, install_env, tmp_path):
        # A member trying to escape the bin dir must never be written; the
        # exact-name match means it is simply not found.
        install_env["serve"](_tar_gz({"../evil": BINARY_CONTENT}))
        result = betterleaks_install.install()
        assert result["status"] == "archive-invalid"
        assert not (install_env["config_dir"] / "evil").exists()
        assert not (tmp_path / "evil").exists()

    def test_verify_failure_never_publishes(self, install_env, monkeypatch):
        install_env["serve"](_tar_gz({"betterleaks": BINARY_CONTENT}))
        monkeypatch.setattr(
            betterleaks_install, "_verify_installed_binary", lambda path: None
        )

        result = betterleaks_install.install()

        assert result["status"] == "verify-failed"
        assert not install_env["target"].exists()
        bin_dir = install_env["target"].parent
        assert not bin_dir.exists() or list(bin_dir.iterdir()) == []

    def test_verify_failure_leaves_existing_binary_untouched(self, install_env, monkeypatch):
        # Verification happens BEFORE publish: a bad download (even under
        # --force) must never displace a working managed binary.
        target = install_env["target"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"known good binary")
        target.chmod(0o755)
        install_env["serve"](_tar_gz({"betterleaks": BINARY_CONTENT}))
        monkeypatch.setattr(
            betterleaks_install, "_verify_installed_binary", lambda path: None
        )

        result = betterleaks_install.install(force=True)

        assert result["status"] == "verify-failed"
        assert "left untouched" in result["error"]
        assert target.read_bytes() == b"known good binary"

    def test_oversized_decompressed_member_is_rejected(self, install_env, monkeypatch):
        monkeypatch.setattr(betterleaks_install, "_MAX_BINARY_BYTES", 4)
        install_env["serve"](_tar_gz({"betterleaks": BINARY_CONTENT}))

        result = betterleaks_install.install()

        assert result["status"] == "archive-invalid"
        assert "size cap" in result["error"]
        assert not install_env["target"].exists()

    def test_install_never_raises_on_blocked_bin_dir(self, install_env):
        # ~/.clawjournal/bin exists as a *file* → mkdir raises despite
        # exist_ok=True; the backstop converts it to a status dict.
        install_env["config_dir"].mkdir(parents=True)
        (install_env["config_dir"] / "bin").write_bytes(b"not a directory")
        install_env["serve"](_tar_gz({"betterleaks": BINARY_CONTENT}))

        result = betterleaks_install.install()

        assert result["status"] == "install-failed"
        assert "FileExistsError" in result["error"]


class TestPlatformKey:
    def test_real_platform_resolves_or_is_unsupported(self):
        key = betterleaks_install.platform_key()
        assert key is None or key in betterleaks_install._ARCHIVE_SHA256

    @pytest.mark.parametrize(
        ("sys_platform", "os_name", "machine", "expected"),
        [
            ("darwin", "posix", "arm64", "darwin_arm64"),
            ("darwin", "posix", "x86_64", "darwin_x64"),
            ("linux", "posix", "x86_64", "linux_x64"),
            ("linux", "posix", "aarch64", "linux_arm64"),
            ("win32", "nt", "AMD64", "windows_x64"),
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
            betterleaks_install.platform, "machine", lambda: machine
        )
        assert betterleaks_install.platform_key() == expected

    def test_checksum_table_covers_release_matrix(self):
        # Upstream naming: x64 (not amd64) — see the release asset list.
        assert set(betterleaks_install._ARCHIVE_SHA256) == {
            "darwin_x64", "darwin_arm64",
            "linux_x64", "linux_arm64",
            "windows_x64", "windows_arm64",
        }
        for value in betterleaks_install._ARCHIVE_SHA256.values():
            assert len(value) == 64
            int(value, 16)  # valid hex

    def test_checksum_values_are_pairwise_distinct(self):
        values = list(betterleaks_install._ARCHIVE_SHA256.values())
        assert len(values) == len(set(values))

    def test_archive_filename_flavors(self):
        assert betterleaks_install.archive_filename("linux_x64") == (
            f"betterleaks_{PINNED}_linux_x64.tar.gz"
        )
        assert betterleaks_install.archive_filename("windows_arm64") == (
            f"betterleaks_{PINNED}_windows_arm64.zip"
        )

    def test_download_url_shape(self):
        url = betterleaks_install.download_url("linux_x64")
        assert url == (
            "https://github.com/betterleaks/betterleaks/releases/download/"
            f"v{PINNED}/betterleaks_{PINNED}_linux_x64.tar.gz"
        )


class TestBinaryResolution:
    """resolve_binary(): managed install wins over PATH."""

    def test_managed_binary_preferred_over_path(self, tmp_path, monkeypatch):
        config_dir = tmp_path / ".clawjournal"
        monkeypatch.setattr("clawjournal.config.CONFIG_DIR", config_dir)
        managed = betterleaks.managed_binary_path()
        managed.parent.mkdir(parents=True)
        managed.write_bytes(BINARY_CONTENT)
        managed.chmod(0o755)
        monkeypatch.setattr(
            "clawjournal.redaction.betterleaks.shutil.which",
            lambda name: "/usr/local/bin/betterleaks",
        )

        assert betterleaks.resolve_binary() == str(managed)
        assert betterleaks.is_available()

    def test_falls_back_to_path_when_no_managed_binary(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")
        monkeypatch.setattr(
            "clawjournal.redaction.betterleaks.shutil.which",
            lambda name: "/usr/local/bin/betterleaks",
        )
        assert betterleaks.resolve_binary() == "/usr/local/bin/betterleaks"

    def test_nothing_resolves(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")
        monkeypatch.setattr(
            "clawjournal.redaction.betterleaks.shutil.which", lambda name: None
        )
        assert betterleaks.resolve_binary() is None

    def test_install_hint_mentions_managed_install(self):
        assert "clawjournal betterleaks install" in betterleaks.INSTALL_HINT


class TestManagedOffPin:
    def _managed(self, tmp_path, monkeypatch):
        config_dir = tmp_path / ".clawjournal"
        monkeypatch.setattr("clawjournal.config.CONFIG_DIR", config_dir)
        managed = betterleaks.managed_binary_path()
        managed.parent.mkdir(parents=True)
        managed.write_bytes(BINARY_CONTENT)
        managed.chmod(0o755)
        return managed

    def test_none_for_path_resolved_binary(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")
        monkeypatch.setattr(
            "clawjournal.redaction.betterleaks.shutil.which",
            lambda name: "/usr/local/bin/betterleaks",
        )
        monkeypatch.setattr(
            betterleaks, "engine_fingerprint", lambda: "betterleaks 1.0.0"
        )
        assert betterleaks.managed_off_pin() is None

    def test_none_when_managed_matches_pin(self, tmp_path, monkeypatch):
        self._managed(tmp_path, monkeypatch)
        monkeypatch.setattr(
            betterleaks, "engine_fingerprint", lambda: f"betterleaks {PINNED}"
        )
        assert betterleaks.managed_off_pin() is None

    def test_reports_drift_for_off_pin_managed_copy(self, tmp_path, monkeypatch):
        self._managed(tmp_path, monkeypatch)
        monkeypatch.setattr(
            betterleaks, "engine_fingerprint", lambda: "betterleaks 1.0.0"
        )
        assert betterleaks.managed_off_pin() == ("1.0.0", PINNED)

    def test_none_when_fingerprint_unparseable(self, tmp_path, monkeypatch):
        self._managed(tmp_path, monkeypatch)
        monkeypatch.setattr(betterleaks, "engine_fingerprint", lambda: "unknown")
        assert betterleaks.managed_off_pin() is None


class TestCliCommand:
    def test_status_json_when_missing_exits_nonzero(self, tmp_path, monkeypatch, capsys):
        import json as json_mod

        from clawjournal.cli import _run_betterleaks_command

        monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")
        monkeypatch.setattr(
            "clawjournal.redaction.betterleaks.shutil.which", lambda name: None
        )
        args = Namespace(betterleaks_command="status", json=True)

        with pytest.raises(SystemExit) as excinfo:
            _run_betterleaks_command(args)

        assert excinfo.value.code == 1
        payload = json_mod.loads(capsys.readouterr().out)
        assert payload["available"] is False
        assert payload["resolved_path"] is None
        assert payload["version"] is None
        assert payload["fingerprint"] == "missing"
        assert payload["pinned_version"] == PINNED

    def test_install_dispatch_passes_force_and_exits_zero(self, monkeypatch, capsys):
        from clawjournal import cli

        calls = {}

        def fake_install(*, force=False, progress=None):
            calls["force"] = force
            calls["progress"] = progress
            return {"status": "installed", "path": "/x/betterleaks", "version": PINNED}

        monkeypatch.setattr(
            "clawjournal.redaction.betterleaks_install.install", fake_install
        )
        args = Namespace(betterleaks_command="install", force=True, json=False)

        cli._run_betterleaks_command(args)

        assert calls["force"] is True
        assert calls["progress"] is not None  # human mode streams progress
        assert f"Installed Betterleaks {PINNED}" in capsys.readouterr().out

    def test_install_failure_exits_nonzero(self, monkeypatch, capsys):
        from clawjournal import cli

        monkeypatch.setattr(
            "clawjournal.redaction.betterleaks_install.install",
            lambda *, force=False, progress=None: {
                "status": "download-failed",
                "error": "offline",
                "url": "https://example.invalid/archive.tar.gz",
                "hint": "Check your network.",
            },
        )
        args = Namespace(betterleaks_command="install", force=False, json=False)

        with pytest.raises(SystemExit) as excinfo:
            cli._run_betterleaks_command(args)

        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert "download-failed" in out
        assert "https://example.invalid/archive.tar.gz" in out
        assert "Check your network." in out

    def test_status_warns_when_managed_copy_off_pin(self, tmp_path, monkeypatch, capsys):
        from clawjournal.cli import _run_betterleaks_command

        config_dir = tmp_path / ".clawjournal"
        monkeypatch.setattr("clawjournal.config.CONFIG_DIR", config_dir)
        managed = betterleaks.managed_binary_path()
        managed.parent.mkdir(parents=True)
        managed.write_bytes(BINARY_CONTENT)
        managed.chmod(0o755)
        monkeypatch.setattr(
            "clawjournal.redaction.betterleaks.engine_fingerprint",
            lambda: "betterleaks 1.0.0",
        )
        monkeypatch.setattr("shutil.which", lambda name: None)
        args = Namespace(betterleaks_command="status", json=False)

        # Warn, never block: off-pin status still exits 0.
        _run_betterleaks_command(args)

        out = capsys.readouterr().out
        assert "Warning" in out
        assert "v1.0.0" in out
        assert f"pins v{PINNED}" in out
        assert "clawjournal betterleaks install" in out

    def test_parser_roundtrip_via_main(self, tmp_path, monkeypatch, capsys):
        import sys as sys_mod

        from clawjournal.cli import main

        monkeypatch.setenv("CLAWJOURNAL_NO_AUTO_UPDATE", "1")
        monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")
        monkeypatch.setattr(
            "clawjournal.redaction.betterleaks.shutil.which", lambda name: None
        )
        monkeypatch.setattr(
            sys_mod, "argv", ["clawjournal", "betterleaks", "status", "--json"]
        )

        with pytest.raises(SystemExit) as excinfo:
            main()

        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert '"available": false' in out
