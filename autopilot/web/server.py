"""Local dashboard JSON API (stdlib http.server). Loopback only, token required."""
from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import secrets
import sys
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .. import answers, ops
from ..config import (DEFAULT_CONFIG_DIR, EDITABLE_KEYS, Config, get_setting, list_projects,
                      load_config, set_override, validate_value)
from ..health import limit_active
from ..store import ItemClosed, Store
from ..worktrees import list_worktrees

TOKEN_FILE = Path.home() / ".config" / "autopilot" / "web-token"
STATIC_DIR = Path(__file__).parent / "static"
COOKIE = "autopilot_token"
MAX_BODY = 2_500_000
TERMINAL_BATCH = ("done", "escalated", "cancelled")
INCIDENT_KINDS = ("escalated", "limit", "notify_failed", "incident")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def load_token(path: Path = TOKEN_FILE) -> str:
    path = Path(path)
    if path.exists():
        tok = path.read_text(encoding="utf-8").strip()
        if tok:
            os.chmod(path, 0o600)
            return tok
    path.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(tok + "\n")
    os.chmod(path, 0o600)
    return tok


def _iso(d) -> str | None:
    return d.isoformat() if isinstance(d, datetime) else d


def _now() -> datetime:
    return datetime.now().astimezone()


class Ctx:
    """Everything the handler needs; factories are injectable for tests."""

    def __init__(self, config_dir: Path, token: str, *, transport_factory=None, github_factory=None,
                 worktree_lister=None):
        self.config_dir, self.token = Path(config_dir), token
        self.transport_factory = transport_factory or self._transport
        self.github_factory = github_factory or self._github
        self.worktree_lister = worktree_lister or (lambda cfg: list_worktrees(cfg.repo))

    @staticmethod
    def _transport(cfg, store):
        from ..transport import get_transport
        return get_transport(cfg, store)

    @staticmethod
    def _github(cfg):
        from ..github import GitHub
        return GitHub(cfg)

    def project(self, query: dict, body: dict | None = None) -> tuple[Config, Store]:
        name = (query.get("project") or [None])[0] or (body or {}).get("project")
        if not name:
            projects = list_projects(self.config_dir)
            if len(projects) != 1:
                raise ApiError(400, "Chưa chọn dự án: thêm ?project=<tên>.")
            name = projects[0]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name.startswith("."):
            raise ApiError(404, "Không tìm thấy dự án.")
        try:
            cfg = load_config(name, self.config_dir)
        except FileNotFoundError:
            raise ApiError(404, f"Không tìm thấy dự án '{name}'.") from None
        except ValueError as e:
            raise ApiError(409, f"Cấu hình dự án '{name}' lỗi: {e}") from None
        return cfg, Store(cfg.db_path)


# ---- GET handlers -----------------------------------------------------------------
def _slots_working(ctx, cfg, store) -> int:
    raw = store.kv_get("slots_working")  # recorded by every router scan
    if raw is not None and raw.isdigit():
        return int(raw)
    try:
        transport = ctx.transport_factory(cfg, store)
        wts = ctx.worktree_lister(cfg)
        transport.set_worktrees([w.path for w in wts.values()])
        return ops.count_working(transport, wts, _now())
    except Exception:
        return 0


def api_overview(ctx, cfg, store, q):
    now = _now()
    lim = limit_active(store, now)
    since = now - timedelta(hours=24)
    incidents = [t for t in store.timeline(limit=500)
                 if t["kind"] in INCIDENT_KINDS and t["at"] >= since]
    snaps = store.snapshots()
    paused_prs = sorted(n for n, d in snaps.items() if d.get("state") == "OPEN" and store.kv_get(f"paused_pr:{n}"))
    return {"paused": bool(store.kv_get("paused")), "paused_prs": paused_prs, "allowlist": ops.get_allowlist(store),
            "limit_until": _iso(lim),
            "slots": {"working": _slots_working(ctx, cfg, store), "max": cfg.max_workers},
            "open_items": len(store.open_items()), "incidents_24h": len(incidents)}


def api_items(ctx, cfg, store, q):
    return [{"code": i["code"], "kind": i["kind"], "pr": i["pr"], "title": i["title"],
             "body_md": i["body_md"], "created_at": _iso(i["created_at"])} for i in store.open_items()]


