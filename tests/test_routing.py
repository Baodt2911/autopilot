from datetime import datetime, timedelta, timezone

import pytest

from autopilot.config import Config
from autopilot.models import PRSnapshot, WorkerStatus, Worktree
from autopilot.routing import Deliver, Incident, Queue, Start, route

T0 = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg(tmp_path):
    return Config(name="t", repo=tmp_path, author="me", gh_user="me")


def pr(n):
    return PRSnapshot(number=n, title="t", url="u", is_draft=False, state="OPEN",
                      head_ref=f"b{n}", head_sha="h", base_ref="main", base_sha="s",
                      mergeable="MERGEABLE", review_decision=None, threads=(), reviews=(),
                      comments=(), failed_checks=(), changed_files=())


def batch(i, p, prio=4, at=T0):
    return {"id": i, "pr": p, "status": "ready", "attempts": 0, "session": None,
            "event_ids": [], "priority": prio, "created_at": at}


def ws(state, session="s"):
    return WorkerStatus(state=state, session=session, updated_at=T0)


def world(*ns):
    return ({n: pr(n) for n in ns}, {f"b{n}": Worktree(f"b{n}", f"/w/{n}") for n in ns})


def run(batches, ns, workers, cfg, **kw):
    prs, wts = world(*ns)
    kw.setdefault("paused", False)
    kw.setdefault("paused_prs", set())
    return route(batches, prs, wts, workers, cfg, **kw)


def test_deliver_to_live_session(cfg):
    r = run([batch(1, 1)], [1], {"b1": ws("idle", "sess1")}, cfg)
    assert r == [Deliver(1, Worktree("b1", "/w/1"), "sess1")]
    r = run([batch(1, 1)], [1], {"b1": ws("waiting", "sess1")}, cfg)
    assert isinstance(r[0], Deliver) and r[0].session == "sess1"


def test_start_worker_when_no_session_and_slot_free(cfg):
    assert run([batch(1, 1)], [1], {}, cfg) == [Start(1, Worktree("b1", "/w/1"))]
    r = run([batch(1, 1)], [1], {"b1": ws("none", None)}, cfg)
    assert r == [Start(1, Worktree("b1", "/w/1"))]


def test_queue_when_five_working_even_if_others_idle(cfg):
    workers = {f"w{i}": ws("working") for i in range(5)}
    workers["b1"] = ws("idle")
    assert run([batch(1, 1)], [1], workers, cfg) == [Queue(1, "slots")]


def test_deliveries_in_same_scan_count_as_slots(cfg):
    workers = {f"w{i}": ws("working") for i in range(3)}
    workers.update({f"b{n}": ws("idle", f"s{n}") for n in (1, 2, 3)})
    r = run([batch(n, n) for n in (1, 2, 3)], [1, 2, 3], workers, cfg)
    assert [type(x) for x in r] == [Deliver, Deliver, Queue]
    assert r[2] == Queue(3, "slots")


def test_busy_worktree_queues_not_double_spawn(cfg):
    assert run([batch(1, 1)], [1], {"b1": ws("working")}, cfg) == [Queue(1, "busy")]
    # two batches on the same worktree in one scan: second must not spawn
    prs, wts = world(1)
    prs[2] = pr(1).__class__(**{**pr(1).__dict__, "number": 2})  # same head_ref b1
    r = route([batch(1, 1), batch(2, 2)], prs, wts, {}, cfg, paused=False, paused_prs=set())
    assert [type(x) for x in r] == [Start, Queue] and r[1].reason == "busy"


def test_owner_session_queues(cfg):
    assert run([batch(1, 1)], [1], {"b1": ws("owner_busy")}, cfg) == [Queue(1, "owner_session")]


def test_waiting_owner_workers_do_not_count(cfg):
    workers = {f"w{i}": ws("working") for i in range(4)}
    workers.update({f"x{i}": ws("waiting") for i in range(3)})
    assert isinstance(run([batch(1, 1)], [1], workers, cfg)[0], Start)


def test_priority_order(cfg):
    # CHANGES_REQUESTED(0) > conflict(1) > base(2) > ci(3) > comment(4)
    bs = [batch(1, 1, 4), batch(2, 2, 3), batch(3, 3, 2), batch(4, 4, 1), batch(5, 5, 0)]
    r = run(bs, [1, 2, 3, 4, 5], {}, cfg)
    assert [x.batch_id for x in r] == [5, 4, 3, 2, 1]
    # ties broken by created_at
    bs = [batch(1, 1, 4, T0 + timedelta(minutes=5)), batch(2, 2, 4, T0)]
    assert [x.batch_id for x in run(bs, [1, 2], {}, cfg)] == [2, 1]
    # priority wins slot contention
    workers = {f"w{i}": ws("working") for i in range(4)}
    r = run([batch(1, 1, 4), batch(2, 2, 0)], [1, 2], workers, cfg)
    assert isinstance(r[0], Start) and r[0].batch_id == 2 and r[1] == Queue(1, "slots")


def test_no_local_worktree_is_incident(cfg):
    prs, _ = world(1)
    r = route([batch(1, 1)], prs, {}, {}, cfg, paused=False, paused_prs=set())
    assert len(r) == 1 and isinstance(r[0], Incident) and "b1" in r[0].text


def test_global_pause_and_pr_pause_queue_everything(cfg):
    r = run([batch(1, 1), batch(2, 2)], [1, 2], {}, cfg, paused=True)
    assert r == [Queue(1, "paused"), Queue(2, "paused")]
    r = run([batch(1, 1), batch(2, 2)], [1, 2], {}, cfg, paused_prs={1})
    assert r[0] == Queue(1, "paused_pr") and isinstance(r[1], Start)


def test_limited_worker_gets_queue_not_restart(cfg):
    assert run([batch(1, 1)], [1], {"b1": ws("limited")}, cfg) == [Queue(1, "limit")]


def test_dead_worker_queues_for_health(cfg):
    assert run([batch(1, 1)], [1], {"b1": ws("dead")}, cfg) == [Queue(1, "dead")]
