import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from autopilot.config import Config
from autopilot.notify import Notifier, notify_send
from autopilot.store import Store

BKK = timezone(timedelta(hours=7))


@pytest.fixture
def local_bkk(monkeypatch):
    old = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Asia/Bangkok")
    time.tzset()
    yield
    if old is None:
        monkeypatch.delenv("TZ", raising=False)
    else:
        monkeypatch.setenv("TZ", old)
    time.tzset()


@pytest.fixture
def env(tmp_path, local_bkk):
    cfg = Config(name="t", repo=tmp_path, author="me", gh_user="me",
                 notify={"hours": "07:00-22:00", "remind_after_h": 4, "digest_at": "08:00"})
    store = Store(tmp_path / "s.db")
    sent = []
    return cfg, store, sent, Notifier(cfg, store, send=lambda t, b: sent.append((t, b)))


def at(h, m=0, d=12):
    return datetime(2026, 11, d, h, m, tzinfo=BKK)


def test_alert_inside_hours_sends(env):
    _, _, sent, n = env
    assert n.alert("question", "Câu hỏi", "nội dung", at(10)) is True
    assert len(sent) == 1 and "Câu hỏi" in sent[0][0] + sent[0][1]


def test_alert_outside_hours_goes_to_digest_queue(env):
    _, store, sent, n = env
    assert n.alert("incident", "Sự cố", "x", at(23, 30)) is False
    assert sent == []
    q = json.loads(store.kv_get("digest_queue"))
    assert len(q) == 1 and q[0]["kind"] == "incident"


def test_quiet_hours_use_local_time(env):
    _, _, sent, n = env
    now = datetime(2026, 11, 12, 16, 0, tzinfo=timezone.utc)  # 23:00 Bangkok
    assert n.alert("plan", "t", "b", now) is False and sent == []
    now = datetime(2026, 11, 12, 4, 0, tzinfo=timezone.utc)  # 11:00 Bangkok
    assert n.alert("plan", "t", "b", now) is True


def test_window_boundaries_and_wrap(env):
    cfg, store, sent, n = env
    assert n.alert("plan", "t", "b", at(7, 0)) is True
    assert n.alert("plan", "t", "b", at(22, 0)) is False
    cfg.notify["hours"] = "22:00-06:00"
    assert n.alert("plan", "t", "b", at(23)) is True
    assert n.alert("plan", "t", "b", at(12)) is False


def test_reminder_after_four_hours_once(env):
    _, store, sent, n = env
    c = store.add_item("Q", 1, 1, "t", "b", at(8))
    assert n.reminders(at(11)) == 0
    assert n.reminders(at(12, 1)) == 1
    assert n.reminders(at(13)) == 0
    assert store.item(c)["reminded_at"] is not None
    c2 = store.add_item("Q", 1, 1, "t", "b", at(9))
    assert n.reminders(at(23)) == 0  # outside window
    assert store.item(c2)["reminded_at"] is None


def test_digest_once_per_day_after_eight(env):
    _, store, sent, n = env
    n.alert("incident", "s", "b", at(23, 30, d=11))
    assert n.digest(at(7, 30), "tóm tắt") is False
    assert n.digest(at(8, 0), "tóm tắt") is True
    assert "tóm tắt" in sent[-1][1] and "s" in sent[-1][1]
    assert json.loads(store.kv_get("digest_queue")) == []
    assert n.digest(at(9), "tóm tắt") is False
    assert n.digest(at(8, 0, d=13), "tóm tắt") is True


def test_send_failure_is_swallowed(env):
    cfg, store, _, _ = env

    def boom(t, b):
        raise FileNotFoundError("notify-send")

    n = Notifier(cfg, store, send=boom)
    assert n.alert("plan", "t", "b", at(10)) is False
    assert any("notify" in r["kind"] for r in store.timeline())
    assert len(json.loads(store.kv_get("digest_queue"))) == 1


def test_notify_send_uses_list_args(monkeypatch):
    calls = []
    monkeypatch.setattr("autopilot.notify.subprocess.run", lambda *a, **k: calls.append((a, k)))
    notify_send("T", "B")
    assert calls[0][0][0] == ["notify-send", "-a", "autopilot", "T", "B"]
    assert not calls[0][1].get("shell")