def api_prs(ctx, cfg, store, q):
    snaps = {n: d for n, d in store.snapshots().items() if d.get("state") == "OPEN"}
    heads = {d["head_ref"]: n for n, d in snaps.items()}
    out = []
    for n, d in sorted(snaps.items()):
        batches = store.batches(pr=n)
        cur = [b for b in batches if b["status"] not in TERMINAL_BATCH]
        last = (cur or batches or [None])[-1]
        tl = store.timeline(n, 1)
        warnings = []
        paused = store.kv_get(f"paused_pr:{n}")
        if paused:
            warnings.append(f"PR đang tạm dừng ({paused}).")
        if d["mergeable"] == "CONFLICTING":
            warnings.append("PR đang conflict với base.")
        parent = heads.get(d["base_ref"])
        if parent is not None and parent != n:
            warnings.append(f"PR xếp chồng trên #{parent}.")
        out.append({"number": n, "title": d["title"], "url": d["url"], "head": d["head_ref"],
                    "base": d["base_ref"], "draft": d["is_draft"], "review": d["review_decision"],
                    "conflict": d["mergeable"] == "CONFLICTING", "failed_checks": d["failed_checks"],
                    "batch_status": last["status"] if last else None,
                    "last_action": tl[0]["text"] if tl else None, "warnings": warnings})
    return out


def _int(v) -> int:
    try:
        return int(v or 0)
    except ValueError:
        return 0


def _last_run(store: Store, path: str, working: bool) -> dict | None:
    raw = store.kv_get(f"run:{path}")
    if not raw:
        return None
    try:
        run = json.loads(raw)
        if not isinstance(run, dict):
            return None
    except ValueError:
        return None
    cost, ended = None, None
    out = Path(run["out"]) if run.get("out") else None
    if out is not None and not working:
        try:
            st = out.stat()
            if st.st_size:
                ended = datetime.fromtimestamp(st.st_mtime).astimezone().isoformat()
            d = json.loads(out.read_text(errors="replace"))
            if isinstance(d, dict) and isinstance(d.get("total_cost_usd"), (int, float)):
                cost = d["total_cost_usd"]
        except (OSError, ValueError):
            pass
    return {"started_at": run.get("started_at"), "ended_at": ended, "cost_usd": cost,
            "attempts": _int(store.kv_get(f"attempt:{run.get('batch')}"))}


def api_workers(ctx, cfg, store, q):
    try:
        worktrees = ctx.worktree_lister(cfg)
    except Exception:
        raise ApiError(409, "Không đọc được danh sách worktree.") from None
    transport = ctx.transport_factory(cfg, store)
    transport.set_worktrees([w.path for w in worktrees.values()])  # before status()
    prs = {d["head_ref"]: n for n, d in store.snapshots().items() if d.get("state") == "OPEN"}
    out = []
    for branch, wt in sorted(worktrees.items()):
        st = transport.status(wt)
        out.append({"branch": branch, "pr": prs.get(branch), "path": wt.path, "state": st.state,
                    "session": st.session, "last_run": _last_run(store, wt.path, st.state == "working")})
    return out


def api_timeline(ctx, cfg, store, q):
    pr = (q.get("pr") or [None])[0]
    try:
        pr_n = int(pr) if pr not in (None, "") else None
    except ValueError:
        raise ApiError(400, "pr phải là số.") from None
    return [{**t, "at": _iso(t["at"])} for t in store.timeline(pr_n)]


def api_backlog(ctx, cfg, store, q):
    f = cfg.data_dir / "backlog.md"
    text = f.read_text(encoding="utf-8") if f.exists() else ""
    entries = []
    for line in text.splitlines():
        m = re.match(r"- \[([^\]]+)\] PR #(\d+) · (\S+) · (.*)", line)
        if m:
            entries.append({"kind": m[1], "pr": int(m[2]), "date": m[3], "text": m[4]})
    return {"entries": entries, "text": text}


def api_settings_get(ctx, cfg, store, q):
    return {k: get_setting(cfg, k) for k in sorted(EDITABLE_KEYS)}


def api_digest(ctx, cfg, store, q):
    since = _now() - timedelta(hours=24)
    recent = [{**t, "at": _iso(t["at"])} for t in store.timeline(limit=200) if t["at"] >= since]
    return {"summary": ops.digest_summary(store), "recent": recent}


def api_projects(ctx, cfg, store, q):  # handled without a project
    return list_projects(ctx.config_dir)


# ---- POST handlers ----------------------------------------------------------------
def _need(body: dict, key: str, typ=str, *, optional=False, default=""):
    v = body.get(key, default if optional else None)
    if v is None or isinstance(v, bool) and typ is not bool or not isinstance(v, typ):
        raise ApiError(400, f"Thiếu hoặc sai kiểu trường '{key}'.")
    return v


