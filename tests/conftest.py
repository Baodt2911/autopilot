import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from autopilot.config import Config  # noqa: E402
from autopilot.models import Comment, PRSnapshot, WorkerStatus, Worktree  # noqa: E402
from autopilot.store import Store  # noqa: E402

T0 = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)


def snap(number=581, head="feat/x", comments=(), state="OPEN", changed=("src/a.py",), base="main", **kw):
    d = dict(number=number, title="T", url=f"https://gh/pr/{number}", is_draft=False, state=state,
             head_ref=head, head_sha="sha1", base_ref=base, base_sha="b1", mergeable="MERGEABLE",
             review_decision=None, threads=(), reviews=(), comments=tuple(comments), failed_checks=(),
             changed_files=tuple(changed), merged_at=None)
    d.update(kw)
    return PRSnapshot(**d)


def comment(cid="c1", body="please fix", author="rev", at=T0):
    return Comment(id=cid, author=author, is_bot=False, body=body, created_at=at, updated_at=at, url="https://gh/c")


class FakeGitHub:
    def __init__(self):
        self.open = []
        self.closed = []
        self.error = None
        self.posted = []

    def open_prs(self):
        if self.error:
            raise self.error
        return list(self.open)

    def recently_closed(self, numbers):
        return [s for s in self.closed if s.number in numbers]

    def pr_by_number(self, number):
        return next((s for s in [*self.open, *self.closed] if s.number == number), None)

    def reply_to_thread(self, thread_id, body):
        self.posted.append(("thread", thread_id, body))
        return f"https://gh/thread/{thread_id}#r"

    def reply_to_comment(self, pr, comment_id, body):
        self.posted.append(("comment", pr, comment_id, body))
        return f"https://gh/pull/{pr}#r{comment_id}"

    def post_issue_comment(self, issue, body):
        self.posted.append((issue, body))
        return f"https://gh/issues/{issue}#c"

    def token(self):
        return "tok"


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.states = {}  # branch -> WorkerStatus
        self.prompts = []

    def set_worktrees(self, paths):
        self.calls.append(("set_worktrees", tuple(paths)))

    def status(self, wt, now=None):
        return self.states.get(wt.branch, WorkerStatus("none", None, None))

    def start_worker(self, wt, prompt, batch_id):
        self.calls.append(("start", wt.branch, batch_id))
        self.prompts.append(prompt)
        return f"sess-{batch_id}"

    def deliver(self, wt, session, prompt, batch_id):
        self.calls.append(("deliver", wt.branch, session, batch_id))
        self.prompts.append(prompt)

    def stop_worker(self, wt, session, *, close=False):
        self.calls.append(("stop", wt.branch, session, close))

    def read_text(self, wt, session):
        return ""

    def forbidden_ops(self, wt):
        return []

    def launches(self):
        return [c for c in self.calls if c[0] in ("start", "deliver")]


class FakeNotifier:
    def __init__(self):
        self.alerts = []

    def alert(self, kind, title, body, now):
        self.alerts.append((kind, title, body))
        return True

    def reminders(self, now):
        return 0

    def digest(self, now, summary):
        return False


@pytest.fixture
def env(tmp_path, monkeypatch):
    from autopilot import router

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = Config(name="t", repo=repo, author="me", gh_user="me",
                 limits={"thread_rounds": 3, "wakes_per_pr_day": 5},
                 notify={"hours": "00:00-23:59", "remind_after_h": 4, "digest_at": "08:00"},
                 worker={})
    store = Store(cfg.db_path)
    wts = {"feat/x": Worktree("feat/x", str(tmp_path / "wt-x"))}
    monkeypatch.setattr(router, "list_worktrees", lambda repo: wts)
    monkeypatch.setattr(router, "BACKUP_ROOT", tmp_path / "backup")
    class E:  # noqa
        pass
    e = E()
    e.cfg, e.store, e.gh, e.t, e.n, e.wts = cfg, store, FakeGitHub(), FakeTransport(), FakeNotifier(), wts
    e.scan = lambda now, **kw: router.scan(cfg, store, e.gh, e.t, e.n, now, **kw)
    return e
