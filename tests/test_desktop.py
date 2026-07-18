"""Tests for the optional desktop shortcut and dynamic expression icon."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest

from clawjournal import desktop


@pytest.fixture
def isolated_desktop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    config = home / ".clawjournal"
    state = config / "desktop"
    desktop_dir = home / "Desktop"
    monkeypatch.setattr(desktop, "CONFIG_DIR", config)
    monkeypatch.setattr(desktop, "STATE_DIR", state)
    monkeypatch.setattr(desktop, "ICONS_DIR", state / "icons")
    monkeypatch.setattr(desktop, "STATE_FILE", state / "install.json")
    monkeypatch.setattr(desktop, "LAST_OPENED_FILE", state / "last_opened")
    monkeypatch.setattr(desktop, "LOG_FILE", state / "launcher.log")
    monkeypatch.setattr(desktop, "WINDOWS_BOOTSTRAP", state / "windows-launch.py")
    monkeypatch.setattr(desktop, "_home", lambda: home)
    monkeypatch.setattr(desktop, "_desktop_dir", lambda _platform: desktop_dir)
    monkeypatch.setattr(desktop, "_platform", lambda: "linux")
    monkeypatch.setattr(desktop, "_frontend_available", lambda: True)
    monkeypatch.setattr(desktop.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        desktop,
        "_run_quiet",
        lambda command: subprocess.CompletedProcess(command, 0, "", ""),
    )
    return home


def test_days_since_last_opened_uses_calendar_days(
    isolated_desktop: Path,
) -> None:
    now = dt.datetime(2026, 7, 17, 0, 5, tzinfo=dt.timezone(dt.timedelta(hours=-4)))
    desktop._write_last_opened(now - dt.timedelta(minutes=10))
    assert desktop.days_since_last_opened(now) == 1

    desktop._write_last_opened(now - dt.timedelta(days=30))
    assert desktop.days_since_last_opened(now) == 10


def test_render_face_writes_standard_icon_formats(isolated_desktop: Path) -> None:
    desktop.render_icon(0)
    desktop.render_icon(10)
    smile = (desktop.ICONS_DIR / "clawjournal-day-0.png").read_bytes()
    crying = (desktop.ICONS_DIR / "clawjournal-day-10.png").read_bytes()
    ico = (desktop.ICONS_DIR / "clawjournal-day-10.ico").read_bytes()
    icns = (desktop.ICONS_DIR / "clawjournal-day-10.icns").read_bytes()

    assert smile.startswith(b"\x89PNG\r\n\x1a\n")
    assert crying.startswith(b"\x89PNG\r\n\x1a\n")
    assert smile != crying
    assert ico[:6] == b"\x00\x00\x01\x00\x01\x00"
    assert b"\x89PNG\r\n\x1a\n" in ico
    assert icns.startswith(b"icns")
    assert b"ic08" in icns[:16]


def test_linux_install_refresh_and_uninstall(isolated_desktop: Path) -> None:
    result = desktop.install()
    shortcut = Path(result["shortcut"])

    assert shortcut.exists()
    assert "X-ClawJournal-Managed=true" in shortcut.read_text(encoding="utf-8")
    assert "clawjournal-day-0.png" in shortcut.read_text(encoding="utf-8")
    assert desktop.status()["installed"] is True

    opened = dt.datetime.now().astimezone() - dt.timedelta(days=20)
    desktop._write_last_opened(opened)
    refreshed = desktop.refresh(quiet=True)
    assert refreshed["day"] == 10
    assert refreshed["mood"] == "dramatic crying"
    assert "clawjournal-day-10.png" in shortcut.read_text(encoding="utf-8")

    removed = desktop.uninstall()
    assert removed["removed"] is True
    assert not shortcut.exists()
    assert not desktop.STATE_DIR.exists()


def test_install_removes_managed_previous_shortcut(isolated_desktop: Path) -> None:
    previous = isolated_desktop / "Desktop" / "ClawJournal Workbench.desktop"
    previous.parent.mkdir(parents=True)
    previous.write_text("X-ClawJournal-Managed=true\n", encoding="utf-8")
    desktop._write_state({
        "version": 1,
        "platform": "linux",
        "shortcut": str(previous),
    })

    result = desktop.install()

    assert not previous.exists()
    assert Path(result["shortcut"]).name == "ClawJournal.desktop"
    assert Path(result["shortcut"]).exists()


def test_note_opened_is_noop_until_installed(isolated_desktop: Path) -> None:
    desktop.note_opened()
    assert not desktop.LAST_OPENED_FILE.exists()


def test_launchers_start_outside_home_shadow_package(isolated_desktop: Path) -> None:
    shell_launcher = desktop._shell_launcher(desktop._command("desktop", "launch"), log=True)
    assert f"cd {desktop.shlex.quote(str(desktop.STATE_DIR))}" in shell_launcher
    assert f"cd {desktop.shlex.quote(str(isolated_desktop))}" not in shell_launcher

    desktop._write_windows_bootstrap()
    bootstrap = desktop.WINDOWS_BOOTSTRAP.read_text(encoding="utf-8")
    assert "from clawjournal.cli import main" in bootstrap
    assert repr(str(desktop.LOG_FILE)) in bootstrap


def test_launch_reuses_live_workbench(
    isolated_desktop: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(desktop, "note_opened", lambda: calls.append("opened"))
    monkeypatch.setattr(desktop, "load_config", lambda: {"daemon_port": 9001})
    monkeypatch.setattr(desktop, "_workbench_running", lambda port: port == 9001)
    monkeypatch.setattr(desktop, "_request_scan", lambda port: calls.append(("scan", port)))
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: calls.append(("browser", url)))

    desktop.launch()

    assert calls == [
        "opened",
        ("scan", 9001),
        ("browser", "http://localhost:9001/"),
    ]


def test_status_json_shape(isolated_desktop: Path) -> None:
    assert desktop.status() == {"installed": False}
    desktop.STATE_DIR.mkdir(parents=True)
    shortcut = isolated_desktop / "Desktop" / "ClawJournal.desktop"
    shortcut.parent.mkdir(parents=True)
    shortcut.write_text("managed", encoding="utf-8")
    desktop._write_last_opened()
    desktop._write_state({
        "version": 1,
        "platform": "linux",
        "shortcut": str(shortcut),
        "refresh_mode": "test",
    })
    result = json.loads(json.dumps(desktop.status()))
    assert result["installed"] is True
    assert result["day"] == 0
    assert result["mood"] == "big smile"