def _decision(fn, code, *a, **kw):
    try:
        return {"batch": fn(*a, **kw)}
    except KeyError:
        raise ApiError(404, f"Không tìm thấy mã {code}.") from None
    except ItemClosed:
        raise ApiError(409, f"Mã {code} đã được trả lời trước đó.") from None
    except ValueError as e:
        raise ApiError(400, str(e)) from None


def api_answer(ctx, cfg, store, q, body):
    code = _need(body, "code")
    choice = _need(body, "choice")
    if not choice.strip():
        raise ApiError(400, "Thiếu lựa chọn.")
    return _decision(answers.answer, code, store, cfg, code, choice, _need(body, "note", optional=True),
                     reply_hint=_need(body, "reply_hint", optional=True),
                     source=_need(body, "source", optional=True))


def api_approve(ctx, cfg, store, q, body):
    code = _need(body, "code")
    ok = _need(body, "ok", bool)
    return _decision(answers.approve, code, store, cfg, code, ok, _need(body, "note", optional=True))


def _pr_arg(body):
    pr = body.get("pr")
    if pr is None:
        return None
    if isinstance(pr, bool) or not isinstance(pr, int):
        raise ApiError(400, "pr phải là số.")
    return pr


def api_pause(ctx, cfg, store, q, body):
    ops.pause(store, _pr_arg(body))
    return {"ok": True}


def api_resume(ctx, cfg, store, q, body):
    return {"ok": True, "requeued": ops.resume(store, _pr_arg(body), _now().date())}


def api_settings_post(ctx, cfg, store, q, body):
    key = _need(body, "key")
    if key not in EDITABLE_KEYS:
        raise ApiError(400, f"Khóa {key} không được phép sửa.")
    value = body.get("value")
    try:
        validate_value(key, value)
        set_override(cfg.name, key, value, ctx.config_dir)
    except (ValueError, TypeError) as e:
        raise ApiError(400, str(e)) from None
    return {"ok": True, "key": key, "value": value}


def api_qa(ctx, cfg, store, q, body):
    try:
        path = ops.save_qa(cfg, _need(body, "name"), _need(body, "text"))
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    return {"ok": True, "path": str(path)}


def api_push_decisions(ctx, cfg, store, q, body):
    from ..github import AuthError, CmdError
    issue = body.get("issue")
    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
        raise ApiError(400, "issue phải là số nguyên dương.")
    try:
        url = answers.push_decisions(store, cfg, ctx.github_factory(cfg), issue, _now())
    except (ValueError, RuntimeError, CmdError, AuthError) as e:
        raise ApiError(409, f"Không đẩy được quyết định: {e}") from None
    return {"ok": True, "url": url}


GET_ROUTES = {"/api/overview": api_overview, "/api/items": api_items, "/api/prs": api_prs,
              "/api/workers": api_workers, "/api/timeline": api_timeline, "/api/backlog": api_backlog,
              "/api/settings": api_settings_get, "/api/digest": api_digest}
POST_ROUTES = {"/api/answer": api_answer, "/api/approve": api_approve, "/api/pause": api_pause,
               "/api/resume": api_resume, "/api/settings": api_settings_post, "/api/qa": api_qa,
               "/api/push-decisions": api_push_decisions}


