"""Tests for the DB-backed findings substrate in clawjournal.findings."""

from __future__ import annotations

import pytest

from clawjournal import findings as findings_mod
from clawjournal.findings import (
    ENGINE_VERSION,
    Finding,
    RawFinding,
    allowlist_add,
    allowlist_list,
    allowlist_remove,
    compute_finding_id,
    compute_findings_revision,
    dedupe_findings_by_entity,
    derive_preview,
    get_enabled_engines,
    hash_entity,
    load_findings_from_db,
    reset_salt_cache,
    set_finding_status,
    write_findings_to_db,
)
from clawjournal.workbench.index import open_index, upsert_sessions


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
    reset_salt_cache()
    connection = open_index()
    yield connection
    connection.close()
    reset_salt_cache()


def _seed_session(conn, session_id="sess-1"):
    upsert_sessions(conn, [{
        "session_id": session_id,
        "project": "p",
        "source": "claude",
        "model": "claude-sonnet-4",
        "start_time": "2025-01-01T00:00:00+00:00",
        "end_time": "2025-01-01T00:10:00+00:00",
        "git_branch": "main",
        "messages": [{"role": "user", "content": "hi", "tool_uses": []}],
        "stats": {"user_messages": 1, "assistant_messages": 0, "tool_uses": 0,
                  "input_tokens": 1, "output_tokens": 0},
    }])
    conn.commit()


def _raw(engine="regex_secrets", rule="jwt", entity_type="jwt",
         entity_text="eyJhbGciOi.payload.signature",
         field="content", offset=0, length=None, confidence=0.98,
         message_index=0, tool_field=None):
    if length is None:
        length = len(entity_text)
    return RawFinding(
        engine=engine, rule=rule, entity_type=entity_type,
        entity_text=entity_text, field=field, offset=offset, length=length,
        confidence=confidence, message_index=message_index, tool_field=tool_field,
    )


class TestHashEntity:
    def test_salt_changes_hash_across_installs(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "a" / "index.db")
        reset_salt_cache()
        open_index().close()
        hash_a = hash_entity("secret-token")

        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "b" / "index.db")
        reset_salt_cache()
        open_index().close()
        hash_b = hash_entity("secret-token")

        assert hash_a != hash_b
        assert len(hash_a) == 64
        assert len(hash_b) == 64

    def test_hash_is_deterministic_within_install(self, conn):
        assert hash_entity("x") == hash_entity("x")
        assert hash_entity("x") != hash_entity("y")


class TestFindingID:
    def test_deterministic_across_calls(self):
        args = dict(
            session_id="s1", revision="v1:abc", engine="regex_secrets",
            rule="jwt", field="content", message_index=0, tool_field=None,
            offset=10, length=20,
        )
        assert compute_finding_id(**args) == compute_finding_id(**args)

    def test_any_field_change_shifts_id(self):
        base = dict(
            session_id="s1", revision="v1:abc", engine="regex_secrets",
            rule="jwt", field="content", message_index=0, tool_field=None,
            offset=10, length=20,
        )
        baseline = compute_finding_id(**base)
        for key, new_val in [
            ("session_id", "s2"), ("revision", "v1:xyz"), ("engine", "regex_pii"),
            ("rule", "email"), ("field", "thinking"), ("message_index", 1),
            ("tool_field", "input"), ("offset", 11), ("length", 21),
        ]:
            shifted = compute_finding_id(**{**base, key: new_val})
            assert shifted != baseline, f"{key} should participate in the hash"


