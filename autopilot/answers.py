"""Owner answers: shared by CLI and web."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from .config import Config
from .models import Event
from .render import atomic_write, decision_section, file_lock, write_decisions
from .store import Store

BACKLOG_KINDS = ("spec-gap", "scope")


def _decide(store: Store, cfg: Config, code: str, choice: str, note: str, reply_hint: str,
            source: str, now: datetime) -> int:
    item = store.item(code)
    if item is None:
        raise KeyError(code)
    ev = Event(id=f"answer:{code}", pr=item["pr"], kind="answer", priority=0, created_at=now,
               payload={"code": code, "kind": item["kind"], "answer": choice, "choice": choice,
                        "note": note, "reply_hint": reply_hint,
                        "decision_template": (cfg.reply or {}).get("decision_template", ""),
                        "source": source, "origin_batch": item["batch_id"]})
    bid = store.resolve_and_enqueue(code, choice, note, reply_hint, now, ev)  # one transaction
    write_decisions(cfg, store)
    return bid


def answer(store: Store, cfg: Config, code: str, choice: str, note: str = "",
           reply_hint: str = "", source: str = "", now: datetime | None = None) -> int:
    if code.split("-", 1)[0] != "Q":
        raise ValueError(f"answer chỉ dùng cho mã Q: {code}")
    return _decide(store, cfg, code, choice, note, reply_hint, source, now or datetime.now().astimezone())


def approve(store: Store, cfg: Config, code: str, ok: bool, note: str = "",
            now: datetime | None = None) -> int:
    if code.split("-", 1)[0] not in ("P", "R"):
        raise ValueError(f"approve chỉ dùng cho mã P hoặc R: {code}")
    return _decide(store, cfg, code, "approved" if ok else "rejected", note, "", "",
                   now or datetime.now().astimezone())


def push_decisions(store: Store, cfg: Config, gh, issue: int, now: datetime) -> str:
    holder = uuid.uuid4().hex
    if not store.acquire("push_decisions", holder, now, ttl_min=5):
        raise RuntimeError("push_decisions đang chạy ở nơi khác")
    try:
        pushed = set(json.loads(store.kv_get("pushed_decisions") or "[]"))
        src = {e.id: e.payload.get("source", "") for e in store.events() if e.kind == "answer"}
        new = [i for i in store.closed_items() if i["code"] not in pushed]
        if not new:
            raise ValueError("không có quyết định mới để đẩy")
        text = "\n".join(decision_section({**i, "source": src.get(f"answer:{i['code']}", "")}) for i in new)
        url = gh.post_issue_comment(issue, "Product decisions\n\n" + text)
        store.kv_set("pushed_decisions", json.dumps(sorted(pushed | {i["code"] for i in new})))
        store.log(None, "push_decisions", f"Đã đẩy {len(new)} quyết định lên issue #{issue}", now, url)
        return url
    finally:
        store.release("push_decisions", holder)


def add_backlog(cfg: Config, kind: str, pr: int, text: str, now: datetime) -> None:
    if kind not in BACKLOG_KINDS:
        raise ValueError(f"kind phải là một trong {BACKLOG_KINDS}")
    line = f"- [{kind}] PR #{pr} · {now.date().isoformat()} · {' '.join(text.split())}\n"
    path = Path(cfg.data_dir) / "backlog.md"
    with file_lock(cfg, "backlog"):
        old = path.read_text(encoding="utf-8") if path.exists() else "# Backlog\n"
        if not old.endswith("\n"):
            old += "\n"
        atomic_write(path, old + line)
