"""Narrow Python syntax evidence for email/hostname false positives.

Never exempt a whole line: literals, comments and call arguments still scan.
Parsing is local, bounded, and never executes the supplied source. In
particular, a successful MatMult parse is NOT evidence against an email.
"""
from __future__ import annotations

import ast
import io
import re
import tokenize
from bisect import bisect_right
from dataclasses import dataclass, field
from .replacements import contains_span, merge_spans, replace_spans

_FENCE = re.compile(r"^```(?:python|py)\s*\n(.*?)^```", re.M | re.S)
_HOST_ATTRS = {"local", "internal", "corp", "lan", "intranet", "localnet"}
_HOST_NAMES = {"host", "hostname", "db_host", "database_host", "redis_host", "pghost", "mysql_host", "internal_host", "internal_hostname"}
_ARRAY_MODULES = {"numpy", "torch"}
_TOKEN_NAMES = {"telegram_bot_token", "telegram_api_token", "telegram_token", "telegrambottoken"}
_MAX_PARSE_CHARS = 8 * 1024 * 1024
_MAX_PARSE_TOKENS = 100_000
_MAX_STATEMENT_TOKENS = 2048


@dataclass
class CodeContext:
    protected: list[tuple[int, int]] = field(default_factory=list)
    ambiguous_emails: list[tuple[int, int]] = field(default_factory=list)
    references: list[tuple[int, int]] = field(default_factory=list)
    detected_hosts: set[tuple[int, int]] | None = None

    def protects(self, start: int, end: int) -> bool:
        return contains_span(self.protected, start, end)

    def is_ambiguous(self, start: int, end: int) -> bool:
        return contains_span(self.ambiguous_emails, start, end)

    def is_reference(self, start: int, end: int) -> bool:
        return contains_span(self.references, start, end)

    def apply_edits(self, edits: list[tuple[int, int, str]]) -> None:
        """Move unchanged source evidence after known, disjoint redactions.

        An edit inside an interval invalidates that interval. Never infer new
        code from placeholders or reparse text changed by an earlier finding.
        """
        if not edits:
            return
        ends = [end for _start, end, _replacement in edits]
        shifts = [0]
        for start, end, replacement in edits:
            shifts.append(shifts[-1] + len(replacement) - (end - start))

        def move(spans):
            moved = []
            for start, end in spans:
                i = bisect_right(ends, start)
                if i < len(edits) and edits[i][0] < end:
                    continue
                moved.append((start + shifts[i], end + shifts[i]))
            return moved

        self.protected = move(self.protected)
        self.ambiguous_emails = move(self.ambiguous_emails)
        self.references = move(self.references)
        if self.detected_hosts is not None:
            # Complete host replacements cannot introduce another hostname.
            # Other edits can expose a formerly embedded suffix (e.g. remove
            # a credential prefix from prefixdb.local.example.com). Rescan
            # that changed text instead of reusing an incomplete host set.
            if all((start, end) in self.detected_hosts and replacement == "[REDACTED_URL]"
                   for start, end, replacement in edits):
                self.detected_hosts = set(move(self.detected_hosts))
            else:
                self.detected_hosts = None


def _parse(source: str) -> ast.Module | None:
    from .boundaries import RedactionBoundaryError

    try:
        if len(source) > _MAX_PARSE_CHARS:
            raise RedactionBoundaryError("code_context_budget")
        if len(source) > 65_536:
            # Python warns that large/complex AST input can exhaust stack or
            # memory. Bound complexity before parsing; long comments/strings
            # count as individual tokens and do not disable code protection.
            tokens = statement_tokens = 0
            for item in tokenize.generate_tokens(io.StringIO(source).readline):
                if item.type == tokenize.NEWLINE:
                    statement_tokens = 0
                if item.type in {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                                 tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}:
                    continue
                tokens += 1
                statement_tokens += 1
                if tokens > _MAX_PARSE_TOKENS or statement_tokens > _MAX_STATEMENT_TOKENS:
                    raise RedactionBoundaryError("code_context_budget")
        return ast.parse(source)
    except RedactionBoundaryError:
        raise
    except (RecursionError, MemoryError):
        raise RedactionBoundaryError("code_context_budget") from None
    except (SyntaxError, ValueError, tokenize.TokenError):
        return None


