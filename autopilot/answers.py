"""Owner answers: shared by CLI and web."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .config import Config
from .models import Event
from .render import atomic_write
from .store import Store

BACKLOG_KINDS = ("spec-gap", "scope")


def _append(path: Path, text: str, header: str = "") -> None:
    old = path.read_text(encoding="utf-8") if path.exists() else header
    if old and not old.endswith("\n"):
        old += "\n"
    atomic_write(path, old + text)


def _decide(store: Store, cfg: Config, code: str, choice: str, note: str, reply_hint: str,
            source: str, now: datetime) -> int:
    item = store.resolve_item(code, choice, note, reply_hint, now)  # KeyError / ItemClosed
    pr = item["pr"]
    q = "\n".join(x for x in (item.get("title") or "", item.get("body_md") or "") if x)
    block = (f"\n## {code} · {now.date().isoformat()}\n\n"
             f"- Câu hỏi: {q}\n- Trả lời: {choice}\n- Ghi chú: {note}\n- Nguồn: {source}\n")
    _append(Path(cfg.data_dir) / "decisions.md", block, "# Decisions\n")
    ev = Event(id=f"answer:{code}", pr=pr, kind="answer", priority=0, created_at=now,
               payload={"code": code, "kind": item["kind"], "answer": choice, "choice": choice,
                        "note": note, "reply_hint": reply_hint,
                        "decision_template": (cfg.reply or {}).get("decision_template", ""),
                        "source": source, "origin_batch": item["batch_id"]})
    store.add_event(ev)
    bid = store.create_batch(pr, [ev.id], now)
    store.mark_events([ev.id], "batched")
    store.log(pr, "answer", f"Owner trả lời {code}: {choice}", now)
    return bid


def answer(store: Store, cfg: Config, code: str, choice: str, note: str = "",
           reply_hint: str = "", source: str = "", now: datetime | None = None) -> int:
    return _decide(store, cfg, code, choice, note, reply_hint, source, now or datetime.now().astimezone())


def approve(store: Store, cfg: Config, code: str, ok: bool, note: str = "",
            now: datetime | None = None) -> int:
    if code.split("-", 1)[0] not in ("P", "R"):
        raise ValueError(f"approve chỉ dùng cho mã P hoặc R: {code}")
    return _decide(store, cfg, code, "approved" if ok else "rejected", note, "", "",
                   now or datetime.now().astimezone())


def _sections(text: str) -> list[str]:
    parts = text.split("\n## ")
    return ["## " + p.strip() + "\n" for p in parts[1:]] if text.startswith("# ") or len(parts) > 1 else []


def push_decisions(store: Store, cfg: Config, gh, issue: int, now: datetime) -> str:
    path = Path(cfg.data_dir) / "decisions.md"
    secs = _sections(path.read_text(encoding="utf-8")) if path.exists() else []
    done = int(store.kv_get("last_pushed_decision") or 0)
    new = secs[done:]
    if not new:
        raise ValueError("không có quyết định mới để đẩy")
    url = gh.post_issue_comment(issue, "Product decisions\n\n" + "\n".join(new))
    store.kv_set("last_pushed_decision", str(len(secs)))
    store.log(None, "push_decisions", f"Đã đẩy {len(new)} quyết định lên issue #{issue}", now, url)
    return url


def add_backlog(cfg: Config, kind: str, pr: int, text: str, now: datetime) -> None:
    if kind not in BACKLOG_KINDS:
        raise ValueError(f"kind phải là một trong {BACKLOG_KINDS}")
    line = f"- [{kind}] PR #{pr} · {now.date().isoformat()} · {' '.join(text.split())}\n"
    _append(Path(cfg.data_dir) / "backlog.md", line, "# Backlog\n")
