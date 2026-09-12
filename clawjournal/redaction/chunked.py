"""Parallel windows for the existing local, non-whitespace regex rules.

Borrow overlapping windows from TruffleHog and bounded blank-line cut
adjustment from Gitleaks. This is not a new external scanner or a change
to the regex definitions. Other rules must not use this adapter without
proving their context requirements.

A fixed overlap cannot cover an unbounded candidate. Such candidates use
the complete-text continuation adapter; they are never truncated, skipped,
or allowed through merely because each window looks harmless. Private keys
and contextual parsing remain outside this module.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import threading
from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CHUNK_SIZE = 1024
OVERLAP = 512
PARALLEL_THRESHOLD = 8192
# Count selected window characters, including overlap. Starting processes for
# a few cheap windows costs more than the actual search (e.g. 300 tokens).
MIN_PARALLEL_WORK = 128 * 1024
MAX_WORKERS = 2
WORKER_TIMEOUT = 30
_BLANK_LINE = re.compile(r"\n[ \t\r]*\n")
_WORKER_SLOTS = threading.BoundedSemaphore(MAX_WORKERS)
_CACHE_LOCK = threading.Lock()
# Only offsets and a digest are retained, never source text or matched values.
# Filtering, review decisions and code context are re-evaluated by the caller.
_CACHE: OrderedDict[tuple, tuple[int, ...]] = OrderedDict()
_CACHE_ENTRIES = 16
_CACHE_MAX_OFFSETS = 10_000


def _windows(text: str) -> Iterable[tuple[str, int, int, int]]:
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        # Gitleaks' idea: prefer a nearby blank line, with a strict read-ahead
        # limit. A blank line is only a cut preference, never a safety proof.
        blank = _BLANK_LINE.search(text, end, min(end + CHUNK_SIZE // 4, len(text)))
        if blank is not None:
            end = blank.end()
        left, right = max(0, start - OVERLAP), min(len(text), end + OVERLAP)
        yield text[left:right], left, start, end
        start = end


def _run_worker(pattern: str, flags: int, chunks: list[tuple]) -> list[int]:
    from .boundaries import RedactionBoundaryError

    payload = json.dumps({"pattern": pattern, "flags": flags, "chunks": chunks})
    try:
        with _WORKER_SLOTS:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("chunked_worker.py"))],
                input=payload, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", timeout=WORKER_TIMEOUT, check=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        offsets = json.loads(result.stdout)
        if not isinstance(offsets, list) or any(type(value) is not int for value in offsets):
            raise ValueError("Invalid worker response")
        owned = [(chunk[2], chunk[3]) for chunk in chunks]
        owned_starts = [start for start, _ in owned]
        def owns(value: int) -> bool:
            index = bisect_right(owned_starts, value) - 1
            return index >= 0 and value < owned[index][1]
        # Validate ordering and ownership before accepting a result. Empty
        # successful output is valid; failed or malformed output is not.
        if offsets != sorted(set(offsets)) or any(
            not owns(value) for value in offsets
        ):
            raise ValueError("Invalid worker offsets")
        return offsets
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        # Do not expose stderr, the input, a candidate, or a hash in an error.
        raise RedactionBoundaryError("chunk_scan_failed") from None


def _parallel_starts(pattern: re.Pattern[str], text: str, has_anchor: Callable[[str], bool]) -> tuple[int, ...]:
    key = (pattern.pattern, pattern.flags, hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).digest())
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
            return cached
    shards: list[list[tuple]] = [[] for _ in range(MAX_WORKERS)]
    selected = 0
    for window in _windows(text):
        # A mandatory literal only rejects an impossible window. It does
        # not pick match starts or replace the existing regex search.
        if has_anchor(window[0]):
            shards[selected % MAX_WORKERS].append(window)
            selected += 1
    work = sum(len(window[0]) for shard in shards for window in shard)
    if work < MIN_PARALLEL_WORK:
        from .chunked_worker import match_starts

        starts = tuple(sorted({value for shard in shards for value in match_starts(pattern, shard)}))
    else:
        # Threads coordinate independent processes; expensive searches can
        # use separate CPU cores despite Python re holding the GIL.
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = [pool.submit(_run_worker, pattern.pattern, pattern.flags, shard)
                       for shard in shards if shard]
            starts = tuple(sorted({value for future in futures for value in future.result()}))
    if len(starts) <= _CACHE_MAX_OFFSETS:
        with _CACHE_LOCK:
            _CACHE[key] = starts
            _CACHE.move_to_end(key)
            while len(_CACHE) > _CACHE_ENTRIES:
                _CACHE.popitem(last=False)
    return starts


def finditer(
    pattern: re.Pattern[str], text: str, *,
    continuation: Callable[[re.Pattern[str], str], Iterable[re.Match[str]]],
    has_anchor: Callable[[str], bool],
) -> Iterable[re.Match[str]]:
    """Run unchanged regexes in parallel; return matches on the original text.

    Only registered non-whitespace rules call this. If an anchor-bearing run
    exceeds the overlap, retain complete-text handling. This also preserves
    greedy and successive-match behavior for chains of adjoining candidates.
    """
    # Reject impossible fields before the size threshold too. Otherwise a
    # marker-free run just below that threshold still takes quadratic time.
    if not has_anchor(text):
        return
    if any(has_anchor(run.group()) for run in re.finditer(r"\S{%d,}" % (OVERLAP + 1), text)):
        yield from continuation(pattern, text)
        return
    if len(text) <= CHUNK_SIZE:
        yield from pattern.finditer(text)
        return
    if len(text) < PARALLEL_THRESHOLD:
        from .chunked_worker import match_starts

        # Bound searches below the process threshold as well. Keep small
        # fields local to avoid worker startup overhead.
        starts = match_starts(pattern, (window for window in _windows(text) if has_anchor(window[0])))
    else:
        starts = _parallel_starts(pattern, text, has_anchor)
    consumed = 0
    for start in starts:
        if start < consumed:
            continue
        # Recheck against original neighbors, never synthetic window edges.
        # Return real re.Match objects with original codepoint/group offsets.
        match = pattern.match(text, start)
        if match is not None:
            yield match
            consumed = match.end()
