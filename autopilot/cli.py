"""`autopilot` command line: owner commands and worker commands. Owner-facing output is Vietnamese."""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import answers, router
from .config import DEFAULT_CONFIG_DIR, EDITABLE_KEYS, Config, list_projects, load_config, set_override
from .github import AuthError, CmdError
from .render import pending_md
from .store import InvalidTransition, ItemClosed, Store


# ---- injectable factories (tests monkeypatch these) -----------------------------
def _github(cfg: Config):
    from .github import GitHub
    return GitHub(cfg)


def _transport(cfg: Config, store: Store):
    from .transport import get_transport
    return get_transport(cfg, store)


def _notifier(cfg: Config, store: Store):
    from .notify import Notifier
    return Notifier(cfg, store)


def _now() -> datetime:
    return datetime.now().astimezone()


def _git(args: list[str], cwd) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def _err(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 1


class _Fail(Exception):
    pass


def _ctx(args) -> tuple[Config, Store]:
    cd = Path(args.config_dir) if args.config_dir else DEFAULT_CONFIG_DIR
    name = args.project
    if not name:
        projects = list_projects(cd)
        if len(projects) != 1:
            raise _Fail("Chưa chọn dự án: dùng --project <tên> "
                        f"(hiện có: {', '.join(projects) or 'không có dự án nào'}).")
        name = projects[0]
    try:
        cfg = load_config(name, cd)
    except (FileNotFoundError, ValueError) as e:
        raise _Fail(f"Không đọc được cấu hình dự án '{name}': {e}") from e
    return cfg, Store(cfg.db_path)


# ---- owner commands -------------------------------------------------------------
def cmd_init(args) -> int:
    cd = Path(args.config_dir) if args.config_dir else DEFAULT_CONFIG_DIR
    try:
        cfg = load_config(args.name, cd)
    except (FileNotFoundError, ValueError) as e:
        return _err(f"Không đọc được cấu hình dự án '{args.name}': {e}")
    for d in (cfg.data_dir, cfg.events_dir, cfg.plans_dir, cfg.qa_dir, cfg.screenshots_dir,
              cfg.data_dir / "runs", cfg.data_dir / "locks", cfg.data_dir / "results"):
        d.mkdir(parents=True, exist_ok=True)
    p = _git(["rev-parse", "--git-common-dir"], cfg.repo)
    if p.returncode != 0:
        return _err(f"{cfg.repo} không phải git repo: {p.stderr.strip()}")
    common = Path(p.stdout.strip())
    common = common if common.is_absolute() else cfg.repo / common
    ex = common / "info" / "exclude"
    ex.parent.mkdir(parents=True, exist_ok=True)
    text = ex.read_text(encoding="utf-8") if ex.exists() else ""
    if ".autopilot/" not in text.splitlines():
        ex.write_text(text + ("" if not text or text.endswith("\n") else "\n") + ".autopilot/\n", encoding="utf-8")
        print("Đã thêm .autopilot/ vào .git/info/exclude.")
    else:
        print(".autopilot/ đã có trong .git/info/exclude.")
    try:
        _github(cfg).token()
        print(f"Token GitHub của {cfg.gh_user}: OK.")
    except AuthError as e:
        return _err(f"Không lấy được token GitHub: {e}")
    print(f"Đã tạo thư mục trong {cfg.data_dir}.")
    print("Chưa bật chạy nền. Để bật bằng systemd (Task 13), cài các file trong systemd/ rồi chạy:\n"
          "  systemctl --user enable --now autopilot.timer")
    return 0


def cmd_status(args) -> int:
    cfg, store = _ctx(args)
    counts: dict[str, int] = {}
    for b in store.batches():
        counts[b["status"]] = counts.get(b["status"], 0) + 1
    paused = store.kv_get("paused")
    print(f"Dự án: {cfg.name}")
    print(f"Tạm dừng toàn bộ: {'có (' + paused + ')' if paused else 'không'}")
    print(f"PR theo dõi: {sum(1 for d in store.snapshots().values() if d['state'] == 'OPEN')}")
    print(f"Việc chờ owner: {len(store.open_items())}")
    print("Lô: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "không có"))
    lim = store.kv_get("limit_until")
    if lim:
        print(f"Giới hạn sử dụng đến: {lim}")
    return 0


def cmd_pending(args) -> int:
    _, store = _ctx(args)
    print(pending_md(store.open_items()), end="")
    return 0


def cmd_backlog(args) -> int:
    cfg, _ = _ctx(args)
    f = cfg.data_dir / "backlog.md"
    print(f.read_text(encoding="utf-8") if f.exists() else "Backlog trống.", end="")
    return 0


def _describe(a) -> str:
    from .routing import Deliver, Incident, Queue, Start
    if isinstance(a, Deliver):
        return f"giao lô {a.batch_id} cho {a.worktree.branch} (resume)"
    if isinstance(a, Start):
        return f"mở worker mới cho lô {a.batch_id} ở {a.worktree.branch}"
    if isinstance(a, Queue):
        return f"lô {a.batch_id} xếp hàng ({a.reason})"
    if isinstance(a, Incident):
        return f"sự cố lô {a.batch_id}: {a.text}"
    if isinstance(a, router.Resume):
        return f"giao lại lô {a.batch_id}"
    if isinstance(a, router.Escalate):
        return f"escalate lô {a.batch_id}"
    return str(a)


def _export_events(args) -> int:
    """Read-only: fetch one PR (any state), detect with prev=None, write the event file to --out."""
    import types
    from . import detect
    from .render import event_file
    cd = Path(args.config_dir) if args.config_dir else DEFAULT_CONFIG_DIR
    name = args.project
    if not name:
        projects = list_projects(cd)
        if len(projects) != 1:
            return _err("Chưa chọn dự án: dùng --project <tên>.")
        name = projects[0]
    try:
        cfg = load_config(name, cd)
    except (FileNotFoundError, ValueError) as e:
        return _err(f"Không đọc được cấu hình dự án '{name}': {e}")
    snap = _github(cfg).pr_by_number(args.pr)
    if snap is None:
        return _err(f"Không tìm thấy PR #{args.pr}.")
    now = _now()
    if args.rewind:
        snap = _rewind(snap, cfg.author)
    events = detect.detect(None, snap, cfg, now)
    if snap.state != "OPEN":  # detect() yields nothing for closed PRs; export their comments anyway
        events = detect.detect(None, _as_open(snap), cfg, now)
    meta: dict = {"force_c2_threads": [], "pushback": {}, "force_c2": []}
    for e in events:
        tid, pb = e.payload.get("thread_id"), int(e.payload.get("pushback") or 0)
        if e.kind == "thread" and tid:
            meta["pushback"][tid] = max(meta["pushback"].get(tid, 0), pb)
            if pb >= 2:
                if tid not in meta["force_c2_threads"]:
                    meta["force_c2_threads"].append(tid)
                meta["force_c2"].append(e.id)
    out = Path(args.out)
    proxy = types.SimpleNamespace(name=cfg.name, events_dir=out)  # event_file only reads these two
    path = event_file(proxy, {"id": "eval"}, events, snap, meta)
    print(f"[export] {len(events)} sự kiện -> {path}")
    return 0


def _as_open(snap):
    from dataclasses import replace
    return replace(snap, state="OPEN", mergeable="UNKNOWN", failed_checks=())


def _rewind(snap, author: str):
    """The PR as the reviewer left it: threads reopened and cut before the author's first reply."""
    from dataclasses import replace
    from .models import Thread
    threads = []
    for t in snap.threads:
        cs = []
        for c in t.comments:
            if c.author == author:
                break
            cs.append(c)
        if cs:
            threads.append(Thread(id=t.id, is_resolved=False, comments=tuple(cs)))
    mine = [c.created_at for t in snap.threads for c in t.comments if c.author == author]
    mine += [r.submitted_at for r in snap.reviews if r.author == author]
    mine += [c.created_at for c in snap.comments if c.author == author]
    cut = min(mine, default=None)  # reviews/comments after the author's first move are follow-ups
    before = (lambda ts: cut is None or ts < cut)
    return replace(snap, threads=tuple(threads),
                   reviews=tuple(r for r in snap.reviews if r.author != author and before(r.submitted_at)),
                   comments=tuple(c for c in snap.comments if c.author != author and before(c.created_at)))


def cmd_scan(args) -> int:
    if args.export_events:
        if not (args.dry_run and args.pr is not None and args.out) or args.claim:
            print("--export-events cần --dry-run, --pr N và --out DIR (không dùng với --claim).", file=sys.stderr)
            return 2
        return _export_events(args)
    if args.claim and args.pr is None:
        return _err("--claim cần đi kèm --pr N.")
    if args.claim and args.dry_run:
        print("--dry-run không dùng chung với --claim (claim luôn ghi trạng thái).", file=sys.stderr)
        return 2
    cfg, store = _ctx(args)
    r = router.scan(cfg, store, _github(cfg), _transport(cfg, store), _notifier(cfg, store), _now(),
                    dry_run=args.dry_run, only_pr=args.pr, claim=args.claim)
    if r.locked:
        print("Đang có lượt quét khác chạy, bỏ qua.")
        return 0
    print(f"{'[dry-run] ' if args.dry_run else ''}Quét xong: {len(r.new_events)} sự kiện mới, "
          f"{len(r.batches)} lô, {len(r.actions)} hành động, {len(r.incidents)} sự cố.")
    if args.claim:
        for bid in r.batches:
            pr = store.batch(bid)["pr"]
            print(f"batch_id: {bid}")
            print(f"Lô {bid} đã tạo, session manual, không spawn worker. File sự kiện: "
                  f"{cfg.events_dir / f'{pr}-{bid}.md'}")
        if not r.batches:
            print("Không có lô nào được tạo (không có sự kiện mới hoặc PR đang có lô chạy).")
    for a in r.actions:
        print(f"  - {_describe(a)}")
    for line in r.health:
        print(f"  Cảnh báo worker: {line}" if line.startswith("alert:") else f"  Worker: {line}")
    for i in r.incidents:
        if not i.startswith("alert:"):
            print(f"  ! {i}")
    for w in r.warnings:
        print(f"  ? {w}")
    return 0


def cmd_pause(args) -> int:
    _, store = _ctx(args)
    if args.pr is None:
        store.kv_set("paused", "1")
        print("Đã tạm dừng router: vẫn quét và gom sự kiện, nhưng không giao việc cho worker.")
    else:
        store.kv_set(f"paused_pr:{args.pr}", "owner")
        print(f"Đã tạm dừng PR #{args.pr}.")
    return 0


def cmd_resume(args) -> int:
    _, store = _ctx(args)
    if args.pr is None:
        store.kv_set("paused", None)
        store.kv_set("auth_alerted", None)
        print("Đã tiếp tục router.")
        return 0
    pr = args.pr
    by_cap = store.kv_get(f"paused_pr:{pr}") == "limit"
    store.kv_set(f"paused_pr:{pr}", None)
    n = 0
    if by_cap:  # events of batches cancelled by the wake cap go back to the queue
        cancelled = {i for b in store.batches(status="cancelled", pr=pr) for i in b["event_ids"]}
        ids = [e.id for e in store.events(status="batched", pr=pr) if e.id in cancelled]
        store.mark_events(ids, "new")
        store.reset_wake(pr, _now().date())
        n = len(ids)
    print(f"Đã tiếp tục PR #{pr}." + (f" Đưa lại {n} sự kiện vào hàng đợi." if n else ""))
    return 0


def _decision(fn, code: str, *a, **kw) -> tuple[int | None, int]:
    """(batch id, 0) on success, (None, exit code) after printing an error."""
    try:
        return fn(*a, **kw), 0
    except KeyError:
        return None, _err(f"Không tìm thấy mã {code}.")
    except ItemClosed:
        return None, _err(f"Mã {code} đã được trả lời trước đó, không ghi lại.")
    except ValueError as e:
        return None, _err(str(e))


def cmd_answer(args) -> int:
    cfg, store = _ctx(args)
    bid, rc = _decision(answers.answer, args.code, store, cfg, args.code, args.choice,
                        args.note or "", reply_hint=args.reply or "", source=args.source or "")
    if bid is None:
        return rc
    print(f"Đã ghi câu trả lời cho {args.code}: {args.choice}. Lô #{bid} sẽ được giao ở lượt quét tới.")
    return 0


def _approve(args, ok: bool) -> int:
    cfg, store = _ctx(args)
    bid, rc = _decision(answers.approve, args.code, store, cfg, args.code, ok, args.note or "")
    if bid is None:
        return rc
    print(f"Đã ghi {'duyệt' if ok else 'từ chối'} cho {args.code}. Lô #{bid} sẽ được giao ở lượt quét tới.")
    return 0


def cmd_ok(args) -> int:
    return _approve(args, True)


def cmd_no(args) -> int:
    return _approve(args, False)


def cmd_qa(args) -> int:
    cfg, _ = _ctx(args)
    name = Path(args.name).name if args.name else _now().strftime("%Y%m%d-%H%M%S")
    path = cfg.qa_dir / (name if name.endswith(".md") else f"{name}.md")
    from .render import atomic_write
    atomic_write(path, sys.stdin.read())
    print(f"Đã lưu {path}.")
    return 0


def _cfg_get(cfg: Config, key: str):
    head, _, rest = key.partition(".")
    if not hasattr(cfg, head):
        raise KeyError(key)
    v = getattr(cfg, head)
    if rest:
        if not isinstance(v, dict) or rest not in v:
            raise KeyError(key)
        v = v[rest]
    return v


def cmd_config(args) -> int:
    cd = Path(args.config_dir) if args.config_dir else DEFAULT_CONFIG_DIR
    cfg, _ = _ctx(args)
    if args.action == "get":
        try:
            print(_cfg_get(cfg, args.key))
        except KeyError:
            return _err(f"Không có khóa cấu hình: {args.key}")
        return 0
    if args.key not in EDITABLE_KEYS:
        return _err(f"Khóa {args.key} không được phép sửa. Được phép: {', '.join(sorted(EDITABLE_KEYS))}.")
    if args.value is None:
        return _err("Thiếu giá trị.")
    value: int | str = int(args.value) if args.value.lstrip("-").isdigit() else args.value
    try:
        set_override(cfg.name, args.key, value, cd)
    except (KeyError, TypeError) as e:
        return _err(f"Không đặt được {args.key}: {e}")
    print(f"Đã đặt {args.key} = {value}.")
    return 0


def cmd_push_decisions(args) -> int:
    cfg, store = _ctx(args)
    try:
        url = answers.push_decisions(store, cfg, _github(cfg), args.issue, _now())
    except (ValueError, RuntimeError, CmdError, AuthError) as e:
        return _err(f"Không đẩy được quyết định: {e}")
    print(f"Đã đẩy quyết định lên issue #{args.issue}: {url}")
    return 0


def cmd_web(args) -> int:
    try:
        from .web import server  # Task 15
    except ImportError:
        return _err("Dashboard web chưa có (Task 15).")
    cfg, store = _ctx(args)
    return server.main(cfg, store)  # pragma: no cover


# ---- worker commands ------------------------------------------------------------
def _batch(store: Store, bid: int) -> dict:
    try:
        return store.batch(bid)
    except KeyError:
        raise _Fail(f"Không tìm thấy lô {bid}.") from None


def _move(store: Store, bid: int, *path: str) -> None:
    now = _now()
    for to in path:
        cur = store.batch(bid)["status"]
        if cur == to:
            continue
        try:
            store.transition(bid, to, now)
        except InvalidTransition:
            raise _Fail(f"Lô {bid} đang ở trạng thái {cur}, không chuyển được sang {to}.") from None


def cmd_ack(args) -> int:
    _, store = _ctx(args)
    b = _batch(store, args.batch)
    if b["status"] not in ("acked", "in_progress"):
        _move(store, args.batch, "acked")
    print(f"Lô {args.batch}: đã ack.")
    return 0


def cmd_start(args) -> int:
    _, store = _ctx(args)
    b = _batch(store, args.batch)
    _move(store, args.batch, *(("acked", "in_progress") if b["status"] == "delivered" else ("in_progress",)))
    print(f"Lô {args.batch}: đang xử lý.")
    return 0


def cmd_item(args) -> int:
    _, store = _ctx(args)
    b = _batch(store, args.batch)
    body = Path(args.file).read_text(encoding="utf-8")
    code = store.add_item(args.kind, b["pr"], args.batch, args.title, body, _now())
    print(code)
    return 0


def cmd_result(args) -> int:
    cfg, store = _ctx(args)
    b = _batch(store, args.batch)
    text = Path(args.file).read_text(encoding="utf-8")
    if b["status"] in ("delivered", "acked"):
        _move(store, args.batch, "acked", "in_progress")
    _move(store, args.batch, args.status)
    from .render import atomic_write
    atomic_write(cfg.data_dir / "results" / f"{args.batch}.md", text)
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    store.log(b["pr"], "result", f"Lô #{args.batch} {args.status}: {first[:200]}", _now())
    print(f"Lô {args.batch}: {args.status}.")
    return 0


def cmd_backlog_add(args) -> int:
    cfg, _ = _ctx(args)
    try:
        answers.add_backlog(cfg, args.kind, args.pr, " ".join(args.text), _now())
    except ValueError as e:
        return _err(str(e))
    print("Đã thêm vào backlog.")
    return 0


def cmd_lock(args) -> int:
    _, store = _ctx(args)
    if args.action == "acquire":
        if not store.acquire(args.name, args.holder, _now()):
            return _err(f"Khóa {args.name} đang do người khác giữ.")
        print(f"Đã giữ khóa {args.name}.")
    else:
        store.release(args.name, args.holder)
        print(f"Đã nhả khóa {args.name}.")
    return 0


def _staged_forbidden(cfg: Config, repo) -> tuple[list[str], str | None]:
    p = _git(["diff", "--cached", "--name-only"], repo)
    if p.returncode != 0:
        return [], p.stderr.strip()
    return [f for f in p.stdout.splitlines() if router.is_forbidden(f, cfg.forbidden)], None


def cmd_guard_staged(args) -> int:
    cfg, _ = _ctx(args)
    bad, err = _staged_forbidden(cfg, args.repo)
    if err:
        return _err(f"Không đọc được file staged: {err}")
    if bad:
        print("Các file bị cấm đang được stage, hãy bỏ stage:", file=sys.stderr)
        for f in bad:
            print(f"  {f}", file=sys.stderr)
        return 1
    print("Không có file cấm trong staged.")
    return 0


def cmd_push(args) -> int:
    cfg, store = _ctx(args)
    b = _batch(store, args.batch)
    snap = store.snapshot(b["pr"])
    if not snap:
        return _err(f"Chưa có snapshot cho PR #{b['pr']}; không biết nhánh đích.")
    if b["status"] in ("done", "escalated", "cancelled") or snap.get("state") != "OPEN":
        return _err(f"Lô {args.batch} đã kết thúc ({b['status']}) hoặc PR #{b['pr']} không còn mở: không push.")
    head = snap["head_ref"]
    repo = args.repo
    cur = _git(["branch", "--show-current"], repo)
    branch = cur.stdout.strip()
    if cur.returncode != 0 or branch != head:
        return _err(f"Nhánh hiện tại ({branch or 'không xác định'}) khác nhánh của PR ({head}): không push.")
    bad, err = _staged_forbidden(cfg, repo)
    if err:
        return _err(f"Không đọc được file staged: {err}")
    if bad:
        return _err("Có file cấm đang được stage: " + ", ".join(bad))
    f = _git(["fetch", "origin", head], repo)
    if f.returncode != 0:
        return _err(f"Không cập nhật được origin/{head}: {f.stderr.strip()}")
    d = _git(["diff", "--name-only", f"origin/{head}..HEAD"], repo)
    if d.returncode != 0:
        return _err(f"Không so sánh được với origin/{head}: {d.stderr.strip()}")
    bad = [x for x in d.stdout.splitlines() if router.is_forbidden(x, cfg.forbidden)]
    if bad:
        return _err("Commit chứa file cấm: " + ", ".join(bad))
    if _git(["merge-base", "--is-ancestor", f"origin/{head}", "HEAD"], repo).returncode != 0:
        return _err(f"origin/{head} có commit mới mà nhánh local chưa có (cần force): không push. "
                    "Hãy rebase/merge origin trước.")
    p = _git(["push", "origin", f"HEAD:{head}"], repo)
    if p.returncode != 0:
        return _err(f"git push thất bại: {p.stderr.strip()}")
    sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    store.log(b["pr"], "pushed", f"pushed {sha}", _now())
    print(f"Đã push {sha[:10]} lên origin/{head}.")
    return 0


# ---- argument parsing -----------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default=argparse.SUPPRESS)
    common.add_argument("--config-dir", default=argparse.SUPPRESS)
    ap = argparse.ArgumentParser(prog="autopilot", description="Autopilot: theo dõi PR và điều phối worker.")
    ap.add_argument("--project", default=None, help="tên dự án (mặc định: dự án duy nhất)")
    ap.add_argument("--config-dir", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, **kw):
        sp = sub.add_parser(name, parents=[common], **kw)
        sp.set_defaults(fn=fn)
        return sp

    add("init", cmd_init).add_argument("name")
    add("status", cmd_status)
    add("pending", cmd_pending)
    add("backlog", cmd_backlog)
    sp = add("scan", cmd_scan)
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--pr", type=int)
    sp.add_argument("--claim", action="store_true")
    sp.add_argument("--export-events", action="store_true")
    sp.add_argument("--out")
    sp.add_argument("--rewind", action="store_true",
                    help="với --export-events: dựng lại PR như lúc reviewer vừa review (trước mọi reply của tác giả)")
    for n, fn in (("pause", cmd_pause), ("resume", cmd_resume)):
        add(n, fn).add_argument("pr", type=int, nargs="?")
    sp = add("answer", cmd_answer)
    sp.add_argument("code")
    sp.add_argument("choice")
    sp.add_argument("note", nargs="?", default="")
    sp.add_argument("--reply")
    sp.add_argument("--source")
    for n, fn in (("ok", cmd_ok), ("no", cmd_no)):
        sp = add(n, fn)
        sp.add_argument("code")
        sp.add_argument("note", nargs="?", default="")
    sp = add("qa", cmd_qa)
    sp.add_argument("action", choices=["paste"])
    sp.add_argument("--name")
    sp = add("config", cmd_config)
    sp.add_argument("action", choices=["get", "set"])
    sp.add_argument("key")
    sp.add_argument("value", nargs="?")
    add("push-decisions", cmd_push_decisions).add_argument("issue", type=int)
    add("web", cmd_web)
    add("ack", cmd_ack).add_argument("batch", type=int)
    add("start", cmd_start).add_argument("batch", type=int)
    sp = add("item", cmd_item)
    sp.add_argument("batch", type=int)
    sp.add_argument("kind", choices=["Q", "P", "R"])
    sp.add_argument("--title", required=True)
    sp.add_argument("--file", required=True)
    sp = add("result", cmd_result)
    sp.add_argument("batch", type=int)
    sp.add_argument("--file", required=True)
    sp.add_argument("--status", required=True, choices=["done", "waiting_owner"])
    sp = add("backlog-add", cmd_backlog_add)
    sp.add_argument("kind")
    sp.add_argument("pr", type=int)
    sp.add_argument("text", nargs="+")
    sp = add("lock", cmd_lock)
    sp.add_argument("action", choices=["acquire", "release"])
    sp.add_argument("name", choices=["testdb"])
    sp.add_argument("--holder", required=True)
    add("guard-staged", cmd_guard_staged).add_argument("--repo", default=".")
    sp = add("push", cmd_push)
    sp.add_argument("batch", type=int)
    sp.add_argument("--repo", default=".")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except _Fail as e:
        return _err(str(e))
    except (AuthError, CmdError) as e:
        return _err(f"Lỗi GitHub: {e}")
    except (OSError, UnicodeDecodeError) as e:
        return _err(f"Lỗi đọc/ghi file: {e}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
