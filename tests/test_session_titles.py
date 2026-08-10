from clawjournal.session_titles import (
    append_fork_title_suffix,
    fork_title_suffix,
    resolve_session_title,
)


def _fork(**overrides):
    session = {
        "session_id": "fork-child-9976_seg-0002",
        "display_title": "Raw title",
        "ai_display_title": "AI title",
        "fork_of": "parent-thread-id",
        "fork_nickname": "Kierkegaard",
    }
    session.update(overrides)
    return session


def test_resolve_prefers_ai_and_labels_fork():
    assert resolve_session_title(_fork()) == "AI title · fork: Kierkegaard"


def test_resolve_can_deliberately_use_ingest_title():
    assert resolve_session_title(_fork(), prefer_ai=False) == (
        "Raw title · fork: Kierkegaard"
    )


def test_fork_label_is_idempotent_for_predecorated_titles():
    session = _fork(ai_display_title="AI title · fork: Kierkegaard")
    resolved = resolve_session_title(session)
    assert resolved == "AI title · fork: Kierkegaard"
    assert resolved.count(" · fork:") == 1
    assert append_fork_title_suffix(resolved, session) == resolved


def test_new_nickname_replaces_an_older_fallback_suffix():
    session = _fork(ai_display_title="AI title · fork: 9976")
    resolved = resolve_session_title(session)
    assert resolved == "AI title · fork: Kierkegaard"
    assert resolved.count(" · fork:") == 1


def test_fork_nickname_cannot_reintroduce_a_secret_into_resolved_title():
    token = "ghp_" + "a" * 36
    resolved = resolve_session_title(_fork(fork_nickname=token))
    assert token not in resolved
    assert "[REDACTED_GITHUB_TOKEN]" in resolved


def test_fork_without_nickname_uses_unsegmented_session_id_tail():
    session = _fork(fork_nickname=None)
    assert fork_title_suffix(session) == " · fork: 9976"
    assert resolve_session_title(session) == "AI title · fork: 9976"


def test_nonfork_and_empty_title_fallback_are_unchanged():
    assert resolve_session_title(
        {"session_id": "main", "display_title": "Main title"}
    ) == "Main title"
    assert resolve_session_title(
        _fork(display_title="", ai_display_title=""), fallback="Fallback"
    ) == "Fallback · fork: Kierkegaard"
