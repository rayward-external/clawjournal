"""Narrow Python syntax evidence for email/hostname false positives.

Never exempt a whole line: literals, comments and call arguments still scan.
Parsing is local, bounded, and never executes the supplied source. In
particular, a successful MatMult parse is NOT evidence against an email.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from .replacements import contains_span, merge_spans, replace_spans

_FENCE = re.compile(r"^```(?:python|py)\s*\n(.*?)^```", re.M | re.S)
_HOST_ATTRS = {"local", "internal", "corp", "lan", "intranet", "localnet"}
_HOST_NAMES = {"host", "hostname", "db_host", "database_host", "redis_host", "pghost", "mysql_host", "internal_host", "internal_hostname"}
_ARRAY_MODULES = {"numpy", "torch"}


@dataclass
class CodeContext:
    protected: list[tuple[int, int]] = field(default_factory=list)
    ambiguous_emails: list[tuple[int, int]] = field(default_factory=list)

    def protects(self, start: int, end: int) -> bool:
        return contains_span(self.protected, start, end)

    def is_ambiguous(self, start: int, end: int) -> bool:
        return contains_span(self.ambiguous_emails, start, end)


def _parse(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None


def code_context(text: str) -> CodeContext:
    context = CodeContext()
    if len(text) > 65_536 or ("@" not in text and not any("." + s in text for s in _HOST_ATTRS)):
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
    return context


def replace_outside_code(text: str, pattern: re.Pattern, replacement: str) -> tuple[str, int]:
    """Apply a known email/hostname entity without deleting syntax elsewhere."""
    context = code_context(text)
    spans = [(m.start(), m.end(), replacement) for m in pattern.finditer(text) if not context.protects(m.start(), m.end())]
    return replace_spans(text, spans)
