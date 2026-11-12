import json
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest

from autopilot.config import load_config
from autopilot.models import WorkerStatus, Worktree
from autopilot.router import snapshot_to_dict
from autopilot.store import Store
from autopilot.web import server as web
from conftest import T0, FakeTransport, snap

TOKEN = "t" * 20


@pytest.fixture
def env(tmp_path):
    cd = tmp_path / "cfg"
    cd.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (cd / "ivt.toml").write_text(f'repo="{repo}"\nauthor="me"\ngh_user="me"\n')
    cfg = load_config("ivt", cd)
    store = Store(cfg.db_path)
    store.add_item("Q", 589, 1, "câu hỏi", "body", T0)
    store.save_snapshot(589, snapshot_to_dict(snap(589, head="feat/x")))
    tr = FakeTransport()
    wt = Worktree("feat/x", str(repo / "wt"))
    srv = web.make_server(cd, port=0, token=TOKEN, transport_factory=lambda c, s: tr,
                          worktree_lister=lambda c: {"feat/x": wt})
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield type("E", (), dict(cd=cd, cfg=cfg, store=store, srv=srv, tr=tr, wt=wt,
                             base=f"http://127.0.0.1:{srv.server_address[1]}"))
    srv.shutdown()
    srv.server_close()


def call(env, path, method="GET", body=None, token=TOKEN, headers=None):
    h = {"X-Autopilot-Token": token} if token else {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(env.base + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null"), r
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null"), e


def test_requires_token(env):
    assert call(env, "/api/overview", token=None)[0] == 401
    assert call(env, "/api/overview", token="wrong")[0] == 401
    st, _, r = call(env, f"/api/overview?token={TOKEN}", token=None)
    assert st == 200
    ck = r.headers["Set-Cookie"]
    assert "HttpOnly" in ck and "SameSite=Strict" in ck
    st, _, _ = call(env, "/api/overview", token=None, headers={"Cookie": ck.split(";")[0]})
    assert st == 200


def test_binds_localhost_only(env, tmp_path):
    assert env.srv.server_address[0] == "127.0.0.1"
    with pytest.raises(ValueError):
        web.make_server(env.cd, host="0.0.0.0", port=0, token=TOKEN)


def test_answer_endpoint_resolves_and_is_idempotent(env):
    body = {"code": "Q-ivt-1", "choice": "A", "note": "n", "reply_hint": "", "source": "web"}
    st, res, _ = call(env, "/api/answer", "POST", body)
    assert st == 200 and res["batch"] >= 1
    assert env.store.item("Q-ivt-1")["status"] == "closed"
    st, res, _ = call(env, "/api/answer", "POST", body)
    assert st == 409 and "error" in res
    assert call(env, "/api/answer", "POST", {**body, "code": "Q-ivt-99"})[0] == 404
    assert len(env.store.batches()) == 1


def test_settings_rejects_non_editable_key(env):
    assert call(env, "/api/settings", "POST", {"key": "repo", "value": "/x"})[0] == 400
    assert call(env, "/api/settings", "POST", {"key": "max_workers", "value": "abc"})[0] == 400
    assert call(env, "/api/settings", "POST", {"key": "notify.hours", "value": "7-22"})[0] == 400
    assert not (env.cd / "ivt.overrides.toml").exists()
    assert call(env, "/api/settings", "POST", {"key": "max_workers", "value": 3})[0] == 200
    assert call(env, "/api/settings")[1]["max_workers"] == 3


def test_qa_paste_writes_file_under_qa_dir(env):
    assert call(env, "/api/qa", "POST", {"name": "../x", "text": "hi"})[0] == 400
    assert call(env, "/api/qa", "POST", {"name": "a/b", "text": "hi"})[0] == 400
    assert call(env, "/api/qa", "POST", {"name": "big", "text": "x" * 1_000_001})[0] == 400
    st, res, _ = call(env, "/api/qa", "POST", {"name": "run-1", "text": "hello"})
    assert st == 200
    assert (env.cfg.qa_dir / "run-1.md").read_text() == "hello"


def test_overview_shapes(env):
    st, o, _ = call(env, "/api/overview")
    assert st == 200
    assert o == {"paused": None, "limit_until": None, "slots": {"working": 0, "max": 5},
                 "open_items": 1, "incidents_24h": 0}
    call(env, "/api/pause", "POST", {})
    assert call(env, "/api/overview")[1]["paused"] == "1"
    call(env, "/api/resume", "POST", {})
    assert call(env, "/api/overview")[1]["paused"] is None
    call(env, "/api/pause", "POST", {"pr": 589})
    assert env.store.kv_get("paused_pr:589") == "owner"
    (item,) = call(env, "/api/items")[1]
    assert set(item) == {"code", "kind", "pr", "title", "body_md", "created_at"}
    (pr,) = call(env, "/api/prs")[1]
    assert {"number", "title", "url", "head", "base", "draft", "review", "conflict", "failed_checks",
            "batch_status", "last_action", "warnings"} <= set(pr)
    assert pr["warnings"]
    assert call(env, "/api/projects")[1] == ["ivt"]
    for p in ("/api/timeline?pr=589", "/api/backlog", "/api/digest", "/api/settings"):
        assert call(env, p)[0] == 200


def test_workers_reads_cost_from_run_json(env, tmp_path):
    out = tmp_path / "run.json"
    out.write_text(json.dumps({"total_cost_usd": 0.42, "result": "ok"}))
    env.store.kv_set(f"run:{env.wt.path}", json.dumps(
        {"batch": 7, "session": "s", "started_at": T0.isoformat(), "out": str(out)}))
    env.store.kv_set("attempt:7", "2")
    env.tr.states["feat/x"] = WorkerStatus("idle", "s", None)
    (w,) = call(env, "/api/workers")[1]
    assert w["branch"] == "feat/x" and w["pr"] == 589 and w["state"] == "idle"
    assert w["last_run"]["cost_usd"] == 0.42 and w["last_run"]["attempts"] == 2
    assert env.tr.calls[0][0] == "set_worktrees"


def test_csrf_and_content_type(env):
    body = {"code": "Q-ivt-1", "choice": "A"}
    st, _, _ = call(env, "/api/answer", "POST", body, headers={"Origin": "http://evil.example"})
    assert st == 403
    st, _, _ = call(env, "/api/answer", "POST", body, headers={"Origin": f"{env.base}"})
    assert st == 200
    req = urllib.request.Request(env.base + "/api/pause", data=b"{}", method="POST",
                                 headers={"X-Autopilot-Token": TOKEN, "Content-Type": "text/plain"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 415


def test_static_traversal_blocked(env):
    assert call(env, "/static/../server.py")[0] == 404
    assert call(env, "/static/%2e%2e/server.py")[0] == 404


def test_token_file_created_0600(tmp_path):
    p = tmp_path / "d" / "web-token"
    t1 = web.load_token(p)
    assert len(t1) >= 32 and oct(os.stat(p).st_mode & 0o777) == "0o600"
    assert web.load_token(p) == t1
