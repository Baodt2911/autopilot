"""SQLite persistent state: events, batches, owner items, counters, kv, locks."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from autopilot.models import Event

TRANSITIONS: dict[str, frozenset[str]] = {
    "new": frozenset({"delivered", "queued", "cancelled"}),
    "queued": frozenset({"delivered", "cancelled"}),
    "delivered": frozenset({"acked", "delivered", "escalated", "waiting_limit", "cancelled"}),
    "acked": frozenset({"in_progress", "delivered", "escalated", "waiting_limit", "cancelled"}),
    "in_progress": frozenset({"done", "waiting_owner", "waiting_limit", "delivered", "escalated", "cancelled"}),
    "waiting_owner": frozenset({"in_progress", "delivered", "cancelled"}),
    "waiting_limit": frozenset({"delivered", "cancelled"}),
    "escalated": frozenset({"delivered", "cancelled"}),
}
EVENT_STATUSES = frozenset({"new", "batched", "noop", "cancelled"})
ITEM_KINDS = frozenset({"Q", "P", "R"})
LEASE = timedelta(minutes=60)
ACK_TIMEOUT = timedelta(minutes=15)
_BATCH_TIME_COLS = ("delivered_at", "acked_at", "lease_until")
_SETTABLE = frozenset({"session", *_BATCH_TIME_COLS})

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  id TEXT PRIMARY KEY, pr INTEGER NOT NULL, kind TEXT NOT NULL, priority INTEGER NOT NULL,
  created_at TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new', seq INTEGER);
CREATE TABLE IF NOT EXISTS batches(
  id INTEGER PRIMARY KEY AUTOINCREMENT, pr INTEGER NOT NULL, status TEXT NOT NULL,
  event_ids TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
  delivered_at TEXT, acked_at TEXT, lease_until TEXT, session TEXT);
CREATE TABLE IF NOT EXISTS items(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
  pr INTEGER, batch_id INTEGER, title TEXT, body_md TEXT, status TEXT NOT NULL DEFAULT 'open',
  answer TEXT, note TEXT, reply_hint TEXT, created_at TEXT NOT NULL, resolved_at TEXT, reminded_at TEXT);
CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS locks(name TEXT PRIMARY KEY, holder TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots(pr INTEGER PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS timeline(
  id INTEGER PRIMARY KEY AUTOINCREMENT, pr INTEGER, kind TEXT NOT NULL, text TEXT NOT NULL,
  at TEXT NOT NULL, url TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_events_pr ON events(pr);
CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status);
"""


class InvalidTransition(Exception):
    pass


