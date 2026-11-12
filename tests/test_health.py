import json
from datetime import datetime, timedelta, timezone

import pytest

from autopilot.health import check_workers, limit_active, parse_limit
from test_transport_headless import Fx, OK, T0

BKK = timezone(timedelta(hours=7))
TXT = "You've hit your 5-hour limit · resets 7:20pm (Asia/Bangkok)"


def test_parse_limit_reset_with_timezone():
    assert parse_limit(TXT, datetime(2026, 11, 11, 19, 0, tzinfo=BKK)) == datetime(2026, 11, 11, 19, 25, tzinfo=BKK)
    assert parse_limit(TXT, datetime(2026, 11, 11, 20, 0, tzinfo=BKK)) == datetime(2026, 11, 12, 19, 25, tzinfo=BKK)
    assert parse_limit("resets 6:30pm", T0) is None
    assert parse_limit("You've hit your 5-hour limit · resets 6pm (Asia/Bangkok)",
                       datetime(2026, 11, 11, 1, 0, tzinfo=BKK)) == datetime(2026, 11, 11, 18, 5, tzinfo=BKK)


def test_parse_limit_none_for_normal_text():
    assert parse_limit("All good, tests pass", T0) is None


@pytest.fixture
def fx(tmp_path):
    return Fx(tmp_path)


def run_rec(fx):
    return json.loads(fx.store.kv_get(f"run:{fx.wt.path}"))


def test_dead_run_resumed_twice_then_escalated(fx):
    b = fx.batch("in_progress")
    sid = fx.t.start_worker(fx.wt, "/pr-triage batch 1", b)
    wtb = {"feat/x": (fx.wt, sid)}
    pid = 4242
    for n in (1, 2):
        fx.die(pid)
        pid += 1
        out = check_workers(fx.store, fx.t, wtb, T0)
        assert any("re-ran" in o for o in out)
        argv = fx.argv()
        assert argv[argv.index("--resume") + 1] == sid and argv[2] == "/pr-triage batch 1"
        assert fx.store.batch(b)["status"] == "delivered"
        # worker picks it up again and gets stuck
        fx.store.transition(b, "acked", T0)
        fx.store.transition(b, "in_progress", T0)
    fx.die(pid)
    spawned = len([c for c in fx.calls if c[0] != "kill"])
    out = check_workers(fx.store, fx.t, wtb, T0)
    assert len([o for o in out if o.startswith("alert:")]) == 1
    assert len([c for c in fx.calls if c[0] != "kill"]) == spawned
    assert fx.store.batch(b)["status"] == "escalated"


def test_stale_worker_alerted_at_60_stopped_at_90(fx):
    b = fx.batch("in_progress")
    sid = fx.t.start_worker(fx.wt, "p", b)
    started = datetime.fromisoformat(run_rec(fx)["started_at"])
    wtb = {"feat/x": (fx.wt, sid)}
    assert check_workers(fx.store, fx.t, wtb, started + timedelta(minutes=59)) == []
    out = check_workers(fx.store, fx.t, wtb, started + timedelta(minutes=61))
    assert len(out) == 1 and out[0].startswith("alert:")
    assert check_workers(fx.store, fx.t, wtb, started + timedelta(minutes=70)) == []  # once
    assert not any(c[0] == "kill" for c in fx.calls)
    out = check_workers(fx.store, fx.t, wtb, started + timedelta(minutes=91))
    assert any(c[0] == "kill" for c in fx.calls) and out[0].startswith("alert:")
    assert fx.store.batch(b)["status"] == "escalated"
    assert fx.store.kv_get(f"session:{fx.wt.path}") == sid


def test_limit_detected_pauses_all_batches_keeps_session(fx):
    b1 = fx.batch("in_progress")
    b2 = fx.batch("delivered")
    sid = fx.t.start_worker(fx.wt, "p", b1)
    fx.die(4242)
    open(run_rec(fx)["out"], "w").write("")
    open(run_rec(fx)["err"], "w").write(TXT)
    now = datetime(2026, 11, 11, 19, 0, tzinfo=BKK)
    check_workers(fx.store, fx.t, {"feat/x": (fx.wt, sid)}, now)
    assert fx.store.batch(b1)["status"] == fx.store.batch(b2)["status"] == "waiting_limit"
    assert limit_active(fx.store, now) == datetime(2026, 11, 11, 19, 25, tzinfo=BKK)
    assert limit_active(fx.store, now + timedelta(hours=1)) is None
    assert fx.store.kv_get(f"session:{fx.wt.path}") == sid


def test_forbidden_op_alert_once(fx):
    b = fx.batch("done")
    sid = fx.t.start_worker(fx.wt, "p", b)
    fx.die(4242)
    open(run_rec(fx)["out"], "w").write(json.dumps({
        "type": "result", "subtype": "success", "is_error": False, "result": "x",
        "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "git push"}}]}))
    wtb = {"feat/x": (fx.wt, sid)}
    out = check_workers(fx.store, fx.t, wtb, T0)
    assert out == ["alert: worker tried forbidden op: Bash(git push:*) (feat/x)"]
    assert check_workers(fx.store, fx.t, wtb, T0) == []


def test_limit_pause_ends_after_reset(fx):
    b = fx.batch("in_progress")
    sid = fx.t.start_worker(fx.wt, "p", b)
    fx.die(4242)
    open(run_rec(fx)["out"], "w").write("")
    open(run_rec(fx)["err"], "w").write(TXT)
    wtb = {"feat/x": (fx.wt, sid)}
    t1 = datetime(2026, 11, 11, 19, 0, tzinfo=BKK)
    check_workers(fx.store, fx.t, wtb, t1)
    until = limit_active(fx.store, t1)
    assert until == datetime(2026, 11, 11, 19, 25, tzinfo=BKK)
    t2 = datetime(2026, 11, 11, 19, 27, tzinfo=BKK)
    assert fx.t.status(fx.wt, t2).state != "limited"
    out = check_workers(fx.store, fx.t, wtb, t2)
    assert limit_active(fx.store, t2) is None
    assert datetime.fromisoformat(fx.store.kv_get("limit_until")) == until  # not re-armed
    assert not any("usage limit" in o for o in out)


def test_spawn_failure_on_rerun_alerts(fx):
    b = fx.batch("in_progress")
    sid = fx.t.start_worker(fx.wt, "p", b)
    fx.die(4242)

    def boom(*a):
        raise FileNotFoundError("claude")

    fx.t.spawn = boom
    out = check_workers(fx.store, fx.t, {"feat/x": (fx.wt, sid)}, T0)
    assert out[0].startswith("alert:") and fx.store.kv_get(f"reruns:{b}") is None
