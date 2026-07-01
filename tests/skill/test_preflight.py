"""Preflight (§7.0): block with next steps on a broken/empty setup."""

from clawjournal.skill import preflight as pf


def _ok_env(monkeypatch, backends=("codex",), cfg=None):
    monkeypatch.setenv("CLAWJOURNAL_SKIP_TRUFFLEHOG", "1")   # skip trufflehog check
    monkeypatch.setattr(pf, "load_config",
                        lambda: {"source": "all", "projects_confirmed": True} if cfg is None else cfg)
    monkeypatch.setattr(pf, "installed_backends", lambda: list(backends))


def test_clean_when_ready(monkeypatch):
    _ok_env(monkeypatch)
    assert pf.preflight() == []


def test_blocks_when_source_and_projects_unset(monkeypatch):
    _ok_env(monkeypatch, cfg={})
    probs = pf.preflight()
    assert any("Source scope" in p for p in probs)
    assert any("Projects" in p for p in probs)


def test_blocks_when_no_backend(monkeypatch):
    _ok_env(monkeypatch, backends=())
    assert any("backend" in p.lower() for p in pf.preflight())


def test_blocks_when_explicit_backend_missing(monkeypatch):
    _ok_env(monkeypatch, backends=("claude",))
    monkeypatch.setattr(pf.shutil, "which", lambda cmd: None if cmd == "codex" else f"/bin/{cmd}")
    assert any("codex backend" in p for p in pf.preflight(backend="codex"))


def test_allows_explicit_backend_when_installed(monkeypatch):
    _ok_env(monkeypatch, backends=("claude",))
    monkeypatch.setattr(pf.shutil, "which", lambda cmd: f"/bin/{cmd}")
    assert pf.preflight(backend="codex") == []


def test_blocks_when_trufflehog_missing(monkeypatch):
    monkeypatch.delenv("CLAWJOURNAL_SKIP_TRUFFLEHOG", raising=False)
    monkeypatch.setattr(pf, "load_config", lambda: {"source": "all", "projects_confirmed": True})
    monkeypatch.setattr(pf, "installed_backends", lambda: ["codex"])
    monkeypatch.setattr(pf.trufflehog, "is_bypassed", lambda: False)
    monkeypatch.setattr(pf.trufflehog, "is_available", lambda: False)
    assert any("TruffleHog" in p for p in pf.preflight())
