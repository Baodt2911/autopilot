"""Debounce batching of new events per PR, plus per-thread / per-PR limits (G9, G2)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from .config import Config
from .models import Event
from .store import Store

# Batch statuses after which a PR may get a new batch.
_TERMINAL = frozenset({"done", "escalated", "cancelled"})
_NON_TERMINAL = ("new", "queued", "delivered", "acked", "in_progress", "waiting_owner", "waiting_limit")


def _in_flight(store: Store, pr: int) -> bool:
    return any(b["status"] not in _TERMINAL for b in store.batches(pr=pr))


def ready_batches(store: Store, cfg: Config, now: datetime, *, skip_prs: frozenset[int] | set[int] = frozenset(),
                  only_prs: set[int] | None = None, force: bool = False) -> list[int]:
    """Group `new` events per PR into batches once debounced (or a review was submitted).

    skip_prs: PRs left untouched (paused). only_prs: restrict to these PRs. force: ignore the debounce.
    """
    debounce = timedelta(minutes=cfg.debounce_min)
    by_pr: dict[int, list[Event]] = {}
    noops: list[str] = []
    for e in store.events(status="new"):
        if e.kind == "noop":
            noops.append(e.id)
        else:
            by_pr.setdefault(e.pr, []).append(e)
    if noops:
        store.mark_events(noops, "noop")
    created: list[int] = []
    for pr, evs in sorted(by_pr.items()):
        if pr in skip_prs or (only_prs is not None and pr not in only_prs):
            continue
        if _in_flight(store, pr):
            continue
        has_review = any(e.kind == "review" for e in evs)
        if not force and not has_review and max(e.created_at for e in evs) > now - debounce:
            continue
        ordered = sorted(evs, key=lambda e: e.priority)  # stable: keeps arrival order within a priority
        ids = [e.id for e in ordered]
        bid = store.create_batch(pr, ids, now)
        store.mark_events(ids, "batched")
        created.append(bid)
    return created


def _meta_key(batch_id: int) -> str:
    return f"batch_meta:{batch_id}"


def batch_meta(store: Store, batch_id: int) -> dict:
    raw = store.kv_get(_meta_key(batch_id))
    base = {"force_c2_threads": [], "pushback": {}, "force_c2": []}
    return {**base, **json.loads(raw)} if raw else base


def apply_limits(store: Store, cfg: Config, batch_id: int, now: datetime) -> str | None:
    """Return "paused" when the PR hit its daily wake cap, else None (meta stored for the batch)."""
    b = store.batch(batch_id)
    pr = b["pr"]
    if store.bump_wake(pr, now.date()) > cfg.limits["wakes_per_pr_day"]:
        store.kv_set(f"paused_pr:{pr}", "limit")
        store.transition(batch_id, "cancelled", now)
        store.log(pr, "limit", f"PR #{pr} vượt {cfg.limits['wakes_per_pr_day']} lần đánh thức trong ngày: tạm dừng", now)
        return "paused"
    by_id = {e.id: e for e in store.events(pr=pr)}
    cap = cfg.limits["thread_rounds"]
    force_threads: list[str] = []
    force_events: list[str] = []
    pushback: dict[str, int] = {}
    bumped: dict[str, int] = {}  # one round per distinct thread per batch
    for eid in b["event_ids"]:
        e = by_id.get(eid)
        if e is None or e.kind != "thread" or not e.payload.get("thread_id"):
            continue
        tid = e.payload["thread_id"]
        if tid not in bumped:
            bumped[tid] = store.bump_round(tid)
        pb = int(e.payload.get("pushback") or 0)
        pushback[tid] = max(pushback.get(tid, 0), pb)
        over_cap = bumped[tid] > cap
        if over_cap or pb >= 2:
            if tid not in force_threads:
                force_threads.append(tid)
            force_events.append(eid)
    store.kv_set(_meta_key(batch_id), json.dumps(
        {"force_c2_threads": force_threads, "pushback": pushback, "force_c2": force_events}))
    return None