class TestComputeFindingsRevision:
    def _session(self, **overrides):
        base = {
            "display_title": "t", "project": "p", "git_branch": "main",
            "messages": [
                {"role": "user", "content": "hi", "thinking": "",
                 "tool_uses": [{"tool": "bash", "input": {"cmd": "ls"},
                                "output": "file.txt"}]}
            ],
        }
        base.update(overrides)
        return base

    def test_stable_under_identical_input(self):
        s = self._session()
        assert compute_findings_revision(s) == compute_findings_revision(s)

    def test_engine_version_participates(self, monkeypatch):
        s = self._session()
        rev1 = compute_findings_revision(s)
        monkeypatch.setattr("clawjournal.findings.ENGINE_VERSION", ENGINE_VERSION + 1)
        rev2 = compute_findings_revision(s)
        assert rev1 != rev2

    def test_enabled_engines_participates(self):
        s = self._session()
        rev_default = compute_findings_revision(s)
        rev_subset = compute_findings_revision(s, config={
            "enabled_findings_engines": ["regex_secrets"],
        })
        assert rev_default != rev_subset

    def test_allowlist_entries_participate(self):
        s = self._session()
        rev_empty = compute_findings_revision(s, config={"allowlist_entries": []})
        rev_one = compute_findings_revision(
            s, config={"allowlist_entries": [{"type": "email", "match": "noreply@x"}]},
        )
        assert rev_empty != rev_one

    def test_blob_change_flips_revision(self):
        rev1 = compute_findings_revision(self._session())
        rev2 = compute_findings_revision(self._session(project="p2"))
        assert rev1 != rev2

    def test_betterleaks_fingerprint_participates(self, monkeypatch):
        # Installing/upgrading the betterleaks binary must invalidate
        # cached findings, same as the trufflehog fingerprint fold.
        s = self._session()
        monkeypatch.setattr(
            "clawjournal.redaction.betterleaks.engine_fingerprint",
            lambda: "betterleaks 1.6.1",
        )
        rev1 = compute_findings_revision(s)
        monkeypatch.setattr(
            "clawjournal.redaction.betterleaks.engine_fingerprint",
            lambda: "betterleaks 1.7.0",
        )
        rev2 = compute_findings_revision(s)
        assert rev1 != rev2

    def test_format_prefix(self):
        assert compute_findings_revision(self._session()).startswith("v1:")


class TestWriteFindingsToDB:
    def test_inserts_hash_only(self, conn):
        _seed_session(conn)
        count = write_findings_to_db(
            conn, "sess-1",
            [_raw(entity_text="my-super-secret-token-value")],
            revision="v1:rev",
        )
        conn.commit()
        assert count == 1

        row = conn.execute("SELECT * FROM findings").fetchone()
        # No plaintext anywhere.
        assert "my-super-secret-token-value" not in str(dict(row))
        assert row["entity_hash"] == hash_entity("my-super-secret-token-value")
        assert row["entity_length"] == len("my-super-secret-token-value")
        assert row["status"] == "open"
        assert row["decided_by"] == "auto"
        assert row["decision_source_id"] is None
        assert row["revision"] == "v1:rev"

    def test_allowlist_auto_ignores_on_write(self, conn):
        _seed_session(conn)
        entry, _, _ = allowlist_add(
            conn,
            entity_text="noreply@example.com",
            entity_type="email",
            reason="service addr",
        )
        conn.commit()
        write_findings_to_db(
            conn, "sess-1",
            [_raw(engine="regex_secrets", rule="email", entity_type="email",
                  entity_text="noreply@example.com")],
            revision="v1:rev2",
        )
        conn.commit()
        row = conn.execute("SELECT * FROM findings").fetchone()
        assert row["status"] == "ignored"
        assert row["decided_by"] == "allowlist"
        assert row["decision_source_id"] == entry["allowlist_id"]
        assert row["decision_reason"] == "service addr"

    def test_null_type_allowlist_matches_any_type(self, conn):
        _seed_session(conn)
        allowlist_add(
            conn,
            entity_text="danger-string",
            entity_type=None,   # matches any type
            reason="generic",
        )
        conn.commit()
        write_findings_to_db(
            conn, "sess-1",
            [_raw(entity_type="custom", entity_text="danger-string")],
            revision="v1:rev",
        )
        conn.commit()
        assert conn.execute(
            "SELECT status FROM findings"
        ).fetchone()["status"] == "ignored"


class TestSetFindingStatus:
    def test_fans_out_to_entity_group(self, conn):
        _seed_session(conn)
        write_findings_to_db(
            conn, "sess-1",
            [
                _raw(entity_text="same-secret", offset=0, length=11),
                _raw(entity_text="same-secret", offset=50, length=11),
                _raw(entity_text="other", offset=100, length=5),
            ],
            revision="v1:rev",
        )
        conn.commit()
        first = conn.execute(
            "SELECT finding_id FROM findings WHERE offset = 0"
        ).fetchone()["finding_id"]
        updated = set_finding_status(conn, [first], "accepted", reason="mine")
        conn.commit()
        statuses = {
            r["offset"]: r["status"]
            for r in conn.execute("SELECT offset, status FROM findings").fetchall()
        }
        assert statuses[0] == "accepted"
        assert statuses[50] == "accepted"   # same entity hash, fanned out
        assert statuses[100] == "open"      # different entity
        assert updated == 2

    def test_also_allowlist_propagates_to_other_sessions(self, conn):
        _seed_session(conn, "sess-1")
        _seed_session(conn, "sess-2")
        write_findings_to_db(
            conn, "sess-1",
            [_raw(entity_text="shared-secret", offset=0)],
            revision="v1:s1",
        )
        write_findings_to_db(
            conn, "sess-2",
            [_raw(entity_text="shared-secret", offset=0)],
            revision="v1:s2",
        )
        conn.commit()
        s1_finding = conn.execute(
            "SELECT finding_id FROM findings WHERE session_id='sess-1'"
        ).fetchone()["finding_id"]
        set_finding_status(
            conn, [s1_finding], "ignored",
            reason="team-wide", also_allowlist=True,
        )
        conn.commit()
        rows = conn.execute(
            "SELECT session_id, status, decided_by FROM findings"
        ).fetchall()
        for row in rows:
            assert row["status"] == "ignored"
        # The second session's row was retroactively flipped via the allowlist.
        s2 = next(r for r in rows if r["session_id"] == "sess-2")
        assert s2["decided_by"] == "allowlist"


