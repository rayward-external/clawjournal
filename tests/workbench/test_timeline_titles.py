from clawjournal.workbench.timeline import (
    TimelinePage,
    _session_title,
    render_timeline_html,
)


def _fork_workbench_row():
    return {
        "session_id": "fork-child-9976",
        "session_key": "codex:proj:fork-child-9976",
        "project": "proj",
        "display_title": "Raw title · fork: Kierkegaard",
        "ai_display_title": "AI title",
        "fork_of": "parent-thread-id",
        "fork_nickname": "Kierkegaard",
    }


def test_session_title_labels_preferred_ai_title_for_fork():
    title = _session_title(
        _fork_workbench_row(),
        {"session_key": "codex:proj:fork-child-9976"},
    )

    assert title == "AI title · fork: Kierkegaard"


def test_pending_timeline_page_labels_preferred_ai_title_for_fork():
    page = TimelinePage(
        requested_session_key="codex:proj:fork-child-9976",
        canonical_session_key="codex:proj:fork-child-9976",
        redirect_session_key=None,
        root=None,
        workbench_row=_fork_workbench_row(),
    )

    rendered = render_timeline_html(page)

    assert "<title>AI title · fork: Kierkegaard · Session Timeline</title>" in rendered
