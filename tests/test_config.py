from pathlib import Path

import pytest

from autopilot.config import list_projects, load_config, set_override


def write(p: Path, text: str) -> None:
    p.write_text(text)


def test_defaults_and_paths(tmp_path):
    write(tmp_path / "ivt.toml", 'repo="/r"\nauthor="vyn-bao-do"\ngh_user="vyn-bao-do"\n')
    c = load_config("ivt", tmp_path)
    assert (c.scan_minutes, c.debounce_min, c.max_workers) == (5, 10, 5)
    assert c.limits == {"thread_rounds": 3, "wakes_per_pr_day": 5}
    assert c.notify == {"hours": "07:00-22:00", "remind_after_h": 4, "digest_at": "08:00"}
    assert ".autopilot/" in c.forbidden and "*.png" in c.forbidden
    assert c.db_path == Path("/r/.autopilot/state.db")
    assert c.transport == "headless" and c.worker["claude_bin"] == "claude"


def test_override_wins_and_base_file_untouched(tmp_path):
    base = 'repo="/r"\nauthor="a"\ngh_user="a"\n[limits]\nthread_rounds = 3 # mine\n'
    write(tmp_path / "ivt.toml", base)
    set_override("ivt", "limits.thread_rounds", 4, tmp_path)
    assert load_config("ivt", tmp_path).limits["thread_rounds"] == 4
    assert (tmp_path / "ivt.toml").read_text() == base


def test_set_override_rejects_non_editable_key(tmp_path):
    write(tmp_path / "ivt.toml", 'repo="/r"\nauthor="a"\ngh_user="a"\n')
    with pytest.raises(KeyError):
        set_override("ivt", "gh_user", "x", tmp_path)


def test_missing_gh_user_is_an_error(tmp_path):
    write(tmp_path / "ivt.toml", 'repo="/r"\nauthor="a"\n')
    with pytest.raises(ValueError, match="gh_user"):
        load_config("ivt", tmp_path)


def test_worker_defaults_and_toml_merge(tmp_path):
    write(tmp_path / "ivt.toml", 'repo="/r"\nauthor="a"\ngh_user="a"\n')
    w = load_config("ivt", tmp_path).worker
    assert w["permission_mode"] == "acceptEdits"
    assert "Bash(rtk git add:*)" in w["allowed_tools"]
    assert not any("pnpm" in t for t in w["allowed_tools"])
    for c in ("fetch", "merge", "show", "grep", "log", "rev-parse", "branch --show-current", "restore", "ls-files"):
        assert f"Bash(git {c}:*)" in w["allowed_tools"] and f"Bash(rtk git {c}:*)" in w["allowed_tools"]
    for c in ("git push", "rm", "git rebase", "git reset --hard", "git push --force", "gh"):
        assert f"Bash({c}:*)" in w["disallowed_tools"] and f"Bash(rtk {c}:*)" in w["disallowed_tools"]
    assert not any(t.startswith("Bash(gh") for t in w["allowed_tools"])
    write(tmp_path / "ivt.toml",
          'repo="/r"\nauthor="a"\ngh_user="a"\n[worker]\n'
          'claude_bin="/x/claude"\nallowed_tools=["Read","Bash(rtk pnpm test:*)"]\n')
    w2 = load_config("ivt", tmp_path).worker
    assert w2["claude_bin"] == "/x/claude"
    assert w2["allowed_tools"] == ["Read", "Bash(rtk pnpm test:*)"]
    assert w2["permission_mode"] == "acceptEdits"
    assert w2["disallowed_tools"] == w["disallowed_tools"]


def test_list_projects_skips_overrides(tmp_path):
    write(tmp_path / "ivt.toml", 'repo="/r"\nauthor="a"\ngh_user="a"\n')
    set_override("ivt", "max_workers", 2, tmp_path)
    assert list_projects(tmp_path) == ["ivt"]
    assert load_config("ivt", tmp_path).max_workers == 2
