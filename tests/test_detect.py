from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autopilot.config import Config
from autopilot.detect import detect, is_noop, stack_warnings
from autopilot.models import Comment, PRSnapshot, Review, Thread

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(days=1)
ME = "author-x"


def cfg():
    return Config(name="t", repo=Path("/r"), author=ME, gh_user="owner-z",
                  limits={}, notify={}, reply={}, pr_rules={}, tests={}, worker={})


def cm(id, author="rev", body="please fix", mins=0, upd=None, bot=False, tid="T1"):
    t = T0 + timedelta(minutes=mins)
    return Comment(id=id, author=author, is_bot=bot, body=body, created_at=t,
                   updated_at=upd or t, url=f"https://x/{id}", thread_id=tid,
                   path="a.py", line=7)


def thread(*comments, resolved=False, tid="T1"):
    return Thread(id=tid, is_resolved=resolved, comments=tuple(comments))


def snap(n=1, **kw):
    base = dict(number=n, title="t", url=f"https://x/pr/{n}", is_draft=False, state="OPEN",
                head_ref=f"feat-{n}", head_sha="h1", base_ref="main", base_sha="b1",
                mergeable="MERGEABLE", review_decision=None, threads=(), reviews=(),
                comments=(), failed_checks=(), changed_files=())
    base.update(kw)
    return PRSnapshot(**base)


def run(cur, prev=None):
    return detect(prev, cur, cfg(), NOW)


def test_new_thread_comment_by_reviewer_creates_thread_event():
    c = cm("C1")
    evs = run(snap(threads=(thread(c),)))
    assert len(evs) == 1
    e = evs[0]
    assert e.id == f"C1@{c.updated_at.isoformat()}"
    assert (e.kind, e.pr, e.priority) == ("thread", 1, 4)
    assert e.payload["thread_id"] == "T1"
    assert e.payload["path"] == "a.py" and e.payload["line"] == 7
    assert e.payload["url"] == "https://x/C1"


def test_own_comments_and_threads_ending_with_own_reply_ignored():
    mine = cm("C1", author=ME)
    assert run(snap(threads=(thread(mine),), comments=(cm("C9", author=ME, tid=None),))) == []
    assert run(snap(threads=(thread(cm("C1"), cm("C2", author=ME, mins=1)),))) == []
    evs = run(snap(threads=(thread(cm("C1"), cm("C2", author=ME, mins=1), cm("C3", mins=2)),)))
    assert [e.kind for e in evs] == ["thread"]
    assert evs[0].id.startswith("C3@")


def test_resolved_threads_ignored():
    assert run(snap(threads=(thread(cm("C1"), resolved=True),))) == []


def test_edited_comment_creates_new_event():
    old = cm("C1")
    new = cm("C1", upd=T0 + timedelta(hours=1), body="actually do Y")
    prev = snap(threads=(thread(old),))
    cur = snap(threads=(thread(new),))
    evs = run(cur, prev)
    assert len(evs) == 1
    assert evs[0].id == f"C1@{new.updated_at.isoformat()}"
    assert evs[0].id != f"C1@{old.updated_at.isoformat()}"
    assert run(prev, prev) == []  # unchanged -> nothing new


def test_lgtm_and_approved_only_are_noop():
    assert is_noop("LGTM") and is_noop("lgtm 👍") and is_noop("Approved!")
    assert not is_noop("LGTM but fix X") and not is_noop("")
    evs = run(snap(comments=(cm("C1", body="LGTM", tid=None),),
                   reviews=(Review("R1", "rev", "APPROVED", T0, "", "u"),)))
    assert sorted(e.kind for e in evs) == ["noop", "noop"]
    evs = run(snap(comments=(cm("C2", body="LGTM but fix X", tid=None),)))
    assert [e.kind for e in evs] == ["comment"]


def test_bot_comments_are_processed():
    evs = run(snap(threads=(thread(cm("C1", author="copilot", bot=True)),)))
    assert [e.kind for e in evs] == ["thread"]
    assert evs[0].payload["is_bot"] is True


