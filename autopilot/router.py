"""Router: one full scan pass (spec 4.1). Never merges, rebases, resolves threads or creates worktrees."""
from __future__ import annotations

import fcntl
import fnmatch
import json
import os
import sqlite3
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .batching import apply_limits, batch_meta, ready_batches
from .config import Config
from .detect import detect, stack_warnings
from .github import AuthError
from .health import check_workers, limit_active
from .models import Comment, Event, PRSnapshot, Review, Thread, Worktree
from .render import event_file, write_all
from .routing import Deliver, Incident, Queue, Start, route
from .store import InvalidTransition, Store
from .transport import TransportError, WorkerBusy
from .worktrees import list_worktrees

BACKUP_ROOT = Path.home() / ".autopilot-backup"
GH_FAIL_ALERT = timedelta(minutes=30)
MAX_ATTEMPTS = 3  # first delivery + 2 re-runs
_TERMINAL = frozenset({"done", "escalated", "cancelled"})
_ITEM_KIND = {"Q": "question", "P": "plan", "R": "draft"}


@dataclass(frozen=True)
class Resume:
    batch_id: int


@dataclass(frozen=True)
class Escalate:
    batch_id: int


@dataclass
class ScanReport:
    new_events: list[Event] = field(default_factory=list)
    batches: list[int] = field(default_factory=list)
    actions: list = field(default_factory=list)
    incidents: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    health: list[str] = field(default_factory=list)
    locked: bool = False  # another scan holds router.lock


# ---- snapshot (de)serialisation -------------------------------------------------
def snapshot_to_dict(s: PRSnapshot) -> dict:
    def conv(o):
        if isinstance(o, datetime):
            return o.isoformat()
        if is_dataclass(o):
            return {f.name: conv(getattr(o, f.name)) for f in fields(o)}
        if isinstance(o, (list, tuple)):
            return [conv(x) for x in o]
        return o

    return conv(s)


def _dt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


def snapshot_from_dict(d: dict) -> PRSnapshot:
    def comment(c: dict) -> Comment:
        return Comment(**{**c, "created_at": _dt(c["created_at"]), "updated_at": _dt(c["updated_at"])})

    return PRSnapshot(
        **{**d,
           "threads": tuple(Thread(t["id"], t["is_resolved"], tuple(comment(c) for c in t["comments"]))
                            for t in d["threads"]),
           "reviews": tuple(Review(**{**r, "submitted_at": _dt(r["submitted_at"])}) for r in d["reviews"]),
           "comments": tuple(comment(c) for c in d["comments"]),
           "failed_checks": tuple(d["failed_checks"]),
           "changed_files": tuple(d["changed_files"]),
           "merged_at": _dt(d.get("merged_at"))})


# ---- helpers --------------------------------------------------------------------
def is_forbidden(path: str, patterns) -> bool:
    p = path.removeprefix("./").lower()
    for pat in patterns:
        pat = pat.lower()
        if pat.endswith("/"):
            if p.startswith(pat):
                return True
        elif fnmatch.fnmatchcase(p, pat) or fnmatch.fnmatchcase(os.path.basename(p), pat):
            return True
    return False


def worker_prompt(cfg: Config, batch_id: int, event_path: Path) -> str:
    """Skill call + event file path + project name. Never any comment content."""
    return f"/pr-triage batch {batch_id} --project {cfg.name}\n{event_path}"


def _try_transition(store: Store, bid: int, to: str, now: datetime, **kw) -> bool:
    try:
        store.transition(bid, to, now, **kw)
        return True
    except (InvalidTransition, KeyError):
        return False


def _alert(notifier, store: Store, rw: bool, kind: str, title: str, body: str, now: datetime) -> None:
    if rw:
        notifier.alert(kind, title, body, now)


def _write_event_file(cfg: Config, store: Store, bid: int, snap: PRSnapshot) -> Path:
    b = store.batch(bid)
    by_id = {e.id: e for e in store.events(pr=b["pr"])}
    evs = [by_id[i] for i in b["event_ids"] if i in by_id]
    return event_file(cfg, b, evs, snap, batch_meta(store, bid))


