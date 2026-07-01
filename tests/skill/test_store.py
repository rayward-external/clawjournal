"""Durable state (§9): fingerprint, upsert, reject-never-reproposed, install."""

from clawjournal.skill import store
from clawjournal.skill.schema import SkillRule


def _r(g="run the test suite first", kind="avoid", support=0):
    return SkillRule(kind=kind, trigger="t", guidance=g, why="w", support=support,
                     evidence_session_ids=["s1"])


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
