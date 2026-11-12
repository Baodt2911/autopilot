"""`autopilot` command line: owner commands and worker commands. Owner-facing output is Vietnamese."""
from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import answers, ops, router
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
    if snap.state != "OPEN":  # detect() yields nothing for closed PRs; export their comments anyway
        snap = _as_open(snap)
    events = detect.detect(None, snap, cfg, now)
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
    from .render import atomic_write
    text = path.read_text(encoding="utf-8")
    atomic_write(path, text.split("\n## Lệnh phải chạy")[0].rstrip("\n") + "\n")  # eval mode runs no commands
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


def _scan_all(args) -> int:
    """Quét lần lượt mọi dự án; một dự án lỗi không chặn dự án sau, nhưng mã thoát phản ánh lỗi."""
    if args.project or args.pr is not None or args.claim or args.export_events:
        print("--all không dùng chung với --project, --pr, --claim, --export-events.", file=sys.stderr)
        return 2
    cd = Path(args.config_dir) if args.config_dir else DEFAULT_CONFIG_DIR
    names = list_projects(cd)
    if not names:
        return _err(f"Không có dự án nào trong {cd}.")
    rc = 0
    for name in names:
        print(f"== {name} ==")
        args.project, args.all = name, False
        try:
            rc |= cmd_scan(args)
        except _Fail as e:
            rc |= _err(str(e))
        except (AuthError, CmdError) as e:
            rc |= _err(f"Lỗi GitHub: {e}")
        except (OSError, UnicodeDecodeError) as e:
            rc |= _err(f"Lỗi đọc/ghi file: {e}")
    return rc


def cmd_scan(args) -> int:
    if args.all:
        return _scan_all(args)
    if (args.rewind or args.out) and not args.export_events:
        print("--rewind và --out chỉ dùng với --export-events.", file=sys.stderr)
        return 2
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
    ops.pause(store, args.pr)
    if args.pr is None:
        print("Đã tạm dừng router: vẫn quét và gom sự kiện, nhưng không giao việc cho worker.")
    else:
        print(f"Đã tạm dừng PR #{args.pr}.")
    return 0


def cmd_resume(args) -> int:
    _, store = _ctx(args)
    n = ops.resume(store, args.pr, _now().date())
    if args.pr is None:
        print("Đã tiếp tục router.")
    else:
        print(f"Đã tiếp tục PR #{args.pr}." + (f" Đưa lại {n} sự kiện vào hàng đợi." if n else ""))
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
    except (KeyError, TypeError, ValueError) as e:
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
    from .web import server
    cd = Path(args.config_dir) if args.config_dir else DEFAULT_CONFIG_DIR
    return server.serve(cd, port=args.port, demo=args.demo)  # pragma: no cover


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


def _canon_target(snap: dict | None, raw: str, pr: int) -> str:
    """Canonical C2 target: thread node id (PRRT_...) for review-thread comments, `pr:<n>` for the rest."""
    raw = (raw or "").strip()
    if not raw or raw.startswith("PRRT_") or raw.startswith("pr:"):
        return raw
    for t in (snap or {}).get("threads", []):
        if raw == t["id"]:
            return raw
        for c in t.get("comments", []):
            if raw == c["id"] or (raw.isdigit() and re.search(rf"discussion_r{raw}$", c.get("url") or "")):
                return t["id"]
    return f"pr:{pr}"  # not in any review thread: a top-level comment or review body


def cmd_item(args) -> int:
    cfg, store = _ctx(args)
    b = _batch(store, args.batch)
    body = Path(args.file).read_text(encoding="utf-8")
    code = store.add_item(args.kind, b["pr"], args.batch, args.title, body, _now(), target=_canon_target(store.snapshot(b["pr"]), args.target or "", b["pr"]))
    if args.kind == "P":  # the CLI keeps its own copy of the plan; the worker's scratch file is disposable
        from .render import atomic_write
        atomic_write(cfg.plans_dir / f"{code}.md", body)
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
    text = " ".join(args.text)
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    if not text.strip():
        return _err("Thiếu nội dung backlog (đối số text hoặc --file).")
    try:
        answers.add_backlog(cfg, args.kind, args.pr, text, _now())
    except ValueError as e:
        return _err(str(e))
    print("Đã thêm vào backlog.")
    return 0