def _launch(cfg, store, transport, notifier, wt: Worktree, session: str | None, bid: int,
            snap: PRSnapshot, now: datetime, report: ScanReport) -> bool:
    """Deliver (resume) or start a worker for a batch, then mark it delivered."""
    path = _write_event_file(cfg, store, bid, snap)
    prompt = worker_prompt(cfg, bid, path)
    try:
        if session:
            try:
                transport.deliver(wt, session, prompt, bid)
            except ValueError:  # session closed: start a fresh one
                session = None
        if not session:
            session = transport.start_worker(wt, prompt, bid)
    except WorkerBusy:
        return False
    except TransportError as e:
        msg = f"Không giao được lô {bid} ở {wt.branch}: {e}"
        report.incidents.append(msg)
        if store.kv_get(f"launch_err:{bid}") != str(e):
            store.kv_set(f"launch_err:{bid}", str(e))
            notifier.alert("incident", f"Lô {bid} không giao được", msg, now)
        return False
    _try_transition(store, bid, "delivered", now, session=session)
    store.log(snap.number, "delivered", f"Lô #{bid} đã giao cho worker ({wt.branch})", now, snap.url)
    return True


def _cancel_pr(store: Store, transport, wt: Worktree | None, snap: PRSnapshot, now: datetime,
               report: ScanReport) -> None:
    pr = snap.number
    store.mark_events([e.id for e in store.events(status="new", pr=pr)], "cancelled")
    session = None
    for b in store.batches(pr=pr):
        if b["status"] not in _TERMINAL and _try_transition(store, b["id"], "cancelled", now):
            session = session or b["session"]
            report.actions.append(f"cancel batch {b['id']}")
    if snap.state == "MERGED":
        store.log(pr, "merged", f"PR #{pr} đã merge", now, snap.url)
        if wt is not None:
            session = session if session and session != "manual" else (transport.status(wt, now).session or "")
            transport.stop_worker(wt, session, close=True)
            report.actions.append(f"stop worker {wt.branch}")
    else:
        store.log(pr, "closed", f"PR #{pr} đã đóng", now, snap.url)


def _backup(cfg: Config, store: Store, now: datetime) -> None:
    day = BACKUP_ROOT / cfg.name / now.astimezone().date().isoformat()
    if day.exists():
        return
    day.mkdir(parents=True)
    src = sqlite3.connect(store.path)
    try:
        dst = sqlite3.connect(day / "state.db")
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    for name in ("decisions.md", "backlog.md", "pending.md"):
        f = cfg.data_dir / name
        if f.exists():
            (day / name).write_bytes(f.read_bytes())


