"""Headless transport: one detached `claude -p` process per batch delivery."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import subprocess
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config
from ..health import parse_limit
from ..models import WorkerStatus, Worktree
from ..store import Store
from . import TransportError, WorkerBusy  # noqa: F401  (re-exported)

_RESULT_STATUSES = frozenset({"done", "waiting_owner"})
_ACTIVE_STATUSES = frozenset({"delivered", "acked", "in_progress"})
TRANSCRIPT_TAIL_LINES = 50


def spawn_detached(argv: list[str], cwd: str, out: Path, err: Path) -> int:
    """Start argv detached (own session) in cwd; stdout/stderr to files. Returns pid (== pgid)."""
    with open(out, "wb") as fo, open(err, "wb") as fe:
        p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe,
                             start_new_session=True)
    return p.pid


def encode_cwd(path: str) -> str:
    return os.path.realpath(path).replace("/", "-").replace(".", "-")


def _matches(denial: dict, patterns: list[str]) -> str | None:
    tool = denial.get("tool_name", "")
    cmd = (denial.get("tool_input") or {}).get("command", "") if isinstance(denial.get("tool_input"), dict) else ""
    for pat in patterns:
        name, _, inner = pat.partition("(")
        if name != tool:
            continue
        if not inner:
            return pat
        prefix = inner.rstrip(")").removesuffix(":*").removesuffix("*")
        if tool != "Bash" or cmd == prefix or cmd.startswith(prefix + " "):
            return pat
    return None


def parse_turn(raw: str, disallowed: list[str]) -> tuple[bool, list[str], str]:
    """(completed, forbidden_ops, result_text) for a finished turn's stdout JSON.

    completed=False for empty/unparseable output, is_error, non-success, empty result, or
    permission denials outside `disallowed`. Denials matching `disallowed` are returned in
    forbidden_ops and do not make the turn incomplete.
    """
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return False, [], ""
    if not isinstance(d, dict):
        return False, [], ""
    forbidden: list[str] = []
    stray = False
    for den in d.get("permission_denials") or []:
        m = _matches(den, disallowed) if isinstance(den, dict) else None
        if m is None:
            stray = True
        elif m not in forbidden:
            forbidden.append(m)
    text = d.get("result") or ""
    ok = (d.get("type") == "result" and d.get("subtype") == "success" and d.get("is_error") is False
          and d.get("api_error_status") is None and bool(text) and not stray)
    return ok, forbidden, text if isinstance(text, str) else ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class HeadlessTransport:
    def __init__(self, cfg: Config, store: Store, *, spawn: Callable[..., int] = spawn_detached,
                 proc_root: Path = Path("/proc"),
                 projects_root: Path = Path("~/.claude/projects").expanduser(),
                 killpg: Callable[[int, int], None] = os.killpg):
        self.cfg, self.store, self.spawn = cfg, store, spawn
        self.proc_root, self.projects_root, self.killpg = Path(proc_root), Path(projects_root), killpg
        self.known_paths: set[str] = set()

    def set_worktrees(self, paths) -> None:
        """Router calls this each scan with every known worktree path (for owner_busy ownership)."""
        self.known_paths = {os.path.realpath(p) for p in paths}

    # ---- paths / records ----
    def lock_path(self, wt: Worktree) -> Path:
        return self.cfg.data_dir / "locks" / f"{hashlib.sha1(wt.path.encode()).hexdigest()}.lock"

    def transcript_path(self, wt: Worktree, session: str) -> Path:
        return self.projects_root / encode_cwd(wt.path) / f"{session}.jsonl"

    def _run(self, wt: Worktree) -> dict | None:
        raw = self.store.kv_get(f"run:{wt.path}")
        return json.loads(raw) if raw else None

    def _session(self, wt: Worktree) -> str | None:
        return self.store.kv_get(f"session:{wt.path}")

    def _stat_fields(self, pid) -> list[str] | None:
        try:
            stat = (self.proc_root / str(pid) / "stat").read_text()
            return stat[stat.rindex(")") + 2:].split()
        except (OSError, ValueError):
            return None

    def _starttime(self, pid) -> str | None:
        f = self._stat_fields(pid)
        return f[19] if f and len(f) > 19 else None

    def _alive(self, run: dict) -> bool:
        """Same pid, same start time (no pid reuse), not a zombie."""
        pid = run["pid"]
        if not (self.proc_root / str(pid)).is_dir():
            return False
        f = self._stat_fields(pid)
        if f and f[0] == "Z":
            return False
        return self._starttime(pid) == run.get("starttime")

    def _pgid_of(self, pid: str) -> int | None:
        try:
            stat = (self.proc_root / pid / "stat").read_text()
            return int(stat[stat.rindex(")") + 2:].split()[2])
        except (OSError, ValueError, IndexError):
            return None

    # ---- spawning ----
    def _launch(self, wt: Worktree, prompt: str, batch_id: int, session: str, new: bool) -> None:
        # The flock only guards this check-and-spawn critical section; it is never inherited
        # by the child. "Busy" afterwards means the recorded run process is alive.
        lock = self.lock_path(wt)
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WorkerBusy(wt.path) from None
            prev = self._run(wt)
            if prev and self._alive(prev):
                raise WorkerBusy(wt.path)
            w = self.cfg.worker
            argv = [w["claude_bin"], "-p", prompt,
                    "--session-id" if new else "--resume", session,
                    "--model", self.cfg.worker_model, "--output-format", "json",
                    "--permission-mode", w["permission_mode"],
                    "--allowedTools", *w["allowed_tools"],
                    "--disallowedTools", *w["disallowed_tools"]]
            n = int(self.store.kv_get(f"attempt:{batch_id}") or 0) + 1
            runs = self.cfg.data_dir / "runs"
            runs.mkdir(parents=True, exist_ok=True)
            out, err = runs / f"{batch_id}-{n}.json", runs / f"{batch_id}-{n}.err"
            try:
                pid = self.spawn(argv, wt.path, out, err)
            except OSError as e:
                raise TransportError(f"spawn failed in {wt.path}: {e}") from e
            self.store.kv_set(f"attempt:{batch_id}", str(n))
            self.store.kv_set(f"session:{wt.path}", session)
            self.store.kv_set(f"run:{wt.path}", json.dumps({
                "pid": pid, "pgid": pid, "starttime": self._starttime(pid), "batch": batch_id,
                "session": session, "started_at": _now().isoformat(),
                "out": str(out), "err": str(err), "prompt": prompt}))
        finally:
            os.close(fd)

    def start_worker(self, wt: Worktree, prompt: str, batch_id: int) -> str:
        session = str(uuid.uuid4())
        self._launch(wt, prompt, batch_id, session, new=True)
        self.store.kv_set(f"session-closed:{wt.path}", None)
        return session

    def deliver(self, wt: Worktree, session: str, prompt: str, batch_id: int) -> None:
        if self.store.kv_get(f"session-closed:{wt.path}"):
            raise ValueError(f"session closed for {wt.path}")
        self._launch(wt, prompt, batch_id, session, new=False)

    def stop_worker(self, wt: Worktree, session: str, *, close: bool = False) -> None:
        run = self._run(wt)
        # Only the process group this router created; never our own group.
        if run and run.get("session") == session and run["pgid"] > 1 and run["pgid"] != os.getpgrp() \
                and self._alive(run):  # re-verified right before signalling
            try:
                self.killpg(run["pgid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
        if close:
            self.store.kv_set(f"session-closed:{wt.path}", "1")

    # ---- reading ----
    def _read(self, p: str | None) -> str:
        try:
            return Path(p).read_text(errors="replace") if p else ""
        except OSError:
            return ""

    def _tail(self, wt: Worktree, session: str, since: datetime | None) -> str:
        try:
            lines = self.transcript_path(wt, session).read_text(errors="replace").splitlines()
        except OSError:
            return ""
        if since is not None:  # only lines from the current run; undated lines are ignored
            lines = [ln for ln in lines if (t := self._line_time(ln)) is not None and t >= since]
        return "\n".join(lines[-TRANSCRIPT_TAIL_LINES:])

    @staticmethod
    def _line_time(line: str) -> datetime | None:
        try:
            t = datetime.fromisoformat(json.loads(line).get("timestamp").replace("Z", "+00:00"))
            return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError, TypeError):
            return None

    def read_text(self, wt: Worktree, session: str) -> str:
        run = self._run(wt)
        since = datetime.fromisoformat(run["started_at"]) if run else None
        parts = [self._read(run and run.get("out")), self._read(run and run.get("err")),
                 self._tail(wt, session, since)]
        return "\n".join(p for p in parts if p)

    def _turn(self, wt: Worktree) -> tuple[bool, list[str], str]:
        run = self._run(wt)
        return parse_turn(self._read(run and run.get("out")), self.cfg.worker["disallowed_tools"])

    def forbidden_ops(self, wt: Worktree) -> list[str]:
        """Disallowed tools the last finished turn tried to use (health alerts on them)."""
        run = self._run(wt)
        if not run or self._alive(run):
            return []
        return self._turn(wt)[1]

    def _owner_busy(self, wt: Worktree, run: dict | None) -> bool:
        """Read-only /proc scan for a foreign `claude` process owned by this worktree.

        A process belongs to the deepest known worktree whose path is a prefix of its cwd.
        """
        me = os.path.realpath(wt.path).rstrip("/")
        roots = {*self.known_paths, me}
        own_pids = {os.getpid()}
        own_pgids = {os.getpgrp()}
        if run:
            own_pids.add(run["pid"])
            own_pgids.add(run["pgid"])
        names = {"claude", os.path.basename(self.cfg.worker["claude_bin"])}
        try:
            entries = [e for e in self.proc_root.iterdir() if e.name.isdigit()]
        except OSError:
            return False
        for e in entries:
            try:
                argv = (e / "cmdline").read_bytes().decode(errors="replace").split("\0")
                if os.path.basename(argv[0]) not in names or ("--input-format" in argv and "stream-json" in argv):
                    continue
                cwd = os.path.realpath(os.readlink(e / "cwd"))
            except OSError:
                continue
            if int(e.name) in own_pids or self._pgid_of(e.name) in own_pgids:
                continue
            owners = [r for r in roots if cwd == r or cwd.startswith(r.rstrip("/") + "/")]
            if owners and max(owners, key=len) == me:
                return True
        return False

    def status(self, wt: Worktree, now: datetime | None = None) -> WorkerStatus:
        now = now or _now()
        session = self._session(wt)
        if not session or self.store.kv_get(f"session-closed:{wt.path}"):
            # a foreign `claude` owns the worktree even if the router never ran a worker there
            if not self.store.kv_get(f"session-closed:{wt.path}") and self._owner_busy(wt, self._run(wt)):
                return WorkerStatus("owner_busy", None, None)
            return WorkerStatus("none", None, None)
        run = self._run(wt)
        try:
            mtime = datetime.fromtimestamp(self.transcript_path(wt, session).stat().st_mtime, timezone.utc)
        except OSError:
            mtime = None
        if run and self._alive(run):
            started = datetime.fromisoformat(run["started_at"])
            return WorkerStatus("working", session, max(mtime, started) if mtime else started)
        if self._owner_busy(wt, run):
            return WorkerStatus("owner_busy", session, mtime)
        if not run:
            return WorkerStatus("idle", session, mtime)
        ok, _, text = self._turn(wt)
        if not ok:
            armed = self.store.kv_get(f"limit-run:{wt.path}")
            armed = json.loads(armed) if armed else None
            if armed and armed["run"] == run["started_at"]:
                if now < datetime.fromisoformat(armed["until"]):
                    return WorkerStatus("limited", session, mtime, text)
            elif parse_limit(self.read_text(wt, session), now) is not None:
                return WorkerStatus("limited", session, mtime, text)
        try:
            bstatus = self.store.batch(run["batch"])["status"]
        except KeyError:
            bstatus = None
        if bstatus in _ACTIVE_STATUSES:
            return WorkerStatus("dead", session, mtime, text)
        if bstatus == "waiting_owner":
            return WorkerStatus("waiting", session, mtime, text)
        return WorkerStatus("idle", session, mtime, text)
