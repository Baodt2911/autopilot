"""Owner operations shared by the CLI and the web API (no I/O formatting here)."""
from __future__ import annotations

import re
from datetime import date

from .config import Config
from .render import atomic_write
from .store import Store

QA_MAX_BYTES = 1_000_000
_QA_NAME = re.compile(r"[a-z0-9_-]{1,64}")


def pause(store: Store, pr: int | None) -> None:
    if pr is None:
        store.kv_set("paused", "1")
    else:
        store.kv_set(f"paused_pr:{pr}", "owner")


def resume(store: Store, pr: int | None, today: date) -> int:
    """Resume the router or one PR; returns how many events went back to the queue."""
    if pr is None:
        store.kv_set("paused", None)
        store.kv_set("auth_alerted", None)
        return 0
    by_cap = store.kv_get(f"paused_pr:{pr}") == "limit"
    store.kv_set(f"paused_pr:{pr}", None)
    if not by_cap:
        return 0
    cancelled = {i for b in store.batches(status="cancelled", pr=pr) for i in b["event_ids"]}
    ids = [e.id for e in store.events(status="batched", pr=pr) if e.id in cancelled]
    store.mark_events(ids, "new")  # events of batches cancelled by the wake cap
    store.reset_wake(pr, today)
    return len(ids)


def save_qa(cfg: Config, name: str, text: str):
    """Store pasted QA text as <data_dir>/qa/<name>.md; returns the path."""
    base = name[:-3] if name.endswith(".md") else name
    if not _QA_NAME.fullmatch(base.lower()):
        raise ValueError("Tên QA chỉ gồm a-z, 0-9, '-', '_' (tối đa 64 ký tự).")
    if len(text.encode("utf-8")) > QA_MAX_BYTES:
        raise ValueError("Nội dung QA vượt quá 1 MB.")
    path = cfg.qa_dir / f"{base.lower()}.md"
    atomic_write(path, text)
    return path