# ---- scan -----------------------------------------------------------------------
def scan(cfg: Config, store: Store, gh, transport, notifier, now: datetime, *,
         dry_run: bool = False, only_pr: int | None = None, claim: bool = False) -> ScanReport:
    report = ScanReport()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(cfg.data_dir / "router.lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        report.locked = True
        return report
    try:
        return _scan(cfg, store, gh, transport, notifier, now, report, dry_run, only_pr, claim)
    finally:
        os.close(fd)  # releases the flock


def _scan(cfg, store, gh, transport, notifier, now, report, dry_run, only_pr, claim) -> ScanReport:
    rw = not dry_run
    # 2. GitHub
    try:
        all_open = gh.open_prs()
    except AuthError as e:
        report.incidents.append(f"auth: {e}")
        if rw:
            store.kv_set("paused", "auth")
            if store.kv_get("auth_alerted") != "1":
                store.kv_set("auth_alerted", "1")
                notifier.alert("incident", "Không lấy được token GitHub",
                               f"Router tạm dừng. Kiểm tra `gh auth login`. ({e})", now)
        return report
    except Exception as e:
        report.warnings.append(f"GitHub lỗi: {e}")
        if rw:
            raw = store.kv_get("gh_fail_since")
            if raw is None:
                store.kv_set("gh_fail_since", now.isoformat())
                since = now
            else:
                since = datetime.fromisoformat(raw)
            if now - since >= GH_FAIL_ALERT and store.kv_get("gh_fail_alerted") != since.isoformat():
                store.kv_set("gh_fail_alerted", since.isoformat())
                notifier.alert("incident", "GitHub lỗi liên tục",
                               f"Không gọi được GitHub từ {since.isoformat()}: {e}", now)
        return report
    if rw:
        store.kv_set("gh_fail_since", None)
        store.kv_set("gh_fail_alerted", None)
        if store.kv_get("paused") == "auth":
            store.kv_set("paused", None)
            store.kv_set("auth_alerted", None)
    open_nums = {p.number for p in all_open}
    prs_open = [p for p in all_open if only_pr is None or p.number == only_pr]

    stored = store.snapshots()
    want = [n for n, d in stored.items()
            if d["state"] == "OPEN" and n not in open_nums and (only_pr is None or n == only_pr)]
    closed: list[PRSnapshot] = []
    if want:
        try:
            closed = [s for s in gh.recently_closed(want) if s.state != "OPEN"]
        except Exception as e:
            report.warnings.append(f"Không đọc được PR đã đóng: {e}")

    # 3 + 4. detect, persist events BEFORE the snapshot (R14)
    prev = {n: snapshot_from_dict(d) for n, d in stored.items()}
    known_ids = {e.id for e in store.events()} if dry_run else set()
    for s in [*prs_open, *closed]:
        evs = detect(prev.get(s.number), s, cfg, now)
        if rw:
            fresh = [e for e in evs if store.add_event(e)]
        else:
            fresh = [e for e in evs if e.id not in known_ids]
        report.new_events += fresh
        if rw:
            store.mark_events([e.id for e in fresh if e.kind == "merged"], "noop")
            store.save_snapshot(s.number, snapshot_to_dict(s))
    merged = [s for s in closed if s.state == "MERGED"]
    for w in stack_warnings(prev, {p.number: p for p in all_open}, merged):
        report.warnings.append(w)
        _alert(notifier, store, rw, "stack", "Cảnh báo PR xếp chồng", w, now)

    transport_paths = []
    try:
        worktrees = list_worktrees(cfg.repo)
    except Exception as e:
        report.warnings.append(f"Không đọc được worktree: {e}")
        worktrees = {}
    transport_paths = [w.path for w in worktrees.values()]
    transport.set_worktrees(transport_paths)  # R20

    if rw:
        for s in closed:
            _cancel_pr(store, transport, worktrees.get(s.head_ref), s, now, report)

    prs = {p.number: p for p in prs_open}
    paused_prs = {n for n in open_nums if store.kv_get(f"paused_pr:{n}")}
    limit = limit_active(store, now)
    if rw and not limit and store.kv_get("limit_until"):
        store.kv_set("limit_until", None)

    # 5. batches + limits
    if rw:
        created = ready_batches(store, cfg, now, skip_prs=paused_prs,
                                only_prs={only_pr} if only_pr is not None else None, force=claim)
        for bid in created:
            if apply_limits(store, cfg, bid, now) == "paused":
                pr = store.batch(bid)["pr"]
                msg = f"PR #{pr} vượt giới hạn đánh thức trong ngày: tạm dừng"
                report.incidents.append(msg)
                notifier.alert("incident", f"PR #{pr} tạm dừng", msg, now)
            else:
                report.batches.append(bid)
    else:
        report.batches = [b["id"] for b in store.batches(status=("new", "queued", "waiting_limit"))
                          if only_pr in (None, b["pr"])]

    if claim:  # manual hand-off: deliver to the owner's own session, spawn nothing
        for bid in report.batches:
            snap = prs.get(store.batch(bid)["pr"])
            if snap is None:
                continue
            _write_event_file(cfg, store, bid, snap)
            _try_transition(store, bid, "delivered", now, session="manual")
            store.log(snap.number, "delivered", f"Lô #{bid} giao thủ công (session manual)", now, snap.url)
        return _finish(cfg, store, notifier, now, report, prs_open, closed, rw)

    # 6. delivered-but-unacked / expired lease batches whose process is gone
    touched: set[str] = set()
    if not limit:
        stale = {b["id"]: b for b in [*store.unacked(now), *store.lease_expired(now)]}
        for bid in sorted(stale):
            b = stale[bid]
            snap = prs.get(b["pr"])
            wt = worktrees.get(snap.head_ref) if snap else None
            if only_pr not in (None, b["pr"]) or b["session"] == "manual" or wt is None or wt.branch in touched:
                continue
            st = transport.status(wt, now)
            if st.state not in ("idle", "waiting", "dead", "none"):
                continue  # live process (or owner session / limit): wait
            touched.add(wt.branch)
            if b["attempts"] >= MAX_ATTEMPTS:
                report.actions.append(Escalate(bid))
                if rw and _try_transition(store, bid, "escalated", now):
                    msg = f"Lô #{bid} (PR #{b['pr']}) không có phản hồi sau {MAX_ATTEMPTS} lần giao: đã escalate"
                    report.incidents.append(msg)
                    store.log(b["pr"], "escalated", msg, now)
                    notifier.alert("incident", f"Lô {bid} bị escalate", msg, now)
                continue
            report.actions.append(Resume(bid))
            if rw:
                _launch(cfg, store, transport, notifier, wt, b["session"] or st.session, bid, snap, now, report)

    # 7. route + execute
    cands = []
    by_id = {e.id: e for e in store.events()}
    for b in store.batches(status=("new", "queued", "waiting_limit")):
        if only_pr not in (None, b["pr"]):
            continue
        evs = [by_id[i] for i in b["event_ids"] if i in by_id]
        cands.append({**b, "priority": min((e.priority for e in evs), default=4),
                      "created_at": min((e.created_at for e in evs), default=now)})
    workers = {br: transport.status(w, now) for br, w in worktrees.items()}
    paused = bool(store.kv_get("paused")) or bool(limit)
    for a in route(cands, prs, worktrees, workers, cfg, paused=paused, paused_prs=paused_prs):
        if isinstance(a, (Deliver, Start)) and a.worktree.branch in touched:
            a = Queue(a.batch_id, "busy")
        report.actions.append(a)
        if not rw:
            continue
        b = store.batch(a.batch_id)
        if isinstance(a, Queue):
            if b["status"] == "new" and _try_transition(store, a.batch_id, "queued", now):
                store.log(b["pr"], "queued", f"Lô #{a.batch_id} xếp hàng: {a.reason}", now)
        elif isinstance(a, Incident):
            report.incidents.append(a.text)
            if store.kv_get(f"incident:{a.batch_id}") != a.text:
                store.kv_set(f"incident:{a.batch_id}", a.text)
                notifier.alert("incident", f"Lô {a.batch_id} không xử lý được", a.text, now)
        else:
            session = a.session if isinstance(a, Deliver) else None
            _launch(cfg, store, transport, notifier, a.worktree, session, a.batch_id, prs[b["pr"]], now, report)

    # 8. health
    if rw:
        wb = {}
        for br, w in worktrees.items():
            st = transport.status(w, now)
            if st.session:
                wb[br] = (w, st.session)
        for line in check_workers(store, transport, wb, now):
            report.health.append(line)
            if line.startswith("alert:"):
                report.incidents.append(line)
                notifier.alert("incident", "Worker cần chú ý", line, now)

    return _finish(cfg, store, notifier, now, report, prs_open, closed, rw)


def _finish(cfg, store, notifier, now, report, prs_open, closed, rw) -> ScanReport:
    # 9. post-checks
    for s in prs_open:
        bad = sorted(f for f in s.changed_files if is_forbidden(f, cfg.forbidden))
        if not bad:
            continue
        msg = f"PR #{s.number} chứa file bị cấm: {', '.join(bad)}"
        report.incidents.append(msg)
        sig = f"{s.head_sha}:{','.join(bad)}"
        if rw and store.kv_get(f"forbidden-pr:{s.number}") != sig:
            store.kv_set(f"forbidden-pr:{s.number}", sig)
            notifier.alert("incident", f"PR #{s.number} có file cấm", msg, now)
    if not rw:
        return report
    # 10. render
    write_all(cfg, store, [*prs_open, *closed])
    # 11. notifications
    notified = set(json.loads(store.kv_get("notified_items") or "[]"))
    for it in store.open_items():
        if it["code"] in notified:
            continue
        notifier.alert(_ITEM_KIND.get(it["kind"], "question"), f"{it['code']} · PR #{it['pr']}",
                       it.get("title") or "", now)
        notified.add(it["code"])
    store.kv_set("notified_items", json.dumps(sorted(notified)))
    notifier.reminders(now)
    active = len(store.batches(status=("delivered", "acked", "in_progress")))
    notifier.digest(now, f"{len(store.open_items())} việc chờ owner, {active} lô đang xử lý")
    # 12. daily backup
    try:
        _backup(cfg, store, now)
    except Exception as e:
        report.warnings.append(f"Backup lỗi: {e}")
    return report
