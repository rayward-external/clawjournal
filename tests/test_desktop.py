"""Tests for the optional desktop shortcut and dynamic expression icon."""

from __future__ import annotations

import datetime as dt
import json
import socket
import struct
import subprocess
import threading
import zlib
from pathlib import Path

import pytest

from clawjournal import desktop


def _decode_rgba(png: bytes) -> tuple[int, int, bytes]:
    """Minimal decoder for the 8-bit RGBA, filter-0 PNGs this module writes."""
    pos, idat, width, height = 8, b"", 0, 0
    while pos < len(png):
        length = struct.unpack(">I", png[pos:pos + 4])[0]
        kind = png[pos + 4:pos + 8]
        data = png[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height = struct.unpack(">II", data[:8])
        elif kind == b"IDAT":
            idat += data
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = width * 4
    pixels = bytearray()
    for y in range(height):
        assert raw[y * (stride + 1)] == 0, "only filter type 0 is written"
        pixels += raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)]
    return width, height, bytes(pixels)


@pytest.fixture
def isolated_desktop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    desktop_dir = home / "Desktop"
    # Every desktop path derives from `clawjournal.config.CONFIG_DIR` at call
    # time, so redirecting that one attribute isolates all of them. The autouse
    # `tmp_config` fixture in conftest already does this for every test in the
    # suite; this only repoints it under the fake home so state and shortcut
    # live together.
    monkeypatch.setattr("clawjournal.config.CONFIG_DIR", home / ".clawjournal")
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


def test_day_count_survives_a_dst_fall_back(isolated_desktop: Path) -> None:
    """Same local date either side of a DST shift must still be Day 0.

    Regression: the stored instant was reprojected through *today's* UTC
    offset, so an open at 00:30 on the fall-back day read as the previous
    calendar day once the clocks went back that afternoon.
    """
    opened = dt.datetime(2026, 11, 1, 0, 30, tzinfo=dt.timezone(dt.timedelta(hours=-4)))
    afternoon = dt.datetime(2026, 11, 1, 20, 0, tzinfo=dt.timezone(dt.timedelta(hours=-5)))
    assert opened.date() == afternoon.date()

    desktop._write_last_opened(opened)
    assert desktop.days_since_last_opened(afternoon) == 0
    assert desktop.opened_today(afternoon) is True

    # The next local day still advances, spring-forward offset included.
    next_day = dt.datetime(2026, 11, 2, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=-5)))
    assert desktop.days_since_last_opened(next_day) == 1
    assert desktop.opened_today(next_day) is False


def test_png_stores_straight_alpha_not_premultiplied(isolated_desktop: Path) -> None:
    """Regression: RGB was written premultiplied into a colour-type-6 PNG, so
    the drop shadow rendered near-black and every edge carried a dark fringe."""
    shadow = (79, 53, 18, 55)
    canvas = desktop._Canvas(4, scale=1)
    canvas.ellipse(2, 2, 2, 2, shadow)
    width, _, pixels = _decode_rgba(canvas.png())
    centre = (2 * width + 2) * 4
    red, green, blue, alpha = pixels[centre:centre + 4]

    assert alpha == shadow[3]
    # Exactness is limited by 8-bit premultiplied storage at low alpha; the bug
    # produced (17, 11, 4), which is nowhere near this tolerance.
    assert abs(red - shadow[0]) <= 3
    assert abs(green - shadow[1]) <= 3
    assert abs(blue - shadow[2]) <= 3

    # Strongly-covered edges of the face disk keep the face colour.
    _, _, face = _decode_rgba(desktop.render_face_png(0))
    edges = [
        face[i:i + 4] for i in range(0, len(face), 4) if 200 <= face[i + 3] < 255
    ]
    assert edges, "expected antialiased edge pixels"
    assert min(px[0] for px in edges) > 150  # premultiplied fringing lands near 145


