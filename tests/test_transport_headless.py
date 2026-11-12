import json
import os
import fcntl
from datetime import datetime, timedelta, timezone

import pytest

from autopilot.config import Config
from autopilot.models import Worktree
from autopilot.store import Store
from autopilot.transport import get_transport
from autopilot.transport.headless import HeadlessTransport, WorkerBusy, encode_cwd

T0 = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
WORKER = {
    "claude_bin": "claude", "permission_mode": "acceptEdits",
    "allowed_tools": ["Edit", "Bash(rtk git add:*)"],
    "disallowed_tools": ["Bash(git push:*)", "Bash(rtk git push:*)"],
}


class Fx:
    def __init__(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self.cfg = Config(name="p", repo=repo, author="a", gh_user="g", worker=WORKER)
        self.store = Store(tmp_path / "s.db")
        self.proc = tmp_path / "proc"
        self.proc.mkdir()
        self.projects = tmp_path / "projects"
        self.wt = Worktree("feat/x", str(tmp_path / "wt"))
        os.makedirs(self.wt.path)
        self.calls = []
        self.t = HeadlessTransport(self.cfg, self.store, spawn=self.spawn, proc_root=self.proc,
                                   projects_root=self.projects, killpg=lambda g, s: self.calls.append(("kill", g, s)))
        self.next_pid = 4242

    def spawn(self, argv, cwd, out, err, pass_fds=()):
        self.calls.append((argv, cwd, out, err))
        pid = self.next_pid
        self.next_pid += 1
        self.alive(pid)
        return pid

    def alive(self, pid):
        (self.proc / str(pid)).mkdir(exist_ok=True)

    def die(self, pid):
        (self.proc / str(pid)).rmdir()

    def argv(self, i=-1):
        return [c for c in self.calls if c[0] != "kill"][i][0]

    def batch(self, to=None):
        b = self.store.create_batch(1, ["e"], T0)
        if to:
            self.store.transition(b, "delivered", T0)
            if to != "delivered":
                self.store.transition(b, "acked", T0)
                self.store.transition(b, "in_progress", T0)
            if to in ("done", "waiting_owner"):
                self.store.transition(b, to, T0)
        return b


@pytest.fixture
def fx(tmp_path):
    return Fx(tmp_path)


def test_start_worker_uses_new_session_id_and_worktree_cwd(fx):
    sid = fx.t.start_worker(fx.wt, "/pr-triage batch 1", 1)
    argv = fx.argv()
    i = argv.index("--session-id")
    assert argv[i + 1] == sid and "--resume" not in argv
    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--model") + 1] == fx.cfg.worker_model
    assert argv[argv.index("--output-format") + 1] == "json"
    assert fx.calls[0][1] == fx.wt.path
    assert fx.store.kv_get(f"session:{fx.wt.path}") == sid
    run = json.loads(fx.store.kv_get(f"run:{fx.wt.path}"))
    assert run["pid"] == run["pgid"] == 4242 and run["batch"] == 1 and run["out"].endswith("1-1.json")


def test_deliver_resumes_same_session(fx):
    sid = fx.t.start_worker(fx.wt, "p", 1)
    fx.die(4242)
    fx.t.deliver(fx.wt, sid, "p2", 1)
    argv = fx.argv()
    assert argv[argv.index("--resume") + 1] == sid and "--session-id" not in argv
    assert json.loads(fx.store.kv_get(f"run:{fx.wt.path}"))["out"].endswith("1-2.json")


def test_worker_flags_come_from_config(fx):
    fx.cfg.worker["claude_bin"]  # sanity
    fx.t.start_worker(fx.wt, "p", 1)
    a = fx.argv()
    assert a[a.index("--permission-mode") + 1] == "acceptEdits"
    s, d = a.index("--allowedTools"), a.index("--disallowedTools")
    assert a[s + 1:d] == WORKER["allowed_tools"] and a[d + 1:] == WORKER["disallowed_tools"]


def test_prompt_has_only_batch_path_and_project(fx):
    fx.t.start_worker(fx.wt, "/pr-triage batch 1 events/1.md --project p", 1)
    a = fx.argv()
    assert isinstance(a, list) and a[2] == "/pr-triage batch 1 events/1.md --project p"


def test_second_spawn_on_same_worktree_raises_busy(fx):
    fx.t.start_worker(fx.wt, "p", 1)
    with pytest.raises(WorkerBusy):
        fx.t.start_worker(fx.wt, "p", 2)
    assert len(fx.calls) == 1


def test_held_flock_raises_busy(fx):
    lock = fx.t.lock_path(fx.wt)
    lock.parent.mkdir(parents=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(WorkerBusy):
            fx.t.start_worker(fx.wt, "p", 1)
    finally:
        os.close(fd)
    assert fx.calls == []


def _write_out(fx, text, err=""):
    run = json.loads(fx.store.kv_get(f"run:{fx.wt.path}"))
    open(run["out"], "w").write(text)
    open(run["err"], "w").write(err)


OK = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done"})


def test_status_states(fx):
    assert fx.t.status(fx.wt).state == "none"
    b = fx.batch("in_progress")
    sid = fx.t.start_worker(fx.wt, "p", b)
    assert fx.t.status(fx.wt).state == "working"
    fx.die(4242)
    _write_out(fx, "")
    st = fx.t.status(fx.wt)
    assert st.state == "dead" and st.session == sid
    fx.store.transition(b, "waiting_owner", T0)
    assert fx.t.status(fx.wt).state == "waiting"
    fx.store.transition(b, "delivered", T0)
    fx.store.transition(b, "acked", T0)
    fx.store.transition(b, "in_progress", T0)
    fx.store.transition(b, "done", T0) if False else None
    b2 = fx.batch("done")
    fx.store.kv_set(f"run:{fx.wt.path}", json.dumps({**json.loads(fx.store.kv_get(f"run:{fx.wt.path}")), "batch": b2}))
    _write_out(fx, OK)
    assert fx.t.status(fx.wt).state == "idle"
    # limited: failed turn whose stderr carries the limit message
    b3 = fx.batch("in_progress")
    fx.store.kv_set(f"run:{fx.wt.path}", json.dumps({**json.loads(fx.store.kv_get(f"run:{fx.wt.path}")), "batch": b3}))
    _write_out(fx, "", "You've hit your 5-hour limit · resets 7:20pm (Asia/Bangkok)")
    assert fx.t.status(fx.wt).state == "limited"


def test_limit_in_transcript_tail(fx):
    b = fx.batch("in_progress")
    sid = fx.t.start_worker(fx.wt, "p", b)
    fx.die(4242)
    _write_out(fx, "")
    p = fx.t.transcript_path(fx.wt, sid)
    p.parent.mkdir(parents=True)
    p.write_text('{"text":"You\'ve hit your 5-hour limit \\u00b7 resets 6:30pm (Asia/Bangkok)"}\n')
    assert fx.t.status(fx.wt).state == "limited"


def test_forbidden_denial_is_completed_turn_stray_denial_is_not(fx):
    b = fx.batch("in_progress")
    fx.t.start_worker(fx.wt, "p", b)
    fx.die(4242)
    deny = lambda cmd: {"type": "result", "subtype": "success", "is_error": False, "result": "x",
                        "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": cmd}}]}
    _write_out(fx, json.dumps(deny("rtk git push origin x")))
    assert fx.t.forbidden_ops(fx.wt) == ["Bash(rtk git push:*)"]
    _write_out(fx, json.dumps(deny("ls")))
    assert fx.t.forbidden_ops(fx.wt) == []
    assert fx.t._turn(fx.wt)[0] is False