class ItemClosed(Exception):
    pass


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d is not None else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class Store:
    def __init__(self, path: Path, prefix: str = "ivt"):
        self.path = Path(path)
        self.prefix = prefix
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def _tx(self, fn):
        """Run fn(db) inside BEGIN IMMEDIATE so concurrent writers serialize on busy_timeout."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            r = fn(self._db)
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")
        return r

    # ---- events ----
    def add_event(self, e: Event) -> bool:
        def f(db):
            cur = db.execute(
                "INSERT OR IGNORE INTO events(id,pr,kind,priority,created_at,payload) VALUES(?,?,?,?,?,?)",
                (e.id, e.pr, e.kind, e.priority, e.created_at.isoformat(), json.dumps(e.payload)),
            )
            return cur.rowcount == 1

        return self._tx(f)

    def events(self, *, status: str | None = None, pr: int | None = None) -> list[Event]:
        q, a = "SELECT * FROM events WHERE 1=1", []
        if status is not None:
            q += " AND status=?"
            a.append(status)
        if pr is not None:
            q += " AND pr=?"
            a.append(pr)
        rows = self._db.execute(q + " ORDER BY rowid", a).fetchall()
        return [
            Event(r["id"], r["pr"], r["kind"], r["priority"], _dt(r["created_at"]), json.loads(r["payload"]))
            for r in rows
        ]

    def mark_events(self, ids: list[str], status: str) -> None:
        if status not in EVENT_STATUSES:
            raise ValueError(f"invalid event status: {status}")
        self._tx(lambda db: db.executemany("UPDATE events SET status=? WHERE id=?", [(status, i) for i in ids]))

    # ---- batches ----
    def _batch_row(self, r) -> dict:
        d = {k: r[k] for k in ("id", "pr", "status", "attempts", "session")}
        d["event_ids"] = json.loads(r["event_ids"])
        for c in _BATCH_TIME_COLS:
            d[c] = _dt(r[c])
        return d

    def create_batch(self, pr: int, event_ids: list[str], now: datetime) -> int:
        return self._tx(
            lambda db: db.execute(
                "INSERT INTO batches(pr,status,event_ids,created_at) VALUES(?,?,?,?)",
                (pr, "new", json.dumps(event_ids), now.isoformat()),
            ).lastrowid
        )

    def batch(self, id: int) -> dict:
        r = self._db.execute("SELECT * FROM batches WHERE id=?", (id,)).fetchone()
        if r is None:
            raise KeyError(id)
        return self._batch_row(r)

    def transition(self, batch_id: int, to: str, now: datetime, **fields) -> None:
        bad = set(fields) - _SETTABLE
        if bad:
            raise ValueError(f"cannot set fields: {sorted(bad)}")

        def f(db):
            r = db.execute("SELECT status, attempts FROM batches WHERE id=?", (batch_id,)).fetchone()
            if r is None:
                raise KeyError(batch_id)
            if to not in TRANSITIONS.get(r["status"], frozenset()):
                raise InvalidTransition(f"batch {batch_id}: {r['status']} -> {to}")
            sets = {"status": to}
            if to == "delivered":
                sets["attempts"] = r["attempts"] + 1
                sets["delivered_at"] = now.isoformat()
            if to == "acked":
                sets["acked_at"] = now.isoformat()
            if to in ("acked", "in_progress"):
                sets["lease_until"] = (now + LEASE).isoformat()
            for k, v in fields.items():
                sets[k] = _iso(v) if isinstance(v, datetime) else v
            db.execute(
                f"UPDATE batches SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
                [*sets.values(), batch_id],
            )

        self._tx(f)

    def batches(self, *, status: str | tuple[str, ...] | None = None, pr: int | None = None) -> list[dict]:
        q, a = "SELECT * FROM batches WHERE 1=1", []
        if status is not None:
            st = (status,) if isinstance(status, str) else tuple(status)
            q += f" AND status IN ({','.join('?' * len(st))})"
            a += st
        if pr is not None:
            q += " AND pr=?"
            a.append(pr)
        return [self._batch_row(r) for r in self._db.execute(q + " ORDER BY id", a).fetchall()]

    def unacked(self, now: datetime) -> list[dict]:
        return [b for b in self.batches(status="delivered") if b["delivered_at"] <= now - ACK_TIMEOUT]

    def lease_expired(self, now: datetime) -> list[dict]:
        return [
            b
            for b in self.batches(status=("acked", "in_progress"))
            if b["lease_until"] is not None and b["lease_until"] < now
        ]

    # ---- owner items ----
    def _item_row(self, r) -> dict:
        d = dict(r)
        for c in ("created_at", "resolved_at", "reminded_at"):
            d[c] = _dt(d[c])
        return d

    def add_item(self, kind: str, pr: int, batch_id: int, title: str, body_md: str, now: datetime) -> str:
        if kind not in ITEM_KINDS:
            raise ValueError(f"invalid item kind: {kind}")

        def f(db):
            cur = db.execute(
                "INSERT INTO items(code,kind,pr,batch_id,title,body_md,created_at) VALUES('',?,?,?,?,?,?)",
                (kind, pr, batch_id, title, body_md, now.isoformat()),
            )
            code = f"{kind}-{self.prefix}-{cur.lastrowid}"
            db.execute("UPDATE items SET code=? WHERE seq=?", (code, cur.lastrowid))
            return code

        return self._tx(f)

    def item(self, code: str) -> dict | None:
        r = self._db.execute("SELECT * FROM items WHERE code=?", (code,)).fetchone()
        return self._item_row(r) if r else None

    def open_items(self) -> list[dict]:
        return [self._item_row(r) for r in self._db.execute("SELECT * FROM items WHERE status='open' ORDER BY seq")]

    def resolve_item(self, code: str, answer: str, note: str, reply_hint: str, now: datetime) -> dict:
        def f(db):
            r = db.execute("SELECT status FROM items WHERE code=?", (code,)).fetchone()
            if r is None:
                raise KeyError(code)
            if r["status"] != "open":
                raise ItemClosed(code)
            db.execute(
                "UPDATE items SET status='closed', answer=?, note=?, reply_hint=?, resolved_at=? WHERE code=?",
                (answer, note, reply_hint, now.isoformat(), code),
            )

        self._tx(f)
        return self.item(code)

    def mark_reminded(self, code: str, now: datetime) -> None:
        self._tx(lambda db: db.execute("UPDATE items SET reminded_at=? WHERE code=?", (now.isoformat(), code)))

    # ---- counters ----
    def _bump(self, name: str) -> int:
        def f(db):
            db.execute(
                "INSERT INTO counters(name,value) VALUES(?,1) ON CONFLICT(name) DO UPDATE SET value=value+1",
                (name,),
            )
            return db.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()[0]

        return self._tx(f)

    def bump_round(self, thread_id: str) -> int:
        return self._bump(f"round:{thread_id}")

    def bump_wake(self, pr: int, day: date) -> int:
        return self._bump(f"wake:{pr}:{day.isoformat()}")

    # ---- kv ----
    def kv_get(self, key: str) -> str | None:
        r = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def kv_set(self, key: str, value: str | None) -> None:
        def f(db):
            if value is None:
                db.execute("DELETE FROM kv WHERE key=?", (key,))
            else:
                db.execute(
                    "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )

        self._tx(f)

    # ---- locks ----
    def acquire(self, lock: str, holder: str, now: datetime, ttl_min: int = 60) -> bool:
        def f(db):
            r = db.execute("SELECT holder, expires_at FROM locks WHERE name=?", (lock,)).fetchone()
            if r is not None and r["holder"] != holder and _dt(r["expires_at"]) > now:
                return False
            db.execute(
                "INSERT INTO locks(name,holder,expires_at) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET holder=excluded.holder, expires_at=excluded.expires_at",
                (lock, holder, (now + timedelta(minutes=ttl_min)).isoformat()),
            )
            return True

        return self._tx(f)

    def release(self, lock: str, holder: str) -> None:
        self._tx(lambda db: db.execute("DELETE FROM locks WHERE name=? AND holder=?", (lock, holder)))

    # ---- snapshots ----
    def save_snapshot(self, pr: int, data: dict) -> None:
        self._tx(
            lambda db: db.execute(
                "INSERT INTO snapshots(pr,data) VALUES(?,?) ON CONFLICT(pr) DO UPDATE SET data=excluded.data",
                (pr, json.dumps(data)),
            )
        )

    def snapshot(self, pr: int) -> dict | None:
        r = self._db.execute("SELECT data FROM snapshots WHERE pr=?", (pr,)).fetchone()
        return json.loads(r[0]) if r else None

    def snapshots(self) -> dict[int, dict]:
        return {r["pr"]: json.loads(r["data"]) for r in self._db.execute("SELECT * FROM snapshots")}

    # ---- timeline ----
    def log(self, pr: int | None, kind: str, text: str, now: datetime, url: str = "") -> None:
        self._tx(
            lambda db: db.execute(
                "INSERT INTO timeline(pr,kind,text,at,url) VALUES(?,?,?,?,?)", (pr, kind, text, now.isoformat(), url)
            )
        )

    def timeline(self, pr: int | None = None, limit: int = 200) -> list[dict]:
        q, a = "SELECT * FROM timeline", []
        if pr is not None:
            q += " WHERE pr=?"
            a.append(pr)
        rows = self._db.execute(q + " ORDER BY id DESC LIMIT ?", [*a, limit]).fetchall()
        return [{**dict(r), "at": _dt(r["at"])} for r in rows]
