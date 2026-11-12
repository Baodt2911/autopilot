import subprocess
from pathlib import Path

import pytest

from autopilot import cli, router
from autopilot.config import load_config
from autopilot.store import Store
from conftest import T0, FakeGitHub, snap


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def new_repo(path, branch="feat/x"):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", branch)
    git(path, "config", "user.email", "t@t")
    git(path, "config", "user.name", "t")
    return path


def commit(repo, name, text="x"):
    p = Path(repo) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    git(repo, "add", "-f", name)
    git(repo, "commit", "-qm", f"add {name}")


@pytest.fixture
def tmp_git_repo(tmp_path):
    r = new_repo(tmp_path / "repo")
    commit(r, "README.md")
    return r


@pytest.fixture
def tmp_git_repo_with_remote(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    r = new_repo(tmp_path / "repo")
    commit(r, "src/base.py")
    git(r, "remote", "add", "origin", str(bare))
    git(r, "push", "-q", "-u", "origin", "feat/x")
    return r


@pytest.fixture
def proj(tmp_path, monkeypatch):
    def make(repo):
        cd = tmp_path / "cfg"
        cd.mkdir(exist_ok=True)
        (cd / "t.toml").write_text(f'repo = "{repo}"\nauthor = "me"\ngh_user = "me"\n')
        monkeypatch.setattr(cli, "_github", lambda cfg: FakeGitHub())
        cfg = load_config("t", cd)
        return cfg, Store(cfg.db_path), ["--config-dir", str(cd), "--project", "t"]
    return make


def run(capsys, argv):
    rc = cli.main(argv)
    out = capsys.readouterr()
    return rc, out.out + out.err


def test_init_adds_exclude_once_and_creates_dirs(tmp_git_repo, proj, capsys):
    cfg, _, opts = proj(tmp_git_repo)
    for _ in range(2):
        rc, out = run(capsys, ["init", "t", "--config-dir", opts[1]])
        assert rc == 0
    ex = (tmp_git_repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert ex.count(".autopilot/") == 1
    for d in ("events", "plans", "qa", "screenshots"):
        assert (cfg.data_dir / d).is_dir()
    assert "systemd" in out


def test_guard_staged_blocks_forbidden(tmp_git_repo, proj, capsys):
    _, _, opts = proj(tmp_git_repo)
    (tmp_git_repo / "a.png").write_bytes(b"x")
    git(tmp_git_repo, "add", "-f", "a.png")
    rc, out = run(capsys, ["guard-staged", "--repo", str(tmp_git_repo), *opts])
    assert rc == 1 and "a.png" in out
    git(tmp_git_repo, "reset", "-q")
    (tmp_git_repo / "src").mkdir()
    (tmp_git_repo / "src/x.py").write_text("1")
    git(tmp_git_repo, "add", "src/x.py")
    assert run(capsys, ["guard-staged", "--repo", str(tmp_git_repo), *opts])[0] == 0


def push_setup(repo, proj):
    cfg, store, opts = proj(repo)
    store.save_snapshot(581, router.snapshot_to_dict(snap()))
    bid = store.create_batch(581, [], T0)
    return cfg, store, opts, bid


def test_push_refuses_wrong_branch_forbidden_and_non_ff(tmp_git_repo_with_remote, tmp_path, proj, capsys,
                                                         monkeypatch):
    r = tmp_git_repo_with_remote
    cfg, store, opts, bid = push_setup(r, proj)
    monkeypatch.chdir(r)
    calls = []
    real = cli._git
    monkeypatch.setattr(cli, "_git", lambda args, cwd: (calls.append(list(args)), real(args, cwd))[1])

    # wrong branch
    git(r, "checkout", "-q", "-b", "other")
    rc, out = run(capsys, ["push", str(bid), *opts])
    assert rc == 1 and "nhánh" in out.lower()
    git(r, "checkout", "-q", "feat/x")

    # forbidden file in commits ahead of origin
    commit(r, "a.png")
    rc, out = run(capsys, ["push", str(bid), *opts])
    assert rc == 1 and "a.png" in out
    git(r, "reset", "-q", "--hard", "HEAD~1")

    # remote ahead (non fast-forward)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "remote.git"), str(other)], check=True)
    git(other, "config", "user.email", "t@t")
    git(other, "config", "user.name", "t")
    git(other, "checkout", "-q", "feat/x")
    commit(other, "src/remote.py")
    git(other, "push", "-q", "origin", "feat/x")
    commit(r, "src/local.py")
    rc, out = run(capsys, ["push", str(bid), *opts])
    assert rc == 1
    assert not [c for c in calls if c[:1] == ["push"]]

    # valid: rebase onto remote then push
    git(r, "pull", "-q", "--rebase", "origin", "feat/x")
    calls.clear()
    rc, out = run(capsys, ["push", str(bid), *opts])
    assert rc == 0, out
    pushes = [c for c in calls if c[:1] == ["push"]]
    assert pushes == [["push", "origin", "HEAD:feat/x"]]
    assert not any("--force" in a or "-f" == a for c in calls for a in c)
    assert git(r, "rev-parse", "HEAD") == git(tmp_path / "remote.git", "rev-parse", "feat/x")
    assert any(t["kind"] == "pushed" for t in store.timeline(581))


def test_worker_protocol_ack_start_item_result(tmp_git_repo, tmp_path, proj, capsys):
    cfg, store, opts, bid = push_setup(tmp_git_repo, proj)
    store.transition(bid, "delivered", T0, session="s1")
    assert run(capsys, ["ack", str(bid), *opts])[0] == 0
    assert store.batch(bid)["status"] == "acked"
    assert run(capsys, ["start", str(bid), *opts])[0] == 0
    assert store.batch(bid)["status"] == "in_progress"
    body = tmp_path / "q.md"
    body.write_text("Chọn A hay B?")
    rc, out = run(capsys, ["item", str(bid), "Q", "--title", "A hay B", "--file", str(body), *opts])
    assert rc == 0 and "Q-ivt-1" in out
    assert store.item("Q-ivt-1")["body_md"] == "Chọn A hay B?"
    res = tmp_path / "r.md"
    res.write_text("done it")
    rc, out = run(capsys, ["result", str(bid), "--file", str(res), "--status", "waiting_owner", *opts])
    assert rc == 0 and store.batch(bid)["status"] == "waiting_owner"
    rc, _ = run(capsys, ["ack", "999", *opts])
    assert rc == 1


def test_config_set_rejects_non_editable(tmp_git_repo, proj, capsys):
    cfg, _, opts = proj(tmp_git_repo)
    rc, out = run(capsys, ["config", "set", "repo", "/x", *opts])
    assert rc == 1 and "repo" in out
    assert run(capsys, ["config", "set", "max_workers", "3", *opts])[0] == 0
    rc, out = run(capsys, ["config", "get", "max_workers", *opts])
    assert rc == 0 and out.strip() == "3"


def test_answer_command_prints_vietnamese_confirmation(tmp_git_repo, proj, capsys):
    cfg, store, opts = proj(tmp_git_repo)
    code = store.add_item("Q", 581, 1, "A hay B", "body", T0)
    rc, out = run(capsys, ["answer", code, "A", "ghi chú", "--reply", "ok", *opts])
    assert rc == 0 and "Đã ghi" in out and code in out
    rc, out = run(capsys, ["answer", code, "A", *opts])
    assert rc == 1 and "đã" in out
    rc, out = run(capsys, ["answer", "Q-ivt-99", "A", *opts])
    assert rc == 1 and "Q-ivt-99" in out


def test_pause_resume_pr_requeues_wake_cap_events(tmp_git_repo, proj, capsys):
    cfg, store, opts = proj(tmp_git_repo)
    from autopilot.models import Event
    store.add_event(Event("e1", 581, "comment", 4, T0, {}))
    bid = store.create_batch(581, ["e1"], T0)
    store.mark_events(["e1"], "batched")
    store.transition(bid, "cancelled", T0)
    store.kv_set("paused_pr:581", "limit")
    assert run(capsys, ["resume", "581", *opts])[0] == 0
    assert store.kv_get("paused_pr:581") is None
    assert store.events(status="new")[0].id == "e1"
    assert run(capsys, ["pause", *opts])[0] == 0 and store.kv_get("paused")
    assert run(capsys, ["resume", *opts])[0] == 0 and store.kv_get("paused") is None


def test_dry_run_claim_rejected_and_writes_nothing(tmp_git_repo, proj, capsys):
    cfg, store, opts = proj(tmp_git_repo)
    rc, out = run(capsys, ["scan", "--dry-run", "--pr", "581", "--claim", *opts])
    assert rc == 2 and "--claim" in out
    assert store.batches() == [] and not cfg.events_dir.exists()


def test_push_refuses_terminal_batch_or_closed_pr(tmp_git_repo_with_remote, proj, capsys, monkeypatch):
    r = tmp_git_repo_with_remote
    cfg, store, opts, bid = push_setup(r, proj)
    monkeypatch.chdir(r)
    store.transition(bid, "cancelled", T0)
    rc, out = run(capsys, ["push", str(bid), *opts])
    assert rc == 1 and "không push" in out
    b2 = store.create_batch(581, [], T0)
    store.save_snapshot(581, router.snapshot_to_dict(snap(state="MERGED")))
    assert run(capsys, ["push", str(b2), *opts])[0] == 1


def test_missing_file_is_vietnamese_error(tmp_git_repo, proj, capsys):
    cfg, store, opts, bid = push_setup(tmp_git_repo, proj)
    rc, out = run(capsys, ["item", str(bid), "Q", "--title", "t", "--file", "/nonexistent/x", *opts])
    assert rc == 1 and "Lỗi" in out and "Traceback" not in out


def test_export_events_writes_files_only(tmp_git_repo, tmp_path, monkeypatch, capsys):
    from conftest import comment
    cd = tmp_path / "cfg"
    cd.mkdir()
    (cd / "t.toml").write_text(f'repo = "{tmp_git_repo}"\nauthor = "me"\ngh_user = "me"\n')
    gh = FakeGitHub()
    gh.closed = [snap(579, state="MERGED", comments=(comment("c9", "why this?", author="rev"),))]
    monkeypatch.setattr(cli, "_github", lambda cfg: gh)
    monkeypatch.setattr(cli, "_transport", lambda *a: pytest.fail("no spawn"))
    out = tmp_path / "out"
    rc, text = run(capsys, ["scan", "--dry-run", "--export-events", "--pr", "579", "--out", str(out),
                            "--config-dir", str(cd), "--project", "t"])
    assert rc == 0, text
    files = list(out.glob("579-*.md"))
    assert len(files) == 1
    body = files[0].read_text()
    assert "why this?" in body and "## Bắt buộc C2" in body
    assert not (tmp_git_repo / ".autopilot").exists()  # zero store writes


def test_export_events_requires_pr_out_and_dry_run(capsys):
    rc, _ = run(capsys, ["scan", "--export-events", "--pr", "1", "--out", "x"])
    assert rc == 2
    rc, _ = run(capsys, ["scan", "--dry-run", "--export-events", "--out", "x"])
    assert rc == 2


def test_export_events_rewind_reopens_threads_before_author_reply(tmp_git_repo, tmp_path, monkeypatch, capsys):
    from autopilot.models import Thread
    from conftest import comment
    cd = tmp_path / "cfg"
    cd.mkdir()
    (cd / "t.toml").write_text(f'repo = "{tmp_git_repo}"\nauthor = "me"\ngh_user = "me"\n')
    gh = FakeGitHub()
    th = Thread("T1", True, (comment("c1", "first ask", author="rev"), comment("c2", "done", author="me"),
                             comment("c3", "later chat", author="rev")))
    gh.closed = [snap(579, state="MERGED", threads=(th,))]
    monkeypatch.setattr(cli, "_github", lambda cfg: gh)
    out = tmp_path / "out"
    rc, text = run(capsys, ["scan", "--dry-run", "--export-events", "--rewind", "--pr", "579", "--out", str(out),
                            "--config-dir", str(cd), "--project", "t"])
    assert rc == 0, text
    body = next(out.glob("579-*.md")).read_text()
    assert "first ask" in body and "later chat" not in body