class Handler(BaseHTTPRequestHandler):
    server_version = "Autopilot"

    def log_message(self, *a):  # quiet
        pass

    @property
    def ctx(self) -> Ctx:
        return self.server.ctx  # type: ignore[attr-defined]

    # ---- plumbing
    def _send(self, status: int, payload, headers: dict | None = None, raw: bytes | None = None,
              ctype: str = "application/json; charset=utf-8"):
        data = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, msg: str):
        self._send(status, {"error": msg})

    def _allowed_origins(self) -> tuple[str, ...]:
        port = self.server.server_address[1]
        return (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    def _authenticate(self, query: dict) -> tuple[bool, bool]:
        """(ok, via_query)."""
        tok = self.ctx.token.encode()

        def same(c: str | None) -> bool:
            return bool(c) and hmac.compare_digest(c.encode(), tok)

        if same(self.headers.get("X-Autopilot-Token")):
            return True, False
        ck = SimpleCookie()
        try:
            ck.load(self.headers.get("Cookie", ""))
        except Exception:
            pass
        if COOKIE in ck and same(ck[COOKIE].value):
            return True, False
        if same((query.get("token") or [None])[0]):
            return True, True
        return False, False

    def _dispatch(self, method: str):
        try:
            u = urlsplit(self.path)
            query = parse_qs(u.query)
            host = self.headers.get("Host", "")
            if host not in tuple(o[len("http://"):] for o in self._allowed_origins()):
                return self._error(403, "Host không hợp lệ.")
            ok, via_query = self._authenticate(query)
            if not ok:
                return self._error(401, "Thiếu hoặc sai token.")
            cookie = {"Set-Cookie": f"{COOKIE}={self.ctx.token}; HttpOnly; SameSite=Strict; Path=/"} \
                if via_query else None
            query.pop("token", None)
            if method == "GET":
                return self._get(u.path, query, cookie)
            return self._post(u.path, query, cookie)
        except ApiError as e:
            self._error(e.status, e.message)
        except Exception:
            self._error(500, "Lỗi nội bộ của máy chủ.")  # never leak a traceback

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _run(self, fn, query, cookie, body=None):
        cfg, store = self.ctx.project(query, body)
        try:
            res = fn(self.ctx, cfg, store, query) if body is None else fn(self.ctx, cfg, store, query, body)
        finally:
            store.close()
        self._send(200, res, cookie)

    def _get(self, path, query, cookie):
        if path == "/api/projects":
            return self._send(200, list_projects(self.ctx.config_dir), cookie)
        if path in GET_ROUTES:
            return self._run(GET_ROUTES[path], query, cookie)
        if path in ("/", "/static/index.html"):
            return self._static("index.html", cookie)
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):], cookie)
        raise ApiError(404, "Không tìm thấy đường dẫn.")

    def _static(self, rel: str, cookie):
        root = STATIC_DIR.resolve()
        f = (root / rel).resolve()
        if root not in f.parents or not f.is_file():
            raise ApiError(404, "Không tìm thấy tệp.")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, None, cookie, raw=f.read_bytes(), ctype=ctype)

    def _post(self, path, query, cookie):
        fn = POST_ROUTES.get(path)
        if fn is None:
            raise ApiError(404, "Không tìm thấy đường dẫn.")
        allowed = self._allowed_origins()
        origin, referer = self.headers.get("Origin"), self.headers.get("Referer")
        if origin is not None and origin not in allowed:
            raise ApiError(403, "Nguồn yêu cầu (Origin) không hợp lệ.")
        if referer is not None and not any(referer == a or referer.startswith(a + "/") for a in allowed):
            raise ApiError(403, "Nguồn yêu cầu (Referer) không hợp lệ.")
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            raise ApiError(415, "Nội dung phải là application/json.")
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise ApiError(411, "Thiếu Content-Length.") from None
        if n < 0 or n > MAX_BODY:
            raise ApiError(413, "Nội dung quá lớn.")
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8") or "null")
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Nội dung JSON không hợp lệ.") from None
        if not isinstance(body, dict):
            raise ApiError(400, "Nội dung phải là một đối tượng JSON.")
        self._run(fn, query, cookie, body)


def make_server(config_dir: Path, host: str = "127.0.0.1", port: int = 8787, *, token: str,
                transport_factory=None, github_factory=None, worktree_lister=None) -> ThreadingHTTPServer:
    if host != "127.0.0.1":
        raise ValueError("Dashboard chỉ được nghe trên 127.0.0.1.")
    if not token:
        raise ValueError("Thiếu token.")
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.ctx = Ctx(config_dir, token, transport_factory=transport_factory,  # type: ignore[attr-defined]
                  github_factory=github_factory, worktree_lister=worktree_lister)
    return srv


def serve(config_dir: Path = DEFAULT_CONFIG_DIR, port: int = 8787, token_path: Path = TOKEN_FILE,
          demo: bool = False) -> int:  # pragma: no cover
    kw = {}
    if demo:  # temp DB + temp config + fake workers; never touches real config, gh, git or claude
        from .demo import build_demo
        config_dir, tf, wl, root = build_demo()
        kw = {"transport_factory": tf, "worktree_lister": wl}
        token = secrets.token_urlsafe(32)
        print(f"Chế độ demo: dữ liệu giả nằm trong {root}")
    else:
        token = load_token(token_path)
    srv = make_server(config_dir, "127.0.0.1", port, token=token, **kw)
    port = srv.server_address[1]
    if demo or sys.stdout.isatty():
        print(f"Dashboard: http://127.0.0.1:{port}/?token={token}")
    else:
        print(f"Dashboard: http://127.0.0.1:{port}/ (token trong {token_path})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
