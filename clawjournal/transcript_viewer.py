"""Scrollable, mouse-wheel-capable modal viewer for redacted transcripts.

Used by the interactive share wizard in :mod:`clawjournal.share_cli` to let a
user read a full transcript without dumping it into the terminal scrollback.
prompt_toolkit (a hard dependency) is imported lazily; the viewer still falls
back to the plain ``render_transcript`` dump whenever a TUI cannot run (stdin
or stdout is not a tty, prompt_toolkit is unavailable/broken, dumb ``$TERM``,
or any runtime error), so the share flow is never broken by the viewer.
"""
from __future__ import annotations

import re
import sys

# Strips SGR color escapes so the prepared lines can go into a plain-text,
# buffer-backed viewer (which is what makes it actually scroll — see _run_pager).
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")

_ROLE_STYLES = {"user": "fg:ansicyan", "assistant": "fg:ansiyellow"}


def _role_line_fragments(line: str) -> list[tuple[str, str]]:
    """Color just the role label word in a transcript line.

    ``_transcript_lines`` lays each message line out as ``"  {role:>9}  {text}"``
    (two leading spaces, a 9-wide right-justified role field, two spaces, text).
    Re-apply the original per-role color to the ``user``/``assistant`` word only,
    leaving padding and message text in the default style. Returns prompt_toolkit
    ``(style, text)`` fragments. Pure: no prompt_toolkit import needed.
    """
    if len(line) >= 13 and line[:2] == "  " and line[11:13] == "  ":
        role = line[2:11].strip()
        style = _ROLE_STYLES.get(role)
        if style is not None:
            start = 11 - len(role)  # role word begins after its right-justify pad
            return [("", line[:start]), (style, line[start:11]), ("", line[11:])]
    return [("", line)]


def _stdout_isatty() -> bool:
    try:
        return sys.stdout.isatty()
    except (AttributeError, OSError):
        return False


def _stdin_isatty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, OSError):
        return False


def view_transcript(redacted_session: dict, *, title: str = "") -> None:
    # Imported lazily to avoid an import cycle with share_cli.
    from .share_cli import _transcript_lines, render_transcript

    try:
        # The pager renders to stdout AND reads keys/mouse from stdin. If either
        # is not an interactive terminal (piped, heredoc, captured by an agent),
        # the TUI would render but be frozen — fall back to the plain dump.
        if not (_stdin_isatty() and _stdout_isatty()):
            render_transcript(redacted_session)
            return
        lines = _transcript_lines(redacted_session)
        _run_pager(lines, title=title)
    except Exception as exc:
        # Missing prompt_toolkit, dumb $TERM, malformed session, or any runtime
        # TUI failure: degrade rather than abort the share flow, but leave a
        # one-line breadcrumb on stderr so the cause is diagnosable in the field.
        print(f"  (transcript viewer fell back: {type(exc).__name__}: {exc})",
              file=sys.stderr)
        try:
            render_transcript(redacted_session)
        except Exception as dump_exc:
            # Even the plain dump failed — never raise into the wizard.
            print(f"  (could not render transcript: {type(dump_exc).__name__})",
                  file=sys.stderr)
            print("  (could not render transcript)")


def _build_pager_app(lines: list[str], *, title: str):
    """Build the full-screen pager Application over ``lines``.

    The body is a read-only, buffer-backed ``TextArea``. A buffer is essential:
    a ``FormattedTextControl``'s cursor is fixed at ``(0, 0)``, so the ``Window``
    snaps ``vertical_scroll`` back to the top every render to keep that cursor in
    view — making it impossible to scroll. With a real ``Buffer`` the cursor
    position moves as you navigate, so the ``Window`` scrolls to follow it.
    Arrow keys (default bindings) and the mouse wheel both move that cursor.

    Returns ``(app, body)`` so tests can drive the app and inspect scroll state.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.key_binding.bindings.scroll import (
        scroll_page_down,
        scroll_page_up,
    )
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.lexers import Lexer
    from prompt_toolkit.widgets import TextArea

    class _RoleLexer(Lexer):
        # Re-color the role label words on top of the plain-text buffer.
        def lex_document(self, document):
            doc_lines = document.lines

            def get_line(lineno):
                return list(_role_line_fragments(doc_lines[lineno]))

            return get_line

    # TextArea holds plain text; drop the SGR color escapes from the lines.
    text = _ANSI_ESCAPE_RE.sub("", "\n".join(lines))
    body = TextArea(
        text=text,
        read_only=True,
        scrollbar=True,
        focusable=True,
        wrap_lines=True,
        lexer=_RoleLexer(),
    )

    safe_title = re.sub(r"[\x00-\x1f\x7f]", " ", title).strip()
    header = f" {safe_title} " if safe_title else " transcript "
    hint = "↑/↓ PgUp/PgDn Home/End · wheel to scroll · q to close"
    status = Window(
        content=FormattedTextControl(ANSI(f"\033[7m{header}— {hint} \033[0m")),
        height=1,
    )

    kb = KeyBindings()
    # Arrow keys + Home/End-of-line come from prompt_toolkit's default bindings
    # (they move the buffer cursor). Add page scrolling and document Home/End.
    kb.add("pageup")(scroll_page_up)
    kb.add("pagedown")(scroll_page_down)

    @kb.add("home")
    def _(event):
        event.app.current_buffer.cursor_position = 0

    @kb.add("end")
    def _(event):
        buf = event.app.current_buffer
        buf.cursor_position = len(buf.text)

    @kb.add("q")
    @kb.add("c-c")
    def _(event):
        event.app.exit()

    app = Application(
        layout=Layout(HSplit([body, status]), focused_element=body),
        key_bindings=kb,
        mouse_support=True,   # buffer-backed Window scrolls on the wheel
        full_screen=True,
    )
    return app, body


def _run_pager(lines: list[str], *, title: str) -> None:
    app, _ = _build_pager_app(lines, title=title)
    app.run()