def test_render_face_writes_standard_icon_formats(isolated_desktop: Path) -> None:
    desktop.render_icon(0)
    desktop.render_icon(10)
    smile = (desktop._icons_dir() / "clawjournal-day-0.png").read_bytes()
    crying = (desktop._icons_dir() / "clawjournal-day-10.png").read_bytes()
    ico = (desktop._icons_dir() / "clawjournal-day-10.ico").read_bytes()
    icns = (desktop._icons_dir() / "clawjournal-day-10.icns").read_bytes()

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
    assert not desktop._state_dir().exists()


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
    assert not desktop._last_opened_file().exists()


def test_note_opened_rewrites_only_on_a_new_day(
    isolated_desktop: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop.install()
    refreshes: list[bool] = []
    monkeypatch.setattr(desktop, "refresh", lambda *, quiet=False: refreshes.append(quiet))

    # The daemon calls this on every index.html load; same-day opens must not
    # rewrite the shortcut.
    desktop.note_opened()
    desktop.note_opened()
    assert refreshes == []

    desktop._write_last_opened(dt.datetime.now().astimezone() - dt.timedelta(days=3))
    desktop.note_opened()
    assert refreshes == [True]
    assert desktop.opened_today() is True


def test_note_opened_async_coalesces_an_active_refresh(
    isolated_desktop: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def slow_note_opened() -> None:
        calls.append("opened")
        started.set()
        assert release.wait(timeout=2)

    monkeypatch.setattr(desktop, "note_opened", slow_note_opened)
    assert desktop.note_opened_async() is True
    assert started.wait(timeout=2)
    assert desktop.note_opened_async() is False
    release.set()
    assert desktop._note_opened_thread is not None
    desktop._note_opened_thread.join(timeout=2)
    assert calls == ["opened"]


def test_install_refuses_to_clobber_a_foreign_file_despite_stale_state(
    isolated_desktop: Path,
) -> None:
    """Ownership must be proven by the artifact, not by install.json."""
    result = desktop.install()
    shortcut = Path(result["shortcut"])

    # The user removed our shortcut and put their own file at the same path,
    # while install.json still names it.
    shortcut.unlink()
    shortcut.write_text("[Desktop Entry]\nName=My own launcher\n", encoding="utf-8")

    with pytest.raises(desktop.DesktopError, match="Refusing to replace"):
        desktop.install()
    assert "My own launcher" in shortcut.read_text(encoding="utf-8")


def test_uninstall_leaves_a_foreign_file_in_place(isolated_desktop: Path) -> None:
    result = desktop.install()
    shortcut = Path(result["shortcut"])
    shortcut.unlink()
    shortcut.write_text("[Desktop Entry]\nName=My own launcher\n", encoding="utf-8")

    assert desktop.uninstall()["removed"] is True
    assert shortcut.exists()


def test_uninstall_rejects_an_unknown_platform(isolated_desktop: Path) -> None:
    desktop._write_state({
        "version": 1,
        "platform": "haiku",
        "shortcut": str(isolated_desktop / "Desktop" / "ClawJournal.desktop"),
    })
    with pytest.raises(desktop.DesktopError, match="Unknown desktop integration platform"):
        desktop.uninstall()


def test_windows_shortcut_ownership_is_detected_in_both_encodings(
    isolated_desktop: Path,
) -> None:
    lnk = isolated_desktop / "Desktop" / "ClawJournal.lnk"
    lnk.parent.mkdir(parents=True)

    lnk.write_bytes(b"L\x00\x00\x00" + "windows-launch.py".encode("utf-16-le"))
    assert desktop._windows_shortcut_is_managed(lnk) is True

    lnk.write_bytes(b"L\x00\x00\x00" + b"clawjournal.cli")
    assert desktop._windows_shortcut_is_managed(lnk) is True

    lnk.write_bytes(b"L\x00\x00\x00" + "C:\\Games\\solitaire.exe".encode("utf-16-le"))
    assert desktop._windows_shortcut_is_managed(lnk) is False


def test_trim_log_keeps_the_recent_tail(isolated_desktop: Path) -> None:
    desktop._log_file().parent.mkdir(parents=True, exist_ok=True)
    line = "x" * 99 + "\n"
    desktop._log_file().write_text(line * 40_000, encoding="utf-8")
    assert desktop._log_file().stat().st_size > desktop.LOG_MAX_BYTES

    desktop._trim_log()

    trimmed = desktop._log_file().read_text(encoding="utf-8")
    assert len(trimmed) <= desktop.LOG_MAX_BYTES // 2
    # Whole lines only, and the newest content survives.
    assert trimmed.startswith("x")
    assert trimmed.endswith(line)

    desktop._trim_log()  # already small enough: unchanged
    assert desktop._log_file().read_text(encoding="utf-8") == trimmed


def test_trim_log_keeps_a_newline_free_log(isolated_desktop: Path) -> None:
    """Regression: one enormous line had no newline in the kept window, so the
    seek-to-line-boundary step discarded the entire log."""
    desktop._log_file().parent.mkdir(parents=True, exist_ok=True)
    desktop._log_file().write_text("y" * (desktop.LOG_MAX_BYTES * 3), encoding="utf-8")

    desktop._trim_log()

    kept = desktop._log_file().read_text(encoding="utf-8")
    assert kept, "the whole log was thrown away"
    assert len(kept) <= desktop.LOG_MAX_BYTES // 2
    assert set(kept) == {"y"}


def test_quiet_platform_commands_have_a_finite_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def time_out(command, **kwargs):
        assert kwargs["timeout"] == desktop.DESKTOP_COMMAND_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(desktop.subprocess, "run", time_out)
    result = desktop._run_quiet(["slow-platform-tool"])

    assert result.returncode == 124
    assert "timed out" in result.stderr


def test_ico_directory_states_a_non_256_dimension(isolated_desktop: Path) -> None:
    """0 means 256 in an ICO entry; smaller icons must state their real size."""
    png = desktop.render_face_png(0, size=64)
    entry = desktop._ico_from_png(png, size=64)[6:8]
    assert entry == bytes([64, 64])
    assert desktop._ico_from_png(png, size=256)[6:8] == bytes([0, 0])


def test_desktop_exec_quote_escapes_backslashes_for_both_layers(
    isolated_desktop: Path,
) -> None:
    """Desktop-entry values are unescaped twice, so one backslash needs four."""
    quoted = desktop._desktop_exec_quote(Path(r"/tmp/od\d/launch"))
    assert quoted == r'"/tmp/od\\\\d/launch"'
    # Escapes added for the other special characters must not be re-escaped.
    assert desktop._desktop_exec_quote(Path('/tmp/a$b`c"d')) == r'"/tmp/a\$b\`c\"d"'


def test_malformed_plist_is_not_ours_rather_than_a_crash(
    isolated_desktop: Path,
) -> None:
    """plistlib raises ExpatError (not InvalidFileException) on bad XML."""
    app = isolated_desktop / "Desktop" / "Foreign.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(
        b'<?xml version="1.0"?><plist version="1.0"><dict><key>oops'
    )
    assert desktop._macos_app_is_managed(app) is False


def test_install_cleans_up_a_shortcut_left_in_a_relocated_desktop_dir(
    isolated_desktop: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The desktop directory itself moves (OneDrive redirect, xdg change).

    Requiring the same parent orphaned the old icon permanently, since
    install.json is rewritten to the new path straight afterwards.
    """
    old_desktop = isolated_desktop / "Desktop"
    stale = Path(desktop.install()["shortcut"])
    assert stale.parent == old_desktop

    relocated = isolated_desktop / "OneDrive" / "Desktop"
    monkeypatch.setattr(desktop, "_desktop_dir", lambda _platform: relocated)
    result = desktop.install()

    assert Path(result["shortcut"]).parent == relocated
    assert not stale.exists(), "old shortcut orphaned in the previous desktop dir"


def test_bootstrap_does_not_log_a_traceback_for_a_clean_exit(
    isolated_desktop: Path,
) -> None:
    desktop._write_windows_bootstrap()
    bootstrap = desktop._windows_bootstrap().read_text(encoding="utf-8")
    assert "except SystemExit:" in bootstrap
    assert bootstrap.index("except SystemExit:") < bootstrap.index("except BaseException:")
    compile(bootstrap, str(desktop._windows_bootstrap()), "exec")


def test_days_since_last_opened_accepts_a_naive_stamp(isolated_desktop: Path) -> None:
    desktop._last_opened_file().parent.mkdir(parents=True, exist_ok=True)
    naive = dt.datetime.now() - dt.timedelta(days=4)
    desktop._last_opened_file().write_text(naive.isoformat(timespec="seconds") + "\n", encoding="utf-8")
    assert desktop.days_since_last_opened() == 4


def test_desktop_paths_follow_the_patched_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: these paths were module constants bound at import time.

    `tmp_config` in conftest is autouse but only patches
    `clawjournal.config.CONFIG_DIR`, so anything captured at import escaped it
    and still pointed at the real ~/.clawjournal. A daemon test that served
    index.html would then reach `note_opened()` and rewrite the developer's
    actual desktop shortcut. Deliberately does NOT use `isolated_desktop`.
    """
    assert Path.home() not in desktop._state_file().parents

    redirected = tmp_path / "elsewhere"
    monkeypatch.setattr("clawjournal.config.CONFIG_DIR", redirected)
    state = redirected / "desktop"
    assert desktop._state_dir() == state
    assert desktop._state_file() == state / "install.json"
    assert desktop._icons_dir() == state / "icons"
    assert desktop._last_opened_file() == state / "last_opened"
    assert desktop._log_file() == state / "launcher.log"
    assert desktop._windows_bootstrap() == state / "windows-launch.py"


def test_refresh_refuses_to_overwrite_a_foreign_file(isolated_desktop: Path) -> None:
    """Refresh runs unattended daily — it must not clobber a user's file."""
    shortcut = Path(desktop.install()["shortcut"])
    shortcut.unlink()
    shortcut.write_text("[Desktop Entry]\nName=My own launcher\n", encoding="utf-8")

    with pytest.raises(desktop.DesktopError, match="Refusing to replace"):
        desktop.refresh(quiet=True)
    assert "My own launcher" in shortcut.read_text(encoding="utf-8")


def test_refresh_does_not_resurrect_a_deleted_shortcut(isolated_desktop: Path) -> None:
    """Deleting the icon must stick, rather than reappearing at the next run."""
    shortcut = Path(desktop.install()["shortcut"])
    shortcut.unlink()

    with pytest.raises(desktop.DesktopError, match="is missing"):
        desktop.refresh(quiet=True)
    assert not shortcut.exists()

    # note_opened swallows it so the daemon keeps serving pages regardless.
    desktop._write_last_opened(dt.datetime.now().astimezone() - dt.timedelta(days=3))
    desktop.note_opened()
    assert not shortcut.exists()


def test_windows_refresh_never_writes_an_unowned_shortcut(
    isolated_desktop: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Windows path had no guards at all: it recreated a deleted .lnk at
    every scheduled run, leaving the user unable to remove the icon."""
    wrote: list[Path] = []
    monkeypatch.setattr(desktop, "_write_windows_shortcut", lambda p, i: wrote.append(p))
    lnk = isolated_desktop / "Desktop" / f"{desktop.SHORTCUT_NAME}.lnk"
    lnk.parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(desktop.DesktopError, match="is missing"):
        desktop._refresh_windows(lnk, 0)
    assert wrote == []
    assert not lnk.exists()

    lnk.write_bytes(b"L\x00\x00\x00" + "C:\\Games\\solitaire.exe".encode("utf-16-le"))
    with pytest.raises(desktop.DesktopError, match="Refusing to replace"):
        desktop._refresh_windows(lnk, 0)
    assert wrote == []


def test_macos_install_leaves_nothing_behind_when_it_fails(
    isolated_desktop: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial bundle would lack Info.plist and disown itself permanently."""
    monkeypatch.setattr(desktop, "_platform", lambda: "macos")
    app = isolated_desktop / "Desktop" / f"{desktop.SHORTCUT_NAME}.app"
    real_write = desktop._write_bytes

    def fail_on_bundle_icon(path: Path, payload: bytes) -> None:
        if path.name == "ClawJournal.icns":
            raise OSError("No space left on device")
        real_write(path, payload)

    monkeypatch.setattr(desktop, "_write_bytes", fail_on_bundle_icon)
    with pytest.raises(OSError):
        desktop.install()

    assert not app.exists()
    assert list(app.parent.glob(f".{app.name}.*.new")) == []

    # And the failure must not have wedged a later attempt.
    monkeypatch.setattr(desktop, "_write_bytes", real_write)
    result = desktop.install()
    assert Path(result["shortcut"]) == app
    assert desktop._macos_app_is_managed(app) is True
    assert (app / "Contents" / "MacOS" / "ClawJournal").exists()


def test_launchers_start_outside_home_shadow_package(isolated_desktop: Path) -> None:
    shell_launcher = desktop._shell_launcher(desktop._command("desktop", "launch"), log=True)
    launcher_lines = shell_launcher.splitlines()
    assert f"cd {desktop.shlex.quote(str(desktop._state_dir()))}" in launcher_lines
    assert f"cd {desktop.shlex.quote(str(isolated_desktop))}" not in launcher_lines

    desktop._write_windows_bootstrap()
    bootstrap = desktop._windows_bootstrap().read_text(encoding="utf-8")
    assert "from clawjournal.cli import main" in bootstrap
    assert "CREATE_NO_WINDOW" in bootstrap
    assert repr(str(desktop._log_file())) in bootstrap
    compile(bootstrap, str(desktop._windows_bootstrap()), "exec")


def test_port_probe_requires_a_valid_health_challenge_response(
    isolated_desktop: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = []

    class Response:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self.payload).encode()

    def urlopen(request, timeout):
        requests.append((request, timeout))
        challenge = request.full_url.rsplit("=", 1)[1]
        token = desktop.ensure_api_token(desktop._config_dir())
        return Response({
            "ok": True,
            "service": "clawjournal",
            "proof": desktop.api_health_proof(token, challenge),
        })

    monkeypatch.setattr(desktop, "_daemon_port_is_open", lambda _port: True)
    monkeypatch.setattr(desktop.urllib.request, "urlopen", urlopen)

    assert desktop._workbench_port_state(8384) == desktop._PORT_WORKBENCH
    request, timeout = requests[0]
    assert request.full_url.startswith(
        "http://127.0.0.1:8384/.well-known/clawjournal?challenge="
    )
    assert request.get_header("Authorization") is None
    assert timeout == 0.75

    monkeypatch.setattr(
        desktop.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response({
            "ok": True,
            "service": "clawjournal",
            "proof": "0" * 64,
        }),
    )
    assert desktop._workbench_port_state(8384) == desktop._PORT_OCCUPIED


def test_launch_reuses_live_workbench(
    isolated_desktop: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(desktop, "note_opened", lambda: calls.append("opened"))
    monkeypatch.setattr(desktop, "load_config", lambda: {"daemon_port": 9001})
    monkeypatch.setattr(
        desktop,
        "_workbench_port_state",
        lambda port: desktop._PORT_WORKBENCH if port == 9001 else desktop._PORT_FREE,
    )
    monkeypatch.setattr(desktop, "_request_scan", lambda port: calls.append(("scan", port)))
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: calls.append(("browser", url)))

    desktop.launch()

    assert calls == [
        "opened",
        ("scan", 9001),
        ("browser", "http://localhost:9001/"),
    ]


def test_occupied_unknown_port_is_not_opened_or_replaced(
    isolated_desktop: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare listener is not proof that the port belongs to ClawJournal."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    # Never accepts, so the TCP probe succeeds but authenticated health cannot.
    listener.listen(64)
    port = listener.getsockname()[1]

    calls: list[object] = []
    monkeypatch.setattr(desktop, "load_config", lambda: {"daemon_port": port})
    monkeypatch.setattr(desktop, "_request_scan", lambda p: calls.append(("scan", p)))
    monkeypatch.setattr(desktop.webbrowser, "open", lambda u: calls.append(("browser", u)))

    def must_not_run(**kwargs: object) -> None:
        raise AssertionError("started a second daemon against a live port")

    monkeypatch.setattr("clawjournal.workbench.daemon.run_server", must_not_run)
    try:
        assert desktop._workbench_port_state(port) == desktop._PORT_OCCUPIED
        with pytest.raises(desktop.DesktopError, match="another local service"):
            desktop.launch()
    finally:
        listener.close()

    assert calls == []
    assert desktop._daemon_port_is_open(port) is False


def test_losing_the_startup_race_does_not_spawn_a_duplicate(
    isolated_desktop: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two fast clicks: the loser must join the winner, not take a random port."""
    calls: list[object] = []
    monkeypatch.setattr(desktop, "load_config", lambda: {"daemon_port": 8384})
    states = iter((desktop._PORT_FREE, desktop._PORT_WORKBENCH))
    monkeypatch.setattr(desktop, "_workbench_port_state", lambda _p: next(states))
    monkeypatch.setattr(desktop, "_request_scan", lambda p: calls.append(("scan", p)))
    monkeypatch.setattr(desktop.webbrowser, "open", lambda u: calls.append(("browser", u)))
    monkeypatch.setattr("clawjournal.pricing.ensure_pricing_fresh", lambda *a, **k: None)

    def bind_conflict(**kwargs: object) -> None:
        assert kwargs["allow_port_fallback"] is False
        raise OSError(48, "Address already in use")

    monkeypatch.setattr("clawjournal.workbench.daemon.run_server", bind_conflict)
    desktop.launch()

    assert calls == [("scan", 8384), ("browser", "http://localhost:8384/")]


def test_losing_startup_race_to_another_service_reports_an_error(
    isolated_desktop: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(desktop, "load_config", lambda: {"daemon_port": 8384})
    states = iter((desktop._PORT_FREE, desktop._PORT_OCCUPIED))
    monkeypatch.setattr(desktop, "_workbench_port_state", lambda _p: next(states))
    monkeypatch.setattr(desktop.webbrowser, "open", lambda u: calls.append(("browser", u)))
    monkeypatch.setattr("clawjournal.pricing.ensure_pricing_fresh", lambda *a, **k: None)
    monkeypatch.setattr(
        "clawjournal.workbench.daemon.run_server",
        lambda **_kwargs: (_ for _ in ()).throw(OSError(48, "Address already in use")),
    )

    with pytest.raises(desktop.DesktopError, match="another local service"):
        desktop.launch()

    assert calls == []


def test_run_server_can_refuse_the_ephemeral_port_fallback() -> None:
    """`serve` keeps the fallback; the launcher opts out of it."""
    from clawjournal.workbench import daemon as daemon_mod

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(64)
    port = listener.getsockname()[1]
    try:
        with pytest.raises(OSError):
            daemon_mod.run_server(
                port=port, open_browser=False, allow_port_fallback=False
            )
    finally:
        listener.close()


def test_status_json_shape(isolated_desktop: Path) -> None:
    assert desktop.status() == {"installed": False}
    desktop._state_dir().mkdir(parents=True)
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
