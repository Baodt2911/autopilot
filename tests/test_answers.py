from datetime import datetime, timezone

import pytest

from autopilot.answers import add_backlog, answer, approve, push_decisions
from autopilot.config import Config
from autopilot.store import ItemClosed, Store

T0 = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg(tmp_path):
    return Config(name="ivt", repo=tmp_path, author="me", gh_user="me", limits={},
                  reply={"decision_template": "Updated: {summary}"})


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "s.db")
    s.add_item("Q", 589, 1, "https://t", "body", T0)
    s.add_item("P", 589, 1, "plan", "x", T0)
    return s


def test_answer_writes_decision_and_creates_priority_batch(store, cfg):
    bid = answer(store, cfg, "Q-ivt-1", "B", "giữ quyền admin", "ý reply", "cli", T0)
    assert "## Q-ivt-1 · 2026-11-12" in (cfg.data_dir / "decisions.md").read_text()
    b = store.batch(bid)
    assert b["pr"] == 589 and b["status"] == "new"
    (e,) = [e for e in store.events(pr=589) if e.id in b["event_ids"]]
    assert e.kind == "answer" and e.priority == 0
    assert e.payload["answer"] == "B" and e.payload["note"] == "giữ quyền admin" and e.payload["reply_hint"] == "ý reply"
    assert e.payload["decision_template"] == "Updated: {summary}"


def test_answer_is_idempotent_and_rejects_unknown(store, cfg):
    answer(store, cfg, "Q-ivt-1", "A", now=T0)
    with pytest.raises(ItemClosed):
        answer(store, cfg, "Q-ivt-1", "B", now=T0)
    assert len(store.batches()) == 1
    with pytest.raises(KeyError):
        answer(store, cfg, "Q-ivt-99", "A", now=T0)
    with pytest.raises(ValueError):
        approve(store, cfg, "Q-ivt-1", True, now=T0)


def test_reply_hint_passed_verbatim(store, cfg):
    hint = "Nói rằng: đã sửa theo yêu cầu, cảm ơn."
    bid = answer(store, cfg, "Q-ivt-1", "A", reply_hint=hint, now=T0)
    e = [e for e in store.events() if e.id in store.batch(bid)["event_ids"]][0]
    assert e.payload["reply_hint"] == hint
    bid2 = approve(store, cfg, "P-ivt-2", False, "no", now=T0)
    assert store.batch(bid2)["id"] != bid


class FakeGH:
    def __init__(self):
        self.posts = []

    def post_issue_comment(self, issue, body):
        self.posts.append((issue, body))
        return f"https://gh/c/{len(self.posts)}"


def test_push_decisions_posts_only_new_ones_once(store, cfg):
    gh = FakeGH()
    answer(store, cfg, "Q-ivt-1", "A", now=T0)
    assert push_decisions(store, cfg, gh, 10, T0) == "https://gh/c/1"
    assert "Q-ivt-1" in gh.posts[0][1]
    approve(store, cfg, "P-ivt-2", True, now=T0)
    push_decisions(store, cfg, gh, 10, T0)
    assert len(gh.posts) == 2 and "Q-ivt-1" not in gh.posts[1][1] and "P-ivt-2" in gh.posts[1][1]
    with pytest.raises(ValueError):
        push_decisions(store, cfg, gh, 10, T0)
    assert len(gh.posts) == 2


def test_add_backlog(cfg):
    add_backlog(cfg, "spec-gap", 589, "thiếu\nmô tả", T0)
    assert "[spec-gap] PR #589" in (cfg.data_dir / "backlog.md").read_text()
    with pytest.raises(ValueError):
        add_backlog(cfg, "x", 1, "t", T0)
