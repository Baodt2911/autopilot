"""Pure routing: ready batches -> Deliver / Start / Queue / Incident. No I/O."""
from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .models import PRSnapshot, WorkerStatus, Worktree


@dataclass(frozen=True)
class Deliver:
    batch_id: int
    worktree: Worktree
    session: str


@dataclass(frozen=True)
class Start:
    batch_id: int
    worktree: Worktree


@dataclass(frozen=True)
class Queue:
    batch_id: int
    reason: str


@dataclass(frozen=True)
class Incident:
    batch_id: int
    text: str


Action = Deliver | Start | Queue | Incident


def route(batches: list[dict], prs: dict[int, PRSnapshot], worktrees: dict[str, Worktree],
          workers: dict[str, WorkerStatus], cfg: Config, *, paused: bool,
          paused_prs: set[int]) -> list[Action]:
    """Decide an action for every batch.

    Each batch dict is a `Store.batch` dict plus two keys supplied by the caller:
    `priority: int` (min priority over the batch's events, 0 = highest) and
    `created_at: datetime`. Batches are handled ordered by (priority, created_at, id);
    the result list follows that order.

    Worktree lookup: `prs[batch["pr"]].head_ref == Worktree.branch`; workers are keyed
    by worktree branch. Slots: workers in state "working" plus every Deliver/Start
    emitted in this call (headless: each delivery is a new process); at max_workers
    the rest get Queue("slots"). A worktree receives at most one action per call.
    """
    ordered = sorted(batches, key=lambda b: (b["priority"], b["created_at"], b["id"]))
    used = sum(1 for w in workers.values() if w.state == "working")
    taken: set[str] = set()
    out: list[Action] = []
    for b in ordered:
        bid, pr_n = b["id"], b["pr"]
        if paused:
            out.append(Queue(bid, "paused"))
            continue
        if pr_n in paused_prs:
            out.append(Queue(bid, "paused_pr"))
            continue
        pr = prs.get(pr_n)
        if pr is None:
            out.append(Incident(bid, f"PR #{pr_n} has no snapshot; cannot route batch {bid}"))
            continue
        wt = worktrees.get(pr.head_ref)
        if wt is None:
            out.append(Incident(bid, f"No local worktree for branch {pr.head_ref} (PR #{pr_n})"))
            continue
        w = workers.get(wt.branch)
        state = w.state if w else "none"
        if wt.branch in taken or state == "working":
            out.append(Queue(bid, "busy"))
        elif state == "owner_busy":
            out.append(Queue(bid, "owner_session"))
        elif state == "limited":
            out.append(Queue(bid, "limit"))
        elif state == "dead":  # health (Task 8) owns re-running dead turns
            out.append(Queue(bid, "dead"))
        elif used >= cfg.max_workers:
            out.append(Queue(bid, "slots"))
        elif state in ("idle", "waiting") and w and w.session:
            used += 1
            taken.add(wt.branch)
            out.append(Deliver(bid, wt, w.session))
        else:  # none, or no stored session to resume
            used += 1
            taken.add(wt.branch)
            out.append(Start(bid, wt))
    return out
