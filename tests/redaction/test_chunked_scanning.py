"""Boundary, retained-text, worker and global-context contracts for PR #225."""
import hashlib
import json
import random
import re
import sqlite3
import subprocess
import threading

import pytest

from clawjournal.redaction import chunked, pii, secrets
from clawjournal.redaction.boundaries import RedactionBoundaryError


def signature(matches):
    return [(m.group(), m.span(), m.groups(), tuple(m.span(i) for i in range(1, len(m.groups()) + 1)))
            for m in matches]


def local_worker(pattern, flags, chunks):
    compiled = re.compile(pattern, flags)
    return [offset + match.start()
            for source, offset, start, end in chunks
            for match in compiled.finditer(source)
            if start <= offset + match.start() < end]


@pytest.fixture
def small_windows(monkeypatch):
    monkeypatch.setattr(chunked, "CHUNK_SIZE", 64)
    monkeypatch.setattr(chunked, "OVERLAP", 128)
    monkeypatch.setattr(chunked, "PARALLEL_THRESHOLD", 0)
    monkeypatch.setattr(chunked, "_run_worker", local_worker)
    monkeypatch.setattr(chunked, "_CACHE", chunked.OrderedDict())


@pytest.mark.parametrize("value", [
    "alice@example.com", "abc@\r\n", "中文éAlice+tag@sub.example.COM，",
    "abc@x.ab1def@next.example", "aaa@bbb.ccc@ddd.example", "aaa@bbb.ccc@ ",
    "abc@foo.bar+baz@example.com", "email%tag@example.com", "o'connor@example.com",
    "12345678:" + "AbCdEf0123456789_-" * 2,
    "١٢٣٤٥٦٧٨:" + "AbCdEf0123456789_-" * 2,
    "before<one.internal.two.local>after", "db.locality db.local_中文",
    "İntranet.INTERNAL", "db.local\u00a0", "@abc@\u2003next", "abc@invalid",
])
def test_every_character_cut_preserves_original_matches(value, small_windows):
    patterns = [pii._EMAIL_PATTERN, pii._TRUNCATED_EMAIL_PATTERN,
                pii._TELEGRAM_PATTERN, pii._INTERNAL_HOST_PATTERN]
    # Put every character of each sample immediately before a core boundary.
    for cut in range(len(value) + 1):
        text = " " * (128 - cut) + value + " ordinary end " * 12
        for pattern in patterns:
            assert signature(pii._content_matches(pattern, text)) == signature(pattern.finditer(text)), (value, cut)
        pattern = secrets._SECRET_EMAIL_PATTERN
        assert signature(secrets._secret_matches(pattern, text)) == signature(pattern.finditer(text)), (value, cut)


@pytest.mark.parametrize("value,pattern", [
    ("a" * 300 + "@host.test", pii._EMAIL_PATTERN),
    ("alice@" + "a." * 160 + "test", pii._EMAIL_PATTERN),
    ("abc@" + "bbb.ccc@" * 40 + " ", pii._TRUNCATED_EMAIL_PATTERN),
    ("1" * 300 + ":" + "A" * 300, pii._TELEGRAM_PATTERN),
    ("a." * 160 + "local", pii._INTERNAL_HOST_PATTERN),
])
def test_candidate_longer_than_overlap_is_checked_complete(value, pattern, small_windows, monkeypatch):
    def forbidden(*args):
        pytest.fail("An unbounded candidate must not use independent windows")
    monkeypatch.setattr(chunked, "_parallel_starts", forbidden)
    text = "prefix " + value + " suffix"
    assert signature(pii._content_matches(pattern, text)) == signature(pattern.finditer(text))


def test_blank_line_cut_moves_within_bound_and_ownership_covers_once(small_windows):
    text = "A" * 68 + "\n \r\n" + "ordinary " * 80
    windows = list(chunked._windows(text))
    assert windows[0][3] == 72
    previous = 0
    for source, offset, start, end in windows:
        assert start == previous
        assert end - start <= chunked.CHUNK_SIZE + chunked.CHUNK_SIZE // 4
        assert source == text[offset:offset + len(source)]
        assert offset <= start < end <= offset + len(source)
        previous = end
    assert previous == len(text)