def cmd_reply(args) -> int:
    cfg, store = _ctx(args)
    b = _batch(store, args.batch)
    pr = b["pr"]
    snap = store.snapshot(pr)
    if b["status"] in ("done", "escalated", "cancelled"):
        return _err(f"Lô {args.batch} đã kết thúc ({b['status']}): không reply.")
    if not snap or snap.get("state") != "OPEN":
        return _err(f"PR #{pr} không còn mở (hoặc chưa có snapshot): không reply.")
    if args.thread:
        target = args.thread
    elif args.comment is not None:
        target = _canon_target(snap, str(args.comment), pr)
    else:
        target = f"pr:{pr}"
    silenced = [i["code"] for i in store.open_items()
                if i["pr"] == pr and i["kind"] == "Q" and i.get("target") == target]
    if silenced:
        return _err(f"Thread đang chờ owner ({', '.join(silenced)}): im lặng, không reply (G7).")
    # the round counter is bumped when the batch is created, so the 3rd round itself must still reply: use ">"
    if target.startswith("PRRT_") and store.round_count(target) > cfg.limits["thread_rounds"]:
        return _err(f"Thread đã quá {cfg.limits['thread_rounds']} vòng reply (G9): không reply, hãy xếp C2.")
    body = Path(args.file).read_text(encoding="utf-8")
    if not body.strip():
        return _err("Nội dung reply rỗng.")
    gh = _github(cfg)
    if args.thread:
        url = gh.reply_to_thread(args.thread, body)
    elif args.comment is not None:
        url = gh.reply_to_comment(pr, args.comment, body)
    else:
        url = gh.post_issue_comment(pr, body)
    store.log(pr, "replied", f"replied {target} {url}", _now(), url)
    print(f"Đã reply: {url}")
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


DEFAULT_B_ONLY = ("**/migrations/**", "**/*.prisma", "**/schema*.sql", "**/openapi*.y*ml", "**/openapi*.json",
                  "docs/**/api-contract*.md", "docs/*api-contract*.md")


def _approved_plan(store: Store, pr: int) -> str | None:
    """Code of an approved P item for this PR, approved after the last push and not yet used."""
    pushed = [t["at"] for t in store.timeline(pr) if t["kind"] == "pushed"]
    last = max(pushed, default=None)
    for i in store.closed_items():
        if (i["kind"] == "P" and i["pr"] == pr and i["answer"] == "approved"
                and (last is None or i["resolved_at"] > last) and not store.kv_get(f"plan_used:{i['code']}")):
            return i["code"]
    return None


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
    # Own first-parent commits, merges included. dense-combined lists, for a merge, only files that differ
    # from every parent (conflict resolutions and extra edits), not files brought unchanged from the base.
    lg = _git(["log", "--first-parent", "--diff-merges=dense-combined", "--name-only", "--format=",
               f"origin/{head}..HEAD"], repo)
    if lg.returncode != 0:
        return _err(f"Không đọc được commit của worker: {lg.stderr.strip()}")
    changed = sorted({x for x in lg.stdout.splitlines() if x})
    pats = (cfg.pr_rules or {}).get("b_only_patterns")
    pats = DEFAULT_B_ONLY if pats is None else pats  # an explicit empty list disables the pattern check
    b_only = [x for x in changed if any(fnmatch.fnmatchcase(x.lower(), q.lower())
                                        or fnmatch.fnmatchcase("/" + x.lower(), q.lower()) for q in pats)]
    plan = None
    if len(changed) > 3 or b_only:
        plan = _approved_plan(store, b["pr"])
    if (len(changed) > 3 or b_only) and not plan:
        why = (f"chạm file chỉ cho mục B: {', '.join(b_only)}" if b_only
               else f"{len(changed)} file thay đổi (> 3)")
        return _err(f"Không push: {why}. Đây không phải mục A: hãy viết plan P "
                    "(`autopilot item <batch> P ...`) và chờ owner duyệt (`autopilot ok P-...`) rồi push lại.")
    if _git(["merge-base", "--is-ancestor", f"origin/{head}", "HEAD"], repo).returncode != 0:
        return _err(f"origin/{head} có commit mới mà nhánh local chưa có (cần force): không push. "
                    "Hãy rebase/merge origin trước.")
    p = _git(["push", "origin", f"HEAD:{head}"], repo)
    if p.returncode != 0:
        return _err(f"git push thất bại: {p.stderr.strip()}")
    if plan:
        store.kv_set(f"plan_used:{plan}", "1")  # one approved plan authorizes one push
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
    sp.add_argument("--all", action="store_true", help="quét mọi dự án trong thư mục cấu hình (dùng cho systemd)")
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
    sp = add("web", cmd_web)
    sp.add_argument("--port", type=int, default=8787)
    sp.add_argument("--demo", action="store_true", help="dữ liệu giả trong DB tạm")
    add("ack", cmd_ack).add_argument("batch", type=int)
    add("start", cmd_start).add_argument("batch", type=int)
    sp = add("item", cmd_item)
    sp.add_argument("batch", type=int)
    sp.add_argument("kind", choices=["Q", "P", "R"])
    sp.add_argument("--title", required=True)
    sp.add_argument("--file", required=True)
    sp.add_argument("--target", default="", help="id thread/comment mà mục này đang chặn (C2: im lặng trên đó)")
    sp = add("result", cmd_result)
    sp.add_argument("batch", type=int)
    sp.add_argument("--file", required=True)
    sp.add_argument("--status", required=True, choices=["done", "waiting_owner"])
    sp = add("backlog-add", cmd_backlog_add)
    sp.add_argument("kind")
    sp.add_argument("pr", type=int)
    sp.add_argument("text", nargs="*")
    sp.add_argument("--file")
    sp = add("reply", cmd_reply)
    sp.add_argument("batch", type=int)
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--thread")
    g.add_argument("--comment", type=int)
    g.add_argument("--pr-comment", action="store_true")
    sp.add_argument("--file", required=True)
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
