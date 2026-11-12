import json
from datetime import datetime, timedelta, timezone

import pytest

from autopilot.batching import apply_limits, batch_meta, ready_batches
from autopilot.config import Config
from autopilot.models import Event
from autopilot.store import Store

T0 = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)


def m(n):
    return timedelta(minutes=n)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "s.db")


@pytest.fixture
def cfg(tmp_path):
    return Config(name="t", repo=tmp_path, author="me", gh_user="me",
                  limits={"thread_rounds": 3, "wakes_per_pr_day": 5})


def ev(i, pr=589, kind="comment", at=T0, prio=4, **payload):
    return Event(id=i, pr=pr, kind=kind, priority=prio, created_at=at, payload=payload)


def thread_ev(i, tid="t1", pushback=0, pr=589, at=T0):
    return ev(i, pr=pr, kind="thread", at=at, thread_id=tid, pushback=pushback)


def test_waits_debounce_window(store, cfg):
    store.add_event(ev("a"))
    assert ready_batches(store, cfg, T0 + m(9)) == []
    ids = ready_batches(store, cfg, T0 + m(10))
    assert len(ids) == 1
    b = store.batch(ids[0])
    assert b["pr"] == 589 and b["status"] == "new" and b["event_ids"] == ["a"]
    assert store.events(status="new") == [] and [e.id for e in store.events(status="batched")] == ["a"]


def test_debounce_uses_latest_event(store, cfg):
    store.add_event(ev("a", at=T0))
    store.add_event(ev("b", at=T0 + m(8)))
    assert ready_batches(store, cfg, T0 + m(10)) == []
    ids = ready_batches(store, cfg, T0 + m(18))
    assert store.batch(ids[0])["event_ids"] == ["a", "b"]


def test_submitted_review_flushes_immediately(store, cfg):
    store.add_event(ev("a"))
    store.add_event(ev("review:1", kind="review", prio=0, state="CHANGES_REQUESTED"))
    ids = ready_batches(store, cfg, T0)
    assert len(ids) == 1
    assert store.batch(ids[0])["event_ids"] == ["review:1", "a"]  # priority order


def test_groups_per_pr(store, cfg):
    store.add_event(ev("a", pr=1))
    store.add_event(ev("b", pr=2))
    ids = ready_batches(store, cfg, T0 + m(10))
    assert sorted(store.batch(i)["pr"] for i in ids) == [1, 2]


@pytest.mark.parametrize("status", ["delivered", "acked", "in_progress", "waiting_owner", "waiting_limit"])
def test_no_new_batch_while_one_in_flight(store, cfg, status):
    store.add_event(ev("old"))
    first = ready_batches(store, cfg, T0 + m(10))[0]
    path = {"delivered": ["delivered"], "acked": ["delivered", "acked"],
            "in_progress": ["delivered", "acked", "in_progress"],
            "waiting_owner": ["delivered", "acked", "in_progress", "waiting_owner"],
            "waiting_limit": ["delivered", "acked", "in_progress", "waiting_limit"]}[status]
    for s in path:
        store.transition(first, s, T0 + m(11))
    store.add_event(ev("new1", at=T0 + m(12)))
    assert ready_batches(store, cfg, T0 + m(30)) == []
    assert [e.id for e in store.events(status="new")] == ["new1"]
    store.transition(first, "cancelled", T0 + m(31))  # terminal
    ids = ready_batches(store, cfg, T0 + m(31))
    assert len(ids) == 1 and store.batch(ids[0])["event_ids"] == ["new1"]


def test_noop_events_do_not_batch(store, cfg):
    store.add_event(ev("n", kind="noop"))
    assert ready_batches(store, cfg, T0 + m(60)) == []


def test_sixth_wake_of_day_pauses_pr(store, cfg):
    for i in range(5):
        store.add_event(ev(f"e{i}", at=T0 + m(i * 20)))
        bid = ready_batches(store, cfg, T0 + m(i * 20 + 10))[0]
        assert apply_limits(store, cfg, bid, T0 + m(i * 20 + 10)) is None
        store.transition(bid, "cancelled", T0 + m(i * 20 + 10))  # finish so the next batch can form
    store.add_event(ev("e5", at=T0 + m(100)))
    bid = ready_batches(store, cfg, T0 + m(110))[0]
    assert apply_limits(store, cfg, bid, T0 + m(110)) == "paused"
    assert store.kv_get("paused_pr:589") == "limit"
    assert store.batch(bid)["status"] == "cancelled"
    assert any(t["pr"] == 589 for t in store.timeline(589))


def test_wake_cap_is_per_day(store, cfg):
    store.add_event(ev("a"))
    bid = ready_batches(store, cfg, T0 + m(10))[0]
    for _ in range(5):
        store.bump_wake(589, T0.date())
    assert apply_limits(store, cfg, bid, T0 + m(10)) == "paused"
    store.add_event(ev("b", at=T0 + timedelta(days=1)))
    nxt = ready_batches(store, cfg, T0 + timedelta(days=1, minutes=10))[0]
    assert apply_limits(store, cfg, nxt, T0 + timedelta(days=1, minutes=10)) is None


def test_fourth_round_forces_c2(store, cfg):
    for r in range(1, 5):
        at = T0 + m(r * 20)
        store.add_event(thread_ev(f"c{r}", at=at))
        bid = ready_batches(store, cfg, at + m(10))[0]
        assert apply_limits(store, cfg, bid, at + m(10)) is None
        meta = batch_meta(store, bid)
        assert meta["force_c2_threads"] == (["t1"] if r == 4 else [])
        store.transition(bid, "cancelled", at + m(10))


def test_second_pushback_forces_c2(store, cfg):
    store.add_event(thread_ev("c1", tid="tA", pushback=2))
    store.add_event(thread_ev("c2", tid="tB", pushback=1))
    bid = ready_batches(store, cfg, T0 + m(10))[0]
    assert apply_limits(store, cfg, bid, T0 + m(10)) is None
    meta = batch_meta(store, bid)
    assert meta["force_c2_threads"] == ["tA"]
    assert meta["force_c2"] == ["c1"]
    assert meta["pushback"] == {"tA": 2, "tB": 1}


def test_batch_meta_default(store, cfg):
    store.add_event(ev("a"))
    bid = ready_batches(store, cfg, T0 + m(10))[0]
    assert batch_meta(store, bid) == {"force_c2_threads": [], "pushback": {}, "force_c2": []}
