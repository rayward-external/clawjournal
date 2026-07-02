"""Durable state (§9): fingerprint, upsert, reject-never-reproposed, install."""

from clawjournal.skill import store
from clawjournal.skill.schema import SkillRule


def _r(g="run the test suite first", kind="avoid", support=0):
    return SkillRule(kind=kind, trigger="t", guidance=g, why="w", support=support,
                     evidence_session_ids=["s1"])


def test_install_preserves_carried_rule_last_seen(index_conn):
    # #0: installing a carried-over rule (last_seen already set, not re-distilled this
    # run) must NOT reset its decay clock, or a stale rule pins itself in forever.
    carried = _r("carried", support=5)
    carried.last_seen = "2026-01-01T00:00:00+00:00"
    store.mark_installed(index_conn, [carried], now="2026-06-01T00:00:00+00:00")
    row = index_conn.execute("SELECT last_seen_at FROM skill_rules WHERE guidance='carried'").fetchone()
    assert row["last_seen_at"] == "2026-01-01T00:00:00+00:00"     # preserved, not bumped to now


def test_install_stamps_fresh_rule_last_seen_now(index_conn):
    fresh = _r("fresh", support=3)                                # last_seen == "" -> seen now
    store.mark_installed(index_conn, [fresh], now="2026-06-01T00:00:00+00:00")
    row = index_conn.execute("SELECT last_seen_at FROM skill_rules WHERE guidance='fresh'").fetchone()
    assert row["last_seen_at"] == "2026-06-01T00:00:00+00:00"


def test_fingerprint_stable_and_distinct():
    assert store.fingerprint(_r("Run  the Test  Suite First")) == store.fingerprint(_r("run the test suite first"))
    assert store.fingerprint(_r()) != store.fingerprint(_r("do a thing", kind="do"))


def test_upsert_and_load_kept(index_conn):
    store.upsert_seen(index_conn, _r(support=3))
    kept = store.load_kept(index_conn)
    assert len(kept) == 1 and kept[0].support == 3


def test_upsert_refreshes_support(index_conn):
    store.upsert_seen(index_conn, _r(support=2))
    store.upsert_seen(index_conn, _r(support=5))
    assert store.load_kept(index_conn)[0].support == 5


def test_reject_hides_and_persists(index_conn):
    fp = store.upsert_seen(index_conn, _r())
    assert store.reject(index_conn, fp)
    assert fp in store.rejected_fingerprints(index_conn)
    assert store.load_kept(index_conn) == []           # rejected is not active
    # a re-seen rejected rule stays rejected (never re-proposed)
    store.upsert_seen(index_conn, _r())
    assert store.load_kept(index_conn) == []


def test_mark_installed_sets_state(index_conn):
    r = _r()
    store.mark_installed(index_conn, [r])
    assert store.fingerprint(r) in store.installed_fingerprints(index_conn)


def test_mark_installed_drops_rules_not_in_new_set(index_conn):
    old = _r("old weak rule")
    keep = _r("kept strong rule")
    store.mark_installed(index_conn, [old, keep])
    store.mark_installed(index_conn, [keep])

    kept_guidance = {r.guidance for r in store.load_kept(index_conn)}
    assert kept_guidance == {"kept strong rule"}
    assert store.fingerprint(old) not in store.installed_fingerprints(index_conn)
