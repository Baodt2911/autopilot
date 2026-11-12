"""Worker health: usage-limit parsing, dead/stale detection, global limit pause."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .models import Worktree
from .store import InvalidTransition, Store
from .transport import TransportError

LIMIT_RE = re.compile(
    r"hit your .*?limit.*?resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)\s*\(([^)]+)\)",
    re.IGNORECASE,
)
RESET_GRACE = timedelta(minutes=5)
STALE_ALERT = timedelta(minutes=60)
STALE_STOP = timedelta(minutes=90)
MAX_RERUNS = 2
_RESULT_STATUSES = frozenset({"done", "waiting_owner"})


def parse_limit(text: str, now: datetime) -> datetime | None:
    """Reset time (+5 min) from a usage-limit message, or None if the text has none."""
    matches = LIMIT_RE.findall(text or "")
    if not matches:
        return None
    hour, minute, ampm, tzname = matches[-1]
    try:
        tz = ZoneInfo(tzname.strip())
    except Exception:
        tz = now.tzinfo
    h = int(hour) % 12 + (12 if ampm.lower() == "pm" else 0)
    local_now = now.astimezone(tz) if tz is not None else now
    reset = local_now.replace(hour=h, minute=int(minute or 0), second=0, microsecond=0)
    if reset <= local_now:
        reset += timedelta(days=1)
    return reset + RESET_GRACE


def limit_active(store: Store, now: datetime) -> datetime | None:
    raw = store.kv_get("limit_until")
    if not raw:
        return None
    until = datetime.fromisoformat(raw)
    return until if until > now else None


def _run_record(store: Store, wt: Worktree) -> dict | None:
    raw = store.kv_get(f"run:{wt.path}")
    return json.loads(raw) if raw else None


def _try(store: Store, batch: int, to: str, now: datetime) -> bool:
    try:
        store.transition(batch, to, now)
        return True
    except (InvalidTransition, KeyError):
        return False


def check_workers(store: Store, transport, worktrees_with_batches: dict[str, tuple[Worktree, str]],
                  now: datetime) -> list[str]:
    """Inspect each worker; returns actions taken (lines starting 'alert:' need the owner).

    Each value is (worktree, session id); the batch comes from the worktree's last run record.
    """
    actions: list[str] = []
    for branch, (wt, session) in worktrees_with_batches.items():
        st = transport.status(wt, now)
        run = _run_record(store, wt)
        batch = run["batch"] if run else None

        if run:
            for tool in transport.forbidden_ops(wt):
                if store.kv_get(f"forbidden-alert:{wt.path}:{tool}") != run["started_at"]:
                    store.kv_set(f"forbidden-alert:{wt.path}:{tool}", run["started_at"])
                    actions.append(f"alert: worker tried forbidden op: {tool} ({branch})")

        if st.state == "limited" and run:
            key = f"limit-run:{wt.path}"
            armed = store.kv_get(key)
            if not armed or json.loads(armed)["run"] != run["started_at"]:  # arm once per run
                until = parse_limit(transport.read_text(wt, session), now) or now + timedelta(hours=5)
                store.kv_set(key, json.dumps({"run": run["started_at"], "until": until.isoformat()}))
                store.kv_set("limit_until", until.isoformat())
                moved = []
                for b in store.batches(status=("in_progress", "delivered")):
                    if _try(store, b["id"], "waiting_limit", now):
                        moved.append(b["id"])
                actions.append(f"usage limit hit ({branch}): paused batches {moved} until {until.isoformat()}")
        elif st.state == "dead" and batch is not None:
            key = f"reruns:{batch}"
            reruns = int(store.kv_get(key) or 0)
            if reruns >= MAX_RERUNS:
                _try(store, batch, "escalated", now)
                actions.append(f"alert: worker dead after {MAX_RERUNS} re-runs, batch {batch} escalated ({branch})")
            else:
                try:
                    transport.deliver(wt, session, run["prompt"], batch)
                except TransportError as e:
                    actions.append(f"alert: re-run of batch {batch} failed ({branch}): {e}")
                    continue
                except Exception as e:  # WorkerBusy etc.: try again next pass
                    actions.append(f"re-run of batch {batch} skipped ({branch}): {e}")
                    continue
                store.kv_set(key, str(reruns + 1))
                _try(store, batch, "delivered", now)
                actions.append(f"re-ran dead worker, batch {batch}, attempt {reruns + 2} ({branch})")
        elif st.state == "working" and st.updated_at is not None and run:
            idle = now - st.updated_at
            if idle >= STALE_STOP:
                if store.kv_get(f"stale-stop:{wt.path}") == run["started_at"]:
                    continue
                store.kv_set(f"stale-stop:{wt.path}", run["started_at"])
                transport.stop_worker(wt, session)
                if batch is not None:
                    _try(store, batch, "escalated", now)
                actions.append(f"alert: worker stalled {int(idle.total_seconds() // 60)} min, stopped, "
                               f"batch {batch} escalated ({branch})")
            elif idle >= STALE_ALERT and store.kv_get(f"stale-alert:{wt.path}") != run["started_at"]:
                store.kv_set(f"stale-alert:{wt.path}", run["started_at"])
                actions.append(f"alert: worker idle {int(idle.total_seconds() // 60)} min ({branch})")
    return actions