def test_real_workers_run_in_two_processes_and_exit(monkeypatch):
    monkeypatch.setattr(chunked, "_CACHE", chunked.OrderedDict())
    processes = []
    original = subprocess.Popen
    lock = threading.Lock()
    def record(*args, **kwargs):
        process = original(*args, **kwargs)
        with lock:
            processes.append(process)
        return process
    monkeypatch.setattr(subprocess, "Popen", record)
    text = "alice@example.com " + "ordinary words " * 800 + "bob@example.com tail"
    assert signature(pii._content_matches(pii._EMAIL_PATTERN, text)) == signature(pii._EMAIL_PATTERN.finditer(text))
    assert len({process.pid for process in processes}) == 2
    assert all(process.poll() == 0 for process in processes)


@pytest.mark.parametrize("response", ["not json", "{}", "[true]", "[-1]", "[99]", "[1, 1]"])
def test_invalid_worker_output_stops_scan(response, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, response))
    with pytest.raises(RedactionBoundaryError, match="chunk_scan_failed"):
        chunked._run_worker("a", 0, [("abc", 0, 0, 3)])


@pytest.mark.parametrize("failure", [
    OSError("SYNTHETIC_SENSITIVE_STDERR"),
    subprocess.TimeoutExpired("worker", 30, stderr="SYNTHETIC_SENSITIVE_STDERR"),
    subprocess.CalledProcessError(1, "worker", stderr="SYNTHETIC_SENSITIVE_STDERR"),
])
def test_worker_failure_is_not_a_clean_verdict_and_does_not_echo_input(failure, monkeypatch):
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(RedactionBoundaryError) as raised:
        chunked._run_worker("a", 0, [("abc", 0, 0, 3)])
    assert "SYNTHETIC_SENSITIVE_STDERR" not in str(raised.value)


def test_one_failed_shard_cannot_publish_other_shard_results(small_windows, monkeypatch):
    def fail_one(pattern, flags, chunks):
        if chunks[0][2] > 0:
            raise RedactionBoundaryError("chunk_scan_failed")
        return local_worker(pattern, flags, chunks)
    monkeypatch.setattr(chunked, "_run_worker", fail_one)
    received = []
    with pytest.raises(RedactionBoundaryError):
        for match in pii._content_matches(pii._EMAIL_PATTERN, "abc@example.com " * 40):
            received.append(match)
    assert received == []
    assert not chunked._CACHE


def test_cache_contains_only_digest_and_offsets_and_is_bounded(small_windows, monkeypatch):
    monkeypatch.setattr(chunked, "_CACHE_ENTRIES", 2)
    for index in range(3):
        text = f"person{index}@example.com " * 30
        list(pii._content_matches(pii._EMAIL_PATTERN, text))
    assert len(chunked._CACHE) == 2
    for key, value in chunked._CACHE.items():
        assert isinstance(key[-1], bytes) and len(key[-1]) == 32
        assert all(type(offset) is int for offset in value)


@pytest.fixture
def builtin_conn(monkeypatch):
    for scanner in ("betterleaks", "trufflehog"):
        monkeypatch.setattr(f"clawjournal.redaction.{scanner}.{scanner}_secret_map_from_blob", lambda *a, **k: {})
    for module in (pii, secrets):
        monkeypatch.setattr(module, "hash_entity", lambda text: hashlib.sha256(text.encode()).hexdigest())
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE findings (session_id TEXT, entity_hash TEXT, status TEXT)")
    yield conn
    conn.close()