def test_changes_requested_review_has_priority_0():
    r = Review("R1", "rev", "CHANGES_REQUESTED", T0, "needs work", "https://x/r1")
    evs = run(snap(reviews=(r,)))
    assert [(e.kind, e.priority) for e in evs] == [("review", 0)]
    r2 = Review("R2", "rev", "COMMENTED", T0, "nit", "https://x/r2")
    assert [(e.kind, e.priority) for e in run(snap(reviews=(r2,)))] == [("review", 4)]


def test_conflict_event_once_per_head_sha():
    cur = snap(mergeable="CONFLICTING")
    evs = run(cur)
    assert [(e.id, e.kind, e.priority) for e in evs] == [("conflict:1:h1", "conflict", 1)]
    assert run(cur, cur) == []
    pushed = replace(cur, head_sha="h2")
    assert [e.id for e in run(pushed, cur)] == ["conflict:1:h2"]
    assert run(snap(mergeable="UNKNOWN")) == []


def test_base_change_event_on_base_sha_or_ref_change():
    prev = snap()
    evs = run(replace(prev, base_sha="b2"), prev)
    assert [(e.id, e.kind, e.priority) for e in evs] == [("base:1:b2", "base", 2)]
    evs = run(replace(prev, base_ref="dev"), prev)
    assert [e.id for e in evs] == ["base:1:b1"]
    assert run(prev, None) == []  # no baseline -> no base event


def test_ci_failure_event_per_check():
    evs = run(snap(failed_checks=("lint", "test")))
    assert sorted((e.id, e.kind, e.priority) for e in evs) == [
        ("ci:1:h1:lint", "ci", 3), ("ci:1:h1:test", "ci", 3)]


def test_merged_event():
    prev = snap()
    cur = replace(prev, state="MERGED", merged_at=T0)
    evs = run(cur, prev)
    assert [(e.id, e.kind) for e in evs] == [("merged:1", "merged")]
    assert run(cur, cur) == []


def test_pushback_count_in_payload():
    a = [cm("C1"), cm("C2", author=ME, mins=1), cm("C3", mins=2)]
    assert run(snap(threads=(thread(*a),)))[0].payload["pushback"] == 1
    b = a + [cm("C4", author=ME, mins=3), cm("C5", mins=4)]
    assert run(snap(threads=(thread(*b),)))[0].payload["pushback"] == 2
    assert run(snap(threads=(thread(cm("C1")),)))[0].payload["pushback"] == 0


def test_stack_warning_parent_merged_lists_children():
    parent = snap(10, state="MERGED", head_ref="feat-a")
    child = snap(11, base_ref="feat-a", mergeable="CONFLICTING")
    other = snap(12)
    ws = stack_warnings({}, {11: child, 12: other}, [parent])
    assert len(ws) == 1
    assert "#10" in ws[0] and "#11" in ws[0] and "#12" not in ws[0]
    assert "feat-a" in ws[0] and "CONFLICTING" in ws[0]


def test_stack_warning_child_merged_before_parent():
    parent = snap(10, head_ref="feat-a")
    child = snap(11, state="MERGED", base_ref="feat-a")
    ws = stack_warnings({10: parent}, {10: parent}, [child])
    assert len(ws) == 1 and "#11" in ws[0] and "#10" in ws[0]
    assert stack_warnings({}, {}, [snap(11, state="MERGED")]) == []


def test_edited_non_last_comment_creates_event():
    c1 = cm("C1")
    r1 = cm("C2", author=ME, mins=5)
    prev = snap(threads=(thread(c1, r1),))
    assert run(prev) == []
    c1e = cm("C1", upd=T0 + timedelta(minutes=30), body="changed my mind")
    evs = run(snap(threads=(thread(c1e, r1),)), prev)
    assert [e.id for e in evs] == [f"C1@{c1e.updated_at.isoformat()}"]


def test_reviewer_comment_before_author_reply_not_reemitted():
    c1, r1, c3 = cm("C1"), cm("C2", author=ME, mins=5), cm("C3", mins=10)
    evs = run(snap(threads=(thread(c1, r1, c3),)))
    assert [e.id.split("@")[0] for e in evs] == ["C3"]