def code_context(text: str) -> CodeContext:
    context = CodeContext()
    # Every supported exemption needs a call, an assignment/dict/keyword, or
    # matrix operators with preceding imports. Bare emails/domain strings
    # cannot establish code evidence and need no Python parser at all.
    if not ("(" in text or "=" in text or ":" in text or ("@" in text and "import" in text)):
        return context
    # Parse the whole source first: a Markdown fence inside a Python string
    # must never turn that string's contents into executable-code evidence.
    tree = _parse(text)
    blocks = [(text, 0, tree)] if tree is not None else [
        (m.group(1), m.start(1), _parse(m.group(1))) for m in _FENCE.finditer(text)
    ]
    for source, base, tree in blocks:
        if "\r" in source.replace("\r\n", ""):
            continue  # Do not infer positions for bare-CR source lines.
        if tree is None:
            continue
        # str.splitlines also splits Unicode separators *inside literals*,
        # which Python does not count as source line breaks.
        lines = source.split("\n")
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line) + 1)

        def span(node: ast.AST) -> tuple[int, int]:
            # AST columns are UTF-8 byte offsets, unlike the scanner's
            # codepoint offsets. Decode the prefix before adding its length.
            def position(line: int, column: int) -> int:
                prefix = lines[line - 1].encode("utf-8")[:column].decode("utf-8")
                return base + offsets[line - 1] + len(prefix)
            return position(node.lineno, node.col_offset), position(node.end_lineno, node.end_col_offset)

        def reference(name: str, value: ast.AST) -> None:
            """Protect reference identifiers, never quoted values or arguments.

            Bare db01 and db01.local can be real host values even in valid
            Python. Require a lookup/call, or an underscore-bearing property
            that the hostname regex would otherwise cut in half.
            """
            name = name.lower()
            if name not in _HOST_NAMES | _TOKEN_NAMES:
                return
            target = None
            if isinstance(value, ast.Subscript):
                target = value.value
            elif isinstance(value, ast.Call):
                target = value.func
                if isinstance(target, ast.Attribute) and target.attr in _HOST_ATTRS:
                    return
            elif isinstance(value, ast.Attribute) and (
                name in _TOKEN_NAMES or "_" in value.attr
            ):
                target = value
            if isinstance(target, (ast.Name, ast.Attribute)):
                start, end = span(target)
                if all(part.isidentifier() for part in text[start:end].split(".")):
                    context.references.append((start, end))

        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        reference(target.id, node.value)
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        reference(key.value, value)
            elif isinstance(node, ast.keyword) and node.arg:
                reference(node.arg, node.value)

        # Only preceding top-level imports establish array module names.
        imported: set[str] = set()
        for statement in tree.body:
            if isinstance(statement, ast.Import):
                imported.update(a.asname or a.name for a in statement.names if a.name in _ARRAY_MODULES)
            host_assignment = isinstance(statement, (ast.Assign, ast.AnnAssign)) and any(
                isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id.lower() in _HOST_NAMES
                for n in ast.walk(statement)
            )
            for node in ast.walk(statement):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in _HOST_ATTRS and not host_assignment:
                        start, end = span(node.func)
                        if all(part.isidentifier() for part in text[start:end].split(".")):
                            context.protected.append((start, end))
                if isinstance(node, ast.BinOp) and isinstance(node.op, ast.MatMult):
                    if isinstance(node.left, ast.Attribute) and isinstance(node.right, ast.Attribute):
                        left, right = node.left, node.right
                        if (isinstance(left.value, ast.Name) and left.value.id in imported
                                and isinstance(right.value, ast.Name) and right.value.id in imported):
                            start, end = span(node)
                            # Do not cover comments, literals or arguments
                            # embedded in a multiline expression.
                            if re.fullmatch(r"[\w.]+@[\w.]+", text[start:end]):
                                context.protected.append((start, end))
                        elif isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is node:
                            context.ambiguous_emails.append(span(node))
    context.protected = merge_spans(context.protected)
    context.ambiguous_emails = merge_spans(context.ambiguous_emails)
    context.references = merge_spans(context.references)
    return context


def replace_outside_code(text: str, pattern: re.Pattern, replacement: str, *,
                         context: CodeContext | None = None) -> tuple[str, int]:
    """Apply a known email/hostname entity without deleting syntax elsewhere."""
    if context is None:
        context = code_context(text)
    def host_boundary(start: int, end: int) -> bool:
        # A known db.local must not eat db.locality, nor a contextual db01
        # eat mydb01. Allow a sentence's final dot, but not another DNS label.
        chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-."
        if start and text[start - 1] in chars:
            if text[start - 1] != "." or (start > 1 and text[start - 2] in chars):
                return False
        if end < len(text) and text[end] in chars:
            if text[end] != "." or (end + 1 < len(text) and text[end + 1] in chars):
                return False
        return True

    spans = []
    for match in pattern.finditer(text):
        start, end = match.span()
        if context.protects(start, end):
            continue
        if replacement == "[REDACTED_URL]" and not host_boundary(start, end):
            # Keep an explicitly detected occurrence (including legacy
            # subdomain spans). The guard restricts propagation to unrelated
            # text; it must never silently drop the source finding itself.
            if context.detected_hosts is None:
                from .pii import scan_text_for_pii
                context.detected_hosts = {(m["start"], m["end"]) for m in scan_text_for_pii(text)
                                          if m["type"] == "private_url"}
            if (start, end) not in context.detected_hosts:
                continue
        spans.append((start, end, replacement))
    return replace_spans(text, spans, context=context)