@pytest.mark.parametrize("value,expected", [
    ("before<alice@example.com>after", "before<[REDACTED_EMAIL]>after"),
    ("before<alice%40example.com>after", "before<[REDACTED_EMAIL]>after"),
    (r"before<alice\u0040example.com>after", "before<[REDACTED_EMAIL]>after"),
    ("before<alice&#64;example.com>after", "before<[REDACTED_EMAIL]>after"),
    ("before<db01.internal>after", "before<[REDACTED_URL]>after"),
    ("before<12345678:" + "AbCdEf0123456789_-" * 2 + ">after", "before<[REDACTED]>after"),
    ("before<12345678%3A" + "AbCdEf0123456789_-" * 2 + ">after", "before<[REDACTED]>after"),
    ("db.locality and configuration stay", "db.locality and configuration stay"),
])
def test_full_output_preserves_ordinary_text_at_each_cut(value, expected, small_windows, builtin_conn):
    for cut in range(len(value) + 1):
        before, after = " " * (128 - cut), " ordinary words " * 15
        blob, _ = secrets.apply_findings_to_blob(
            {"messages": [{"content": before + value + after}]}, builtin_conn, "chunk-test")
        assert blob["messages"][0]["content"] == before + expected + after


def test_distant_code_context_and_cross_field_credential_evidence_survive(small_windows, builtin_conn):
    code = "import numpy\nimport torch\n" + "# ordinary comment\n" * 20 + "value = numpy.array@torch.tensor"
    original, _ = secrets.apply_findings_to_blob({"messages": [{"content": code}]}, builtin_conn, "chunk-code")
    assert original["messages"][0]["content"] == code
    for messages in ([{"content": 'PASSWORD="numpy.array@torch.tensor"'}, {"content": code}],
                     [{"content": code}, {"content": 'PASSWORD="numpy.array@torch.tensor"'}]):
        result, _ = secrets.apply_findings_to_blob({"messages": messages}, builtin_conn, "chunk-secret")
        assert "numpy.array@torch.tensor" not in json.dumps(result)
        assert "# ordinary comment\n" * 20 in next(m["content"] for m in result["messages"] if "import numpy" in m["content"])


def test_multiline_private_key_spans_many_chunks(small_windows, builtin_conn):
    key = "-----BEGIN RSA PRIVATE KEY-----\n" + "SYNTHETIC_BODY\n" * 80 + "-----END EC PRIVATE KEY-----"
    blob, _ = secrets.apply_findings_to_blob({"messages": [{"content": "before<" + key + ">after"}]}, builtin_conn, "chunk-key")
    assert blob["messages"][0]["content"] == "before<[REDACTED_PRIVATE_KEY]>after"


def test_worker_uses_current_install_when_cwd_contains_another_checkout(tmp_path, monkeypatch):
    # A CLI entry point or embedding caller can import the installed package
    # before visiting a directory with an older/different checkout.
    shadow = tmp_path / "clawjournal"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("raise RuntimeError('WRONG_CHECKOUT')")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(chunked, "_CACHE", chunked.OrderedDict())
    text = "<alice@example.com> " + "ordinary words " * 800 + "<bob@example.com>"
    assert signature(pii._content_matches(pii._EMAIL_PATTERN, text)) == signature(pii._EMAIL_PATTERN.finditer(text))


@pytest.mark.parametrize("length", [1023, 1024, 1025, 8191, 8192, 8193])
def test_marker_free_fields_never_enter_regex_search(length):
    class ForbiddenPattern:
        pattern = pii._EMAIL_PATTERN.pattern
        flags = pii._EMAIL_PATTERN.flags

        def finditer(self, text):
            pytest.fail("A required marker is absent")
    assert list(chunked.finditer(ForbiddenPattern(), "A" * length,
                continuation=lambda *args: pytest.fail("No candidate to continue"),
                has_anchor=lambda text: "@" in text)) == []


