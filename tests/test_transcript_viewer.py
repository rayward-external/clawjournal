from clawjournal import transcript_viewer


SESSION = {"messages": [
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "world"},
]}


def test_falls_back_to_dump_when_not_a_tty(monkeypatch, capsys):
    monkeypatch.setattr(transcript_viewer, "_stdout_isatty", lambda: False)
    called = {"pager": False}
    monkeypatch.setattr(transcript_viewer, "_run_pager",
                        lambda *a, **k: called.__setitem__("pager", True))
    transcript_viewer.view_transcript(SESSION, title="t")
    out = capsys.readouterr().out
    assert "hello" in out and "world" in out
    assert called["pager"] is False  # never tried the TUI without a tty


def test_falls_back_to_dump_when_stdin_not_a_tty(monkeypatch, capsys):
    # stdout is a tty but stdin is not (e.g. a heredoc / piped invocation).
    # prompt_toolkit reads keys from stdin, so the pager would render but be
    # frozen — fall back to the dump instead of launching a dead TUI.
    monkeypatch.setattr(transcript_viewer, "_stdout_isatty", lambda: True)
    monkeypatch.setattr(transcript_viewer, "_stdin_isatty", lambda: False)
    called = {"pager": False}
    monkeypatch.setattr(transcript_viewer, "_run_pager",
                        lambda *a, **k: called.__setitem__("pager", True))
    transcript_viewer.view_transcript(SESSION, title="t")
    out = capsys.readouterr().out
    assert "hello" in out and "world" in out
    assert called["pager"] is False  # no TUI when stdin can't deliver input


def test_falls_back_to_dump_when_pager_raises(monkeypatch, capsys):
    monkeypatch.setattr(transcript_viewer, "_stdout_isatty", lambda: True)
    monkeypatch.setattr(transcript_viewer, "_stdin_isatty", lambda: True)

    def _boom(*a, **k):
        raise ImportError("no prompt_toolkit")

    monkeypatch.setattr(transcript_viewer, "_run_pager", _boom)
    transcript_viewer.view_transcript(SESSION, title="t")
    captured = capsys.readouterr()
    assert "hello" in captured.out and "world" in captured.out
    assert "fell back" in captured.err  # diagnostic breadcrumb, not silent


def test_module_does_not_import_prompt_toolkit_at_top_level():
    import inspect
    import re

    src = inspect.getsource(transcript_viewer)
    top = src.split("def ", 1)[0]  # module preamble, before the first function
    # The name may appear in the docstring; what must NOT appear is a top-level
    # import (prompt_toolkit must be imported lazily inside functions).
    assert not re.search(r"^\s*(import prompt_toolkit|from prompt_toolkit)", top, re.M), \
        "prompt_toolkit must be imported lazily, not at module top level"


def test_pager_scrolls_and_holds_then_exits_on_q():
    # Regression: the original FormattedTextControl viewer had no cursor, so the
    # Window snapped vertical_scroll back to 0 every render and could not scroll.
    # The buffer-backed TextArea moves a real cursor, so Down advances and HOLDS.
    import pytest
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    lines = [f"line {i}" for i in range(200)]
    with create_pipe_input() as inp:
        inp.send_text("\x1b[B\x1b[B\x1b[B\x1b[B\x1b[B")  # five Down arrows
        inp.send_text("q")                                # then quit
        with create_app_session(input=inp, output=DummyOutput()):
            # Build inside the session so the app uses the pipe input / dummy output.
            app, body = transcript_viewer._build_pager_app(lines, title="t")
            app.run()
    # Cursor advanced 5 rows and held — not stuck/oscillating at 0.
    assert body.buffer.document.cursor_position_row == 5


def test_never_raises_on_malformed_session_tty(monkeypatch, capsys):
    # Regression: a non-dict message entry used to escape on the TTY path,
    # aborting the share wizard. It must degrade instead.
    monkeypatch.setattr(transcript_viewer, "_stdout_isatty", lambda: True)
    monkeypatch.setattr(transcript_viewer, "_stdin_isatty", lambda: True)
    transcript_viewer.view_transcript({"messages": [123]}, title="t")  # must not raise
    assert "could not render transcript" in capsys.readouterr().out


def test_never_raises_on_malformed_session_non_tty(monkeypatch, capsys):
    monkeypatch.setattr(transcript_viewer, "_stdout_isatty", lambda: False)
    transcript_viewer.view_transcript({"messages": [123]}, title="t")  # must not raise
    assert "could not render transcript" in capsys.readouterr().out


def test_role_line_fragments_colors_only_the_role_word():
    from clawjournal import share_cli

    session = {"messages": [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there\nsecond line"},
    ]}
    raw = transcript_viewer._ANSI_ESCAPE_RE.sub(
        "", "\n".join(share_cli._transcript_lines(session)))
    doc_lines = raw.split("\n")

    # The note line (index 0) carries no role color.
    assert transcript_viewer._role_line_fragments(doc_lines[0]) == [("", doc_lines[0])]

    def colored(line):
        return [(style, word) for style, word in
                transcript_viewer._role_line_fragments(line) if style]

    user_line = next(l for l in doc_lines if l[2:11].strip() == "user")
    asst_line = next(l for l in doc_lines if l[2:11].strip() == "assistant")
    cont_line = next(l for l in doc_lines if l.startswith("             second line"))

    assert colored(user_line) == [("fg:ansicyan", "user")]
    assert colored(asst_line) == [("fg:ansiyellow", "assistant")]
    # Continuation/wrapped message lines keep the default style.
    assert colored(cont_line) == []


def _vt100_app(lines):
    # Build the pager with a real fixed-size Vt100 output so render_info exists
    # (DummyOutput leaves it None, which makes wheel/scroll math no-op).
    import io
    from prompt_toolkit.output.vt100 import Vt100_Output
    from prompt_toolkit.data_structures import Size

    out = Vt100_Output(io.StringIO(), lambda: Size(rows=24, columns=80))
    app, body = transcript_viewer._build_pager_app(lines, title="t")
    app.output = out
    return app, body


def test_pager_mouse_wheel_scrolls():
    import pytest
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.input import create_pipe_input

    lines = [f"line {i}" for i in range(200)]
    with create_pipe_input() as inp:
        inp.send_text("\x1b[<65;1;1M" * 5)  # SGR mouse wheel-down x5
        inp.send_text("q")
        app, body = _vt100_app(lines)
        app.input = inp
        app.run()
    assert body.buffer.document.cursor_position_row == 5  # wheel scrolled 5 lines


def test_pager_end_jumps_to_document_bottom():
    import pytest
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.input import create_pipe_input

    lines = [f"line {i}" for i in range(200)]
    with create_pipe_input() as inp:
        inp.send_text("\x1b[F")  # End -> document bottom
        inp.send_text("q")
        app, body = _vt100_app(lines)
        app.input = inp
        app.run()
    assert body.buffer.document.cursor_position_row == 199


def test_role_lexer_is_wired_into_the_textarea():
    import pytest
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from clawjournal import share_cli

    lines = share_cli._transcript_lines({"messages": [{"role": "user", "content": "hi"}]})
    with create_pipe_input() as inp:
        inp.send_text("q")
        with create_app_session(input=inp, output=DummyOutput()):
            app, body = transcript_viewer._build_pager_app(lines, title="t")
            # The lexer must be the one attached to the body, and it must color
            # the user role on the actual buffer content (line 0 is the note).
            frags = body.lexer.lex_document(body.buffer.document)(1)
            app.run()
    assert any(style == "fg:ansicyan" for style, _ in frags)
