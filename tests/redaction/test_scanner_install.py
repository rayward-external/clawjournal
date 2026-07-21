from __future__ import annotations

from pathlib import Path

from clawjournal.redaction import (
    betterleaks,
    betterleaks_install,
    scanner_install,
    trufflehog,
    trufflehog_install,
)


def _scanner_state(monkeypatch, tmp_path: Path, *, better: bool, truffle: bool):
    state = {"betterleaks": better, "trufflehog": truffle}
    managed = {
        "betterleaks": tmp_path / "bin" / "betterleaks",
        "trufflehog": tmp_path / "bin" / "trufflehog",
    }
    for name, scanner in (("betterleaks", betterleaks), ("trufflehog", trufflehog)):
        monkeypatch.delenv(scanner.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(scanner, "managed_binary_path", lambda name=name: managed[name])
        monkeypatch.setattr(scanner, "is_available", lambda name=name: state[name])
        monkeypatch.setattr(
            scanner,
            "resolve_binary",
            lambda name=name: str(managed[name]) if state[name] else None,
        )
    return state, managed


def test_available_scanners_do_not_run_installers(monkeypatch, tmp_path):
    _scanner_state(monkeypatch, tmp_path, better=True, truffle=True)

    def unexpected_install(**_kwargs):
        raise AssertionError("installer should not run")

    monkeypatch.setattr(betterleaks_install, "install", unexpected_install)
    monkeypatch.setattr(trufflehog_install, "install", unexpected_install)

    result = scanner_install.ensure_share_scanners()

    assert result["ok"] is True
    assert result["missing"] == []
    assert all(
        row["install_attempted"] is False
        for row in result["scanners"].values()
    )


def test_first_share_repairs_scanners_added_by_an_update(monkeypatch, tmp_path):
    state, _managed = _scanner_state(
        monkeypatch,
        tmp_path,
        better=False,
        truffle=True,
    )
    installs: list[str] = []

    def install_betterleaks(**_kwargs):
        installs.append("betterleaks")
        state["betterleaks"] = True
        return {"status": "installed", "version": "1.6.1"}

    monkeypatch.setattr(betterleaks_install, "install", install_betterleaks)
    monkeypatch.setattr(
        trufflehog_install,
        "install",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("existing scanner should not be reinstalled")
        ),
    )

    result = scanner_install.ensure_share_scanners()

    assert result["ok"] is True
    assert installs == ["betterleaks"]
    assert result["scanners"]["betterleaks"] == {
        "ok": True,
        "status": "installed",
        "install_attempted": True,
        "available": True,
        "managed": True,
        "resolved_path": str(tmp_path / "bin" / "betterleaks"),
        "version": "1.6.1",
    }


def test_prefer_managed_installs_over_path_copy(monkeypatch, tmp_path):
    state, managed = _scanner_state(monkeypatch, tmp_path, better=True, truffle=True)
    paths = {
        "betterleaks": "/usr/local/bin/betterleaks",
        "trufflehog": "/usr/local/bin/trufflehog",
    }
    monkeypatch.setattr(betterleaks, "resolve_binary", lambda: paths["betterleaks"])
    monkeypatch.setattr(trufflehog, "resolve_binary", lambda: paths["trufflehog"])

    def managed_install(name: str):
        paths[name] = str(managed[name])
        state[name] = True
        return {"status": "installed"}

    monkeypatch.setattr(
        betterleaks_install,
        "install",
        lambda **_kwargs: managed_install("betterleaks"),
    )
    monkeypatch.setattr(
        trufflehog_install,
        "install",
        lambda **_kwargs: managed_install("trufflehog"),
    )

    result = scanner_install.ensure_share_scanners(prefer_managed=True)

    assert result["ok"] is True
    assert all(row["managed"] for row in result["scanners"].values())
    assert all(row["install_attempted"] for row in result["scanners"].values())


def test_install_failure_is_structured_and_fail_closed(monkeypatch, tmp_path):
    _scanner_state(monkeypatch, tmp_path, better=False, truffle=True)
    monkeypatch.setattr(
        betterleaks_install,
        "install",
        lambda **_kwargs: {
            "status": "download-failed",
            "error": "network unavailable",
            "hint": "check proxy settings",
        },
    )

    result = scanner_install.ensure_share_scanners()

    assert result["ok"] is False
    assert result["missing"] == ["betterleaks"]
    assert "network unavailable" in result["error"]
    assert result["scanners"]["betterleaks"]["hint"] == "check proxy settings"


def test_bypassed_scanner_is_never_installed(monkeypatch, tmp_path):
    _scanner_state(monkeypatch, tmp_path, better=False, truffle=True)
    monkeypatch.setenv(betterleaks.SKIP_ENV_VAR, "1")
    monkeypatch.setattr(
        betterleaks_install,
        "install",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("bypassed scanner should not be installed")
        ),
    )

    result = scanner_install.ensure_share_scanners()

    assert result["ok"] is True
    assert result["scanners"]["betterleaks"]["status"] == "bypassed"