class TestDedupe:
    def test_groups_and_sorts_by_confidence(self, conn):
        _seed_session(conn)
        write_findings_to_db(
            conn, "sess-1",
            [
                _raw(entity_text="a", offset=0, confidence=0.8),
                _raw(entity_text="a", offset=10, confidence=0.9),
                _raw(entity_text="b", offset=20, confidence=0.5),
            ],
            revision="v1:r",
        )
        conn.commit()
        groups = dedupe_findings_by_entity(load_findings_from_db(conn, "sess-1"))
        assert len(groups) == 2
        # Highest-confidence group first.
        assert groups[0]["entity_hash"] == hash_entity("a")
        assert groups[0]["occurrences"] == 2
        assert groups[0]["max_confidence"] == 0.9
        assert groups[1]["entity_hash"] == hash_entity("b")
        assert groups[1]["occurrences"] == 1


class TestDerivePreview:
    def test_extracts_context_from_message_content(self, conn):
        # "Authorization: Bearer TOKEN123 hello"; "TOKEN123" is at offset 22,
        # length 8. context_chars=10 yields 10 code points either side.
        finding = Finding(
            finding_id="f1", session_id="s", engine="e", rule="r",
            entity_type="t", entity_hash="h", entity_length=8,
            field="content", message_index=0, tool_field=None,
            offset=22, length=8, confidence=1.0,
            status="open", decided_by=None, decision_source_id=None,
            decided_at=None, decision_reason=None,
            revision="v1:x", created_at="now",
        )
        blob = {"messages": [{"role": "user", "content": "Authorization: Bearer TOKEN123 hello"}]}
        preview = derive_preview(blob, finding, context_chars=10)
        # Match text itself is never returned.
        assert "TOKEN1" not in preview["before"]
        assert "TOKEN1" not in preview["after"]
        assert preview["before"].endswith("Bearer ")
        assert preview["after"].startswith(" hello")
        assert preview["match_placeholder"] == "[...]"

    def test_astral_chars_respected_as_code_points(self, conn):
        """Python str indexing is code-point based; emojis must not misalign."""
        finding = Finding(
            finding_id="f1", session_id="s", engine="e", rule="r",
            entity_type="t", entity_hash="h", entity_length=8,
            field="content", message_index=0, tool_field=None,
            offset=3, length=8, confidence=1.0,
            status="open", decided_by=None, decision_source_id=None,
            decided_at=None, decision_reason=None,
            revision="v1:x", created_at="now",
        )
        # "🔑 " is 2 code points; "sk_12345" starts at offset 3.
        blob = {"messages": [{"role": "user", "content": "🔑 xsk_12345tail"}]}
        preview = derive_preview(blob, finding, context_chars=5)
        assert preview["before"].endswith("🔑 x")
        assert preview["after"].startswith("tail")

    def test_resolves_widened_message_field(self, conn):
        """A finding whose field is a widened-model label (e.g. ``author``)
        must resolve to real preview context, not empty strings."""
        finding = Finding(
            finding_id="f1", session_id="s", engine="trufflehog", rule="r",
            entity_type="r", entity_hash="h", entity_length=6,
            field="author", message_index=0, tool_field=None,
            offset=4, length=6, confidence=1.0,
            status="open", decided_by=None, decision_source_id=None,
            decided_at=None, decision_reason=None,
            revision="v1:x", created_at="now",
        )
        # "pre SECRET post": SECRET at offset 4, length 6.
        blob = {"messages": [{"role": "user", "content": "x", "author": "pre SECRET post"}]}
        preview = derive_preview(blob, finding, context_chars=10)
        assert preview["before"].endswith("pre ")
        assert preview["after"].startswith(" post")
        assert preview["match_placeholder"] == "[...]"


