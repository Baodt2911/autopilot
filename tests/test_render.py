from datetime import datetime, timezone

import pytest

from autopilot.config import Config
from autopilot.models import Event, PRSnapshot
from autopilot.render import event_file, pending_md, state_md, write_all
from autopilot.store import Store

T0 = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg(tmp_path):
    return Config(name="ivt", repo=tmp_path, author="me", gh_user="me", limits={}, reply={})


def snap():
    return PRSnapshot(589, "Fix X", "https://gh/pr/589", False, "OPEN", "feat/x", "s1", "main", "s0",
                      "MERGEABLE", None, (), (), (), (), ())


def test_event_file_contains_verbatim_body_and_commands(cfg):
    body = "run $(rm -rf ~) ~~~ and ```"
    e = Event("c1", 589, "thread", 4, T0, {"author": "rev", "url": "u", "path": "a.py", "line": 3, "body": body})
    p = event_file(cfg, {"id": 7, "pr": 589}, [e], snap(), {"force_c2_threads": [], "force_c2": [], "pushback": {}})
    t = p.read_text()
    assert p.name == "589-7.md" and body in t and "a.py:3" in t
    assert "autopilot ack 7" in t and "autopilot result 7" in t and "autopilot push 7" in t
    assert "~~~~~text" in t or "~~~~text" in t


def test_pending_md_five_line_question_format(tmp_path):
    s = Store(tmp_path / "s.db")
    s.add_item("Q", 589, 1, "https://t", 'Reviewer hỏi: "x"\nSpec: không đề cập\nVì sao: y\nA: a | B: b', T0)
    lines = pending_md(s.open_items()).split("\n\n")[1].splitlines()
    assert len(lines) == 5 and lines[0].startswith("[Q-ivt-1] PR #589 · ")
    assert lines[4].startswith("A: ") and "| B: " in lines[4]


def test_plan_truncated_and_state(cfg, tmp_path):
    s = Store(tmp_path / "s.db")
    s.add_item("P", 589, 1, "plan", "\n".join(f"l{i}" for i in range(30)), T0)
    assert pending_md(s.open_items()).count("\nl") == 10
    write_all(cfg, s, [snap()])
    assert "#589" in (cfg.data_dir / "STATE.md").read_text()
    assert (cfg.data_dir / "pending.md").exists() and (cfg.data_dir / "backlog.md").exists()
    assert "feat/x" in state_md([snap()], s)
