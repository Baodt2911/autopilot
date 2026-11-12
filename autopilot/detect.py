"""Pure detection layer: PRSnapshot (prev, cur) -> Event list, plus G15 stack warnings."""
from __future__ import annotations

import re
from datetime import datetime

from .config import Config
from .models import Comment, Event, Review, PRSnapshot, Thread

_NOOP_RE = re.compile(r"^[\W_]*(lgtm|approved)[\W_]*$", re.IGNORECASE)


def is_noop(body: str) -> bool:
    """True only for a bare "LGTM" / "approved" (any case), optionally with emoji/punctuation."""
    return bool(_NOOP_RE.match(body or ""))


def _comment_event(c: Comment, pr: int, kind: str, now: datetime, extra: dict) -> Event:
    payload = {
        "comment_id": c.id, "author": c.author, "is_bot": c.is_bot, "body": c.body,
        "url": c.url, "path": c.path, "line": c.line, "thread_id": c.thread_id,
        **extra,
    }
    if is_noop(c.body):
        kind = "noop"
    return Event(id=f"{c.id}@{c.updated_at.isoformat()}", pr=pr, kind=kind,
                 priority=4, created_at=now, payload=payload)


def _is_own(c: Comment, cfg: Config) -> bool:
    """Authored by the PR owner, unless it carries the test-only self-review prefix."""
    if c.author != cfg.author:
        return False
    pre = cfg.self_review_prefix
    return not (pre and (c.body or "").lstrip().startswith(pre))


def _pushback(thread: Thread, cfg: Config) -> int:
    seen_own, count = False, 0
    for c in thread.comments:
        if _is_own(c, cfg):
            seen_own = True
        elif seen_own:
            count += 1
    return count


def _all_events(s: PRSnapshot, cfg: Config, now: datetime, base: PRSnapshot | None) -> list[Event]:
    n, me = s.number, cfg.author
    if s.state == "MERGED":
        return [Event(f"merged:{n}", n, "merged", 4, now,
                      {"url": s.url, "merged_at": s.merged_at.isoformat() if s.merged_at else None,
                       "base_ref": s.base_ref, "head_ref": s.head_ref})]
    if s.state != "OPEN":
        return []
    out: list[Event] = []
    for t in s.threads:
        if t.is_resolved or not t.comments:
            continue
        own_times = [c.created_at for c in t.comments if _is_own(c, cfg)]
        pb = _pushback(t, cfg)
        for c in t.comments:
            if _is_own(c, cfg) or any(ot > c.updated_at for ot in own_times):
                continue
            out.append(_comment_event(c, n, "thread", now, {"thread_id": t.id, "pushback": pb}))
    for c in s.comments:
        if not _is_own(c, cfg):
            out.append(_comment_event(c, n, "comment", now, {}))
    latest: dict[str, Review] = {}
    for r in s.reviews:
        if r.author not in latest or r.submitted_at >= latest[r.author].submitted_at:
            latest[r.author] = r
    for r in latest.values():
        if r.author == me:
            continue
        noop = (is_noop(r.body) or r.state == "DISMISSED"
                or (not r.body.strip() and r.state in ("APPROVED", "COMMENTED")))
        cr = r.state == "CHANGES_REQUESTED"
        out.append(Event(f"review:{r.id}", n, "noop" if noop and not cr else "review",
                         0 if cr else 4, now,
                         {"review_id": r.id, "author": r.author, "state": r.state,
                          "body": r.body, "url": r.url}))
    if s.mergeable == "CONFLICTING":
        out.append(Event(f"conflict:{n}:{s.head_sha}", n, "conflict", 1, now,
                         {"url": s.url, "head_sha": s.head_sha, "base_ref": s.base_ref}))
    if base is not None and base.base_ref != s.base_ref:
        out.append(Event(f"base:{n}:{s.base_sha}", n, "base", 2, now,
                         {"url": s.url, "old_base_ref": base.base_ref, "base_ref": s.base_ref,
                          "old_base_sha": base.base_sha, "base_sha": s.base_sha}))
    for chk in s.failed_checks:
        out.append(Event(f"ci:{n}:{s.head_sha}:{chk}", n, "ci", 3, now,
                         {"url": s.url, "check": chk, "head_sha": s.head_sha}))
    return out


def detect(prev: PRSnapshot | None, cur: PRSnapshot, cfg: Config, now: datetime) -> list[Event]:
    """Events present for `cur` that were not already present for `prev`."""
    known = {e.id for e in _all_events(prev, cfg, now, None)} if prev else set()
    return [e for e in _all_events(cur, cfg, now, prev) if e.id not in known]


def stack_warnings(prev: dict[int, PRSnapshot], cur: dict[int, PRSnapshot],
                   merged: list[PRSnapshot]) -> list[str]:
    """G15: report (never fix) parent-merged children and out-of-order merges."""
    warnings: list[str] = []
    merged_nums = {m.number for m in merged}
    for m in merged:
        children = sorted((p for p in cur.values()
                           if p.number != m.number and p.state == "OPEN"
                           and p.base_ref == m.head_ref), key=lambda p: p.number)
        if children:
            desc = ", ".join(f"#{c.number} (base hiện tại {c.base_ref}, {c.mergeable})"
                             for c in children)
            warnings.append(f"PR #{m.number} ({m.head_ref}) đã merge; PR con bị ảnh hưởng: {desc}")
        pool = {k: v for k, v in {**prev, **cur}.items() if k not in merged_nums}
        for p in sorted(pool.values(), key=lambda p: p.number):
            if p.number != m.number and p.state == "OPEN" and p.head_ref == m.base_ref:
                warnings.append(f"PR #{m.number} đã merge trước PR cha #{p.number} ({p.head_ref}) "
                                f"vẫn đang mở: merge sai thứ tự hoặc vào sai nhánh đích")
    return warnings
