"""Desktop notifications with quiet hours (machine local time), reminders, morning digest."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, time
from typing import Callable

KINDS = ("question", "plan", "draft", "incident", "stack")
MAX_QUEUE = 200
TITLE_PREFIX = "Autopilot: cần chú ý"


def notify_send(title: str, body: str) -> None:
    subprocess.run(["notify-send", "-a", "autopilot", title, body], check=True, timeout=10)


def _local(now: datetime) -> datetime:
    return now.astimezone()


def _hm(s: str) -> time:
    h, m = s.strip().split(":")
    return time(int(h), int(m))


class Notifier:
    def __init__(self, cfg, store, send: Callable[[str, str], None] = notify_send):
        self.cfg, self.store, self._send = cfg, store, send

    # ---- helpers ----
    def _in_window(self, now: datetime) -> bool:
        a, b = (_hm(p) for p in self.cfg.notify["hours"].split("-"))
        t = _local(now).time().replace(second=0, microsecond=0)
        return a <= t < b if a <= b else (t >= a or t < b)

    def _deliver(self, title: str, body: str, now: datetime) -> bool:
        try:
            self._send(title, body)
            return True
        except Exception as e:  # missing binary, no display, timeout...
            try:
                self.store.log(None, "notify_failed", f"{type(e).__name__}: {e}", now)
            except Exception:
                pass
            return False

    @staticmethod
    def _parse(raw: str | None) -> list[dict]:
        try:
            q = json.loads(raw or "[]")
            return q if isinstance(q, list) else []
        except ValueError:
            return []

    @staticmethod
    def _push(q: list[dict], new: list[dict]) -> list[dict]:
        """Append, keep at most MAX_QUEUE real entries; dropped ones are counted in a leading marker."""
        dropped = sum(i.get("n", 0) for i in q if i.get("kind") == "_dropped")
        items = [i for i in q if i.get("kind") != "_dropped"] + new
        if len(items) > MAX_QUEUE:
            dropped += len(items) - MAX_QUEUE
            items = items[-MAX_QUEUE:]
        return ([{"kind": "_dropped", "n": dropped}] if dropped else []) + items

    def _enqueue(self, kind: str, title: str, body: str, now: datetime) -> None:
        entry = {"kind": kind, "title": title, "body": body, "at": now.isoformat()}
        self.store.kv_update(
            "digest_queue", lambda cur: json.dumps(self._push(self._parse(cur), [entry]), ensure_ascii=False)
        )

    def _restore(self, taken: list[dict]) -> None:
        self.store.kv_update(
            "digest_queue",
            lambda cur: json.dumps(self._push(taken, [i for i in self._parse(cur)]), ensure_ascii=False),
        )

    # ---- API ----
    def alert(self, kind: str, title: str, body: str, now: datetime) -> bool:
        if self._in_window(now) and self._deliver(f"{TITLE_PREFIX}: {title}", body, now):
            return True
        self._enqueue(kind, title, body, now)
        return False

    def reminders(self, now: datetime) -> int:
        if not self._in_window(now):
            return 0
        limit = float(self.cfg.notify["remind_after_h"]) * 3600
        n = 0
        for it in self.store.open_items():
            if it["reminded_at"] is not None or (now - it["created_at"]).total_seconds() < limit:
                continue
            if self._deliver(f"Nhắc: {it['code']} chưa trả lời", it.get("title") or "", now):
                self.store.mark_reminded(it["code"], now)
                n += 1
        return n

    def digest(self, now: datetime, summary: str) -> bool:
        loc = _local(now)
        if not self._in_window(now) or loc.time().replace(second=0, microsecond=0) < _hm(self.cfg.notify["digest_at"]):
            return False
        today = loc.date().isoformat()
        claim: dict = {}

        def claim_fn(cur):
            if cur == today:
                return cur
            claim["prev"] = cur
            return today

        self.store.kv_update("last_digest", claim_fn)
        if "prev" not in claim:
            return False  # already sent (or being sent) today
        taken: list[dict] = []

        def take(cur):
            taken.extend(self._parse(cur))
            return "[]"

        self.store.kv_update("digest_queue", take)
        dropped = sum(i.get("n", 0) for i in taken if i.get("kind") == "_dropped")
        lines = [summary] + [f"- [{i['kind']}] {i['title']}: {i['body']}" for i in taken if i.get("kind") != "_dropped"]
        if dropped:
            lines.append(f"… và {dropped} thông báo khác")
        if self._deliver("Autopilot: tóm tắt buổi sáng", "\n".join(lines), now):
            return True
        self.store.kv_update("last_digest", lambda cur: claim["prev"] if cur == today else cur)
        self._restore(taken)
        return False
