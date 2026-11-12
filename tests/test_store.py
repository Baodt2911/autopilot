import multiprocessing as mp
from datetime import date, datetime, timedelta, timezone

import pytest

from autopilot.models import Event
from autopilot.store import InvalidTransition, ItemClosed, Store

T0 = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)


def m(n):
    return timedelta(minutes=n)


def ev(i, pr=1, status_kind="thread", prio=4):
    return Event(id=i, pr=pr, kind=status_kind, priority=prio, created_at=T0, payload={"k": i})


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "s.db")


def test_add_event_dedupes(store):
    assert store.add_event(ev("c1")) and not store.add_event(ev("c1"))


def test_events_roundtrip_filter_and_mark(store):
    store.add_event(ev("c1", pr=1))
    store.add_event(ev("c2", pr=2))
    got = store.events(pr=1)
    assert got == [ev("c1", pr=1)] and got[0].created_at.tzinfo is not None
    store.mark_events(["c1"], "batched")
    assert [e.id for e in store.events(status="new")] == ["c2"]
    assert [e.id for e in store.events(status="batched")] == ["c1"]
    with pytest.raises(ValueError):
        store.mark_events(["c1"], "bogus")


def test_batch_lifecycle_and_lease(store):
    store.add_event(ev("c1"))
    b = store.create_batch(589, ["c1"], T0)
    assert store.batch(b)["status"] == "new" and store.batch(b)["event_ids"] == ["c1"]
    store.transition(b, "delivered", T0, session="s1")
    assert store.batch(b)["session"] == "s1"
    store.transition(b, "acked", T0 + m(1))
    assert store.batch(b)["lease_until"] == T0 + m(61)
    with pytest.raises(InvalidTransition):
        store.transition(b, "done", T0)
    assert [x["id"] for x in store.lease_expired(T0 + m(62))] == [b]
    assert store.lease_expired(T0 + m(60)) == []
    store.transition(b, "in_progress", T0 + m(30))
    assert store.batch(b)["lease_until"] == T0 + m(90)
    assert [x["id"] for x in store.batches(status=("in_progress", "acked"))] == [b]
    assert store.batches(status="done") == [] and len(store.batches(pr=589)) == 1


def test_resend_counts_attempts_and_unacked(store):
    b = store.create_batch(1, ["c1"], T0)
    store.transition(b, "delivered", T0)
    assert store.unacked(T0 + m(14)) == [] and store.unacked(T0 + m(15))[0]["id"] == b
    store.transition(b, "delivered", T0 + m(15))
    assert store.batch(b)["attempts"] == 2


def test_item_codes_increment_per_project(store):
    assert store.add_item("Q", 1, 1, "t", "b", T0) == "Q-ivt-1"
    assert store.add_item("P", 1, 1, "t", "b", T0) == "P-ivt-2"
    with pytest.raises(ValueError):
        store.add_item("X", 1, 1, "t", "b", T0)


def test_item_resolve_and_errors(store):
    c = store.add_item("Q", 1, 1, "t", "b", T0)
    assert [i["code"] for i in store.open_items()] == [c]
    store.mark_reminded(c, T0 + m(5))
    assert store.item(c)["reminded_at"] == T0 + m(5)
    r = store.resolve_item(c, "yes", "n", "hint", T0 + m(6))
    assert r["answer"] == "yes" and r["status"] == "closed"
    assert store.open_items() == []
    with pytest.raises(ItemClosed):
        store.resolve_item(c, "again", "", "", T0)
    with pytest.raises(KeyError):
        store.resolve_item("Q-ivt-99", "x", "", "", T0)
    assert store.item("nope") is None


def test_counters_kv_snapshots_log(store):
    assert [store.bump_round("t1"), store.bump_round("t1"), store.bump_round("t2")] == [1, 2, 1]
    d = date(2026, 11, 12)
    assert [store.bump_wake(5, d), store.bump_wake(5, d), store.bump_wake(5, date(2026, 11, 13))] == [1, 2, 1]
    assert store.kv_get("paused") is None
    store.kv_set("paused", "1")
    assert store.kv_get("paused") == "1"
    store.kv_set("paused", None)
    assert store.kv_get("paused") is None
    store.save_snapshot(3, {"a": 1})
    store.save_snapshot(3, {"a": 2})
    assert store.snapshot(3) == {"a": 2} and store.snapshot(4) is None and store.snapshots() == {3: {"a": 2}}
    store.log(3, "x", "one", T0)
    store.log(None, "y", "two", T0 + m(1), url="u")
    assert [t["text"] for t in store.timeline()] == ["two", "one"]
    assert [t["text"] for t in store.timeline(3)] == ["one"]
    assert store.timeline(limit=1)[0]["url"] == "u"


def test_lock_is_exclusive_until_released_or_expired(store):
    assert store.acquire("testdb", "w1", T0) and not store.acquire("testdb", "w2", T0)
    assert store.acquire("testdb", "w2", T0 + m(61))
    store.release("testdb", "w1")  # not holder: no-op
    assert not store.acquire("testdb", "w1", T0 + m(62))
    store.release("testdb", "w2")
    assert store.acquire("testdb", "w1", T0 + m(62))


def _writer(args):
    path, n = args
    s = Store(path)
    for i in range(50):
        s.add_event(ev(f"p{n}-{i}"))
    return True


def test_concurrent_writers(tmp_path):
    path = tmp_path / "c.db"
    Store(path)
    with mp.get_context("spawn").Pool(8) as pool:
        assert all(pool.map(_writer, [(path, n) for n in range(8)]))
    assert len(Store(path).events()) == 400