class TestAllowlistRetroactive:
    def test_add_flips_existing_open_findings(self, conn):
        _seed_session(conn, "sess-a")
        _seed_session(conn, "sess-b")
        write_findings_to_db(
            conn, "sess-a", [_raw(entity_text="E", offset=0)], revision="v1:a",
        )
        write_findings_to_db(
            conn, "sess-b", [_raw(entity_text="E", offset=0)], revision="v1:b",
        )
        conn.commit()
        entry, updates, sessions = allowlist_add(
            conn, entity_text="E", entity_type="jwt", reason="example",
        )
        conn.commit()
        assert updates == 2
        assert sessions == 2
        statuses = {
            r["session_id"]: r["status"]
            for r in conn.execute("SELECT session_id, status FROM findings").fetchall()
        }
        assert statuses == {"sess-a": "ignored", "sess-b": "ignored"}

    def test_remove_reverts_when_no_other_match(self, conn):
        _seed_session(conn)
        entry, _, _ = allowlist_add(
            conn, entity_text="E", entity_type="jwt", reason="oops",
        )
        write_findings_to_db(
            conn, "sess-1", [_raw(entity_text="E", offset=0)], revision="v1:r",
        )
        conn.commit()
        removed, reverted, reassigned = allowlist_remove(conn, entry["allowlist_id"])
        conn.commit()
        assert removed is True
        assert reverted == 1
        assert reassigned == 0
        row = conn.execute("SELECT status, decided_by FROM findings").fetchone()
        assert row["status"] == "open"
        assert row["decided_by"] == "auto"

    def test_remove_reassigns_when_another_match_remains(self, conn):
        _seed_session(conn)
        specific, _, _ = allowlist_add(
            conn, entity_text="E", entity_type="jwt", reason="team",
        )
        # Second "any-type" entry for the same hash remains after we remove
        # the typed one.
        any_type, _, _ = allowlist_add(
            conn, entity_text="E", entity_type=None, reason="generic",
        )
        write_findings_to_db(
            conn, "sess-1", [_raw(entity_text="E", offset=0)], revision="v1:r",
        )
        conn.commit()
        # Finding was auto-ignored by the typed entry on write.
        row = conn.execute("SELECT decision_source_id FROM findings").fetchone()
        assert row["decision_source_id"] == specific["allowlist_id"]

        removed, reverted, reassigned = allowlist_remove(conn, specific["allowlist_id"])
        conn.commit()
        assert removed is True
        assert reverted == 0
        assert reassigned == 1
        # Finding stayed ignored but moved to the any-type entry.
        row = conn.execute(
            "SELECT status, decision_source_id, decision_reason FROM findings"
        ).fetchone()
        assert row["status"] == "ignored"
        assert row["decision_source_id"] == any_type["allowlist_id"]
        assert row["decision_reason"] == "generic"

    def test_list_hides_hash(self, conn):
        allowlist_add(conn, entity_text="secret-x", entity_label="team bot",
                      entity_type="email", reason="r")
        conn.commit()
        entries = allowlist_list(conn)
        assert len(entries) == 1
        assert "entity_hash" not in entries[0]
        assert entries[0]["entity_label"] == "team bot"


class TestEnabledEngines:
    DEFAULT = ("betterleaks", "regex_pii", "regex_secrets")

    def test_defaults_to_all_shipped_engines(self):
        # Betterleaks replaced trufflehog in the default set when it
        # became the share gate's primary scanner; trufflehog remains
        # an explicit opt-in.
        assert get_enabled_engines(None) == self.DEFAULT
        assert get_enabled_engines({}) == self.DEFAULT

    def test_returns_sorted_tuple(self):
        result = get_enabled_engines({"enabled_findings_engines": ["betterleaks", "regex_secrets", "regex_pii"]})
        assert result == self.DEFAULT

    def test_trufflehog_opt_in_is_preserved(self):
        result = get_enabled_engines(
            {"enabled_findings_engines": ["trufflehog", "regex_secrets", "betterleaks"]}
        )
        assert result == ("betterleaks", "regex_secrets", "trufflehog")

    def test_rejects_non_list(self):
        # Non-list config is treated as "use defaults".
        assert get_enabled_engines({"enabled_findings_engines": "regex_secrets"}) == self.DEFAULT