@pytest.mark.parametrize("length", [1025, 4096, 8191])
def test_below_process_threshold_still_bounds_search_windows(length, monkeypatch):
    monkeypatch.setattr(chunked, "_run_worker", lambda *args: pytest.fail("Keep this scan local"))
    searches = []
    class RecordingPattern:
        def finditer(self, text):
            searches.append(len(text))
            return pii._EMAIL_PATTERN.finditer(text)
        def match(self, text, start):
            return pii._EMAIL_PATTERN.match(text, start)
    suffix = " <alice@example.com> "
    text = "A" * (length - len(suffix)) + suffix
    assert len(text) == length
    actual = chunked.finditer(RecordingPattern(), text,
                             continuation=lambda *args: pytest.fail("No oversized candidate"),
                             has_anchor=lambda text: "@" in text)
    assert signature(actual) == signature(pii._EMAIL_PATTERN.finditer(text))
    assert searches and max(searches) <= chunked.CHUNK_SIZE + 2 * chunked.OVERLAP


@pytest.mark.parametrize("length", [511, 512, 513])
@pytest.mark.parametrize("kind", ["email", "partial", "telegram", "host"])
def test_production_overlap_limit_and_both_window_edges(length, kind, monkeypatch):
    monkeypatch.setattr(chunked, "_run_worker", local_worker)
    monkeypatch.setattr(chunked, "_CACHE", chunked.OrderedDict())
    suffix = {"email": "@host.test", "partial": "@", "telegram": ":" + "A" * 30, "host": ".internal"}[kind]
    value = ("1" if kind == "telegram" else "a") * (length - len(suffix)) + suffix
    for start in [0, 511, 512, 513, 1023, 1024, 1025, 1535, 1536, 1537, 8191]:
        text = " " * start + value + " ordinary words " * 600
        for pattern in [pii._EMAIL_PATTERN, pii._TRUNCATED_EMAIL_PATTERN, pii._TELEGRAM_PATTERN,
                        pii._INTERNAL_HOST_PATTERN, secrets._SECRET_EMAIL_PATTERN]:
            fn = secrets._secret_matches if pattern == secrets._SECRET_EMAIL_PATTERN else pii._content_matches
            assert signature(fn(pattern, text)) == signature(pattern.finditer(text)), (kind, length, start)


def test_generated_unicode_and_adjacent_candidates_keep_original_spans(small_windows, monkeypatch):
    monkeypatch.setattr(chunked, "OVERLAP", 24)
    rng = random.Random(225)
    alphabet = list("ab19.-_+%@:\n\r\t ") + ["\u00a0", "\u2003", "中", "İ", "ſ", "K", "١", "²", "\x00"]
    values = ["aaa@bbb.ccc@ddd.example", "aa@host.test", "abc@", "db.local", "host.internal.local",
              "１２３４５６７８:" + "A" * 30, "éabc@example.com中", "abc@x.ab1def@next.example"]
    for _ in range(200):
        text = "".join(rng.choices(alphabet, k=rng.randrange(150)))
        for _ in range(rng.randrange(1, 4)):
            at = rng.randrange(len(text) + 1)
            text = text[:at] + rng.choice(values) + text[at:]
        for pattern in [pii._EMAIL_PATTERN, pii._TRUNCATED_EMAIL_PATTERN, pii._TELEGRAM_PATTERN,
                        pii._INTERNAL_HOST_PATTERN, secrets._SECRET_EMAIL_PATTERN]:
            fn = secrets._secret_matches if pattern == secrets._SECRET_EMAIL_PATTERN else pii._content_matches
            assert signature(fn(pattern, text)) == signature(pattern.finditer(text))


def test_real_worker_handles_unicode_offsets_and_non_utf8_codepoints(monkeypatch):
    monkeypatch.setattr(chunked, "_CACHE", chunked.OrderedDict())
    text = "中文😀\ud800" + " ordinary words " * 800 + "<alice@example.com>\r\n"
    assert signature(pii._content_matches(pii._EMAIL_PATTERN, text)) == signature(pii._EMAIL_PATTERN.finditer(text))


def test_more_matches_than_cache_budget_keep_all_results(monkeypatch):
    monkeypatch.setattr(chunked, "_CACHE", chunked.OrderedDict())
    text = "<alice@example.com> " * (chunked._CACHE_MAX_OFFSETS + 1)
    assert signature(pii._content_matches(pii._EMAIL_PATTERN, text)) == signature(pii._EMAIL_PATTERN.finditer(text))
    assert not chunked._CACHE