def test_owner_session_detected_via_proc_cwd(fx):
    fx.t.start_worker(fx.wt, "p", fx.batch("in_progress"))
    fx.die(4242)
    _write_out(fx, OK)
    # not-owner noise: node helper in worktree, observer, claude elsewhere
    def proc(pid, argv, cwd):
        d = fx.proc / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
        os.symlink(cwd, d / "cwd")
    proc(10, ["node", "-e", "x"], fx.wt.path)
    proc(11, ["/usr/bin/claude", "--output-format", "stream-json", "--input-format", "stream-json"], fx.wt.path)
    proc(12, ["/usr/bin/claude"], "/elsewhere")
    assert fx.t.status(fx.wt).state != "owner_busy"
    proc(13, ["/home/u/.local/bin/claude", "--dangerously-skip-permissions"], fx.wt.path + "/sub")
    assert fx.t.status(fx.wt).state == "owner_busy"


def test_stop_worker_kills_group_keeps_session_unless_closed(fx):
    sid = fx.t.start_worker(fx.wt, "p", 1)
    fx.t.stop_worker(fx.wt, sid)
    assert ("kill", 4242, 15) in fx.calls
    assert fx.store.kv_get(f"session:{fx.wt.path}") == sid and not fx.store.kv_get(f"session-closed:{fx.wt.path}")
    fx.t.stop_worker(fx.wt, sid, close=True)
    assert fx.store.kv_get(f"session-closed:{fx.wt.path}") == "1"
    assert fx.t.status(fx.wt).state == "none"
    with pytest.raises(ValueError):
        fx.t.deliver(fx.wt, sid, "p", 1)


def test_encode_cwd():
    assert encode_cwd("/home/mht/Projects/MHT/iviettien/.worktrees/int-a2") == \
        "-home-mht-Projects-MHT-iviettien--worktrees-int-a2"


def test_get_transport_headless_default_orca_not_implemented(fx):
    assert isinstance(get_transport(fx.cfg, fx.store), HeadlessTransport)
    import dataclasses
    with pytest.raises(NotImplementedError):
        get_transport(dataclasses.replace(fx.cfg, transport="orca"), fx.store)
