from autopilot.worktrees import list_worktrees

PORCELAIN = """worktree /r
HEAD aaa
branch refs/heads/main

worktree /r/.worktrees/a
HEAD bbb
branch refs/heads/feat/a

worktree /r/.worktrees/det
HEAD ccc
detached

"""


def test_parse_porcelain_skips_detached(tmp_path):
    calls = []

    def run(args, env):
        calls.append(args)
        return PORCELAIN

    got = list_worktrees(tmp_path, run=run)
    assert set(got) == {"main", "feat/a"}
    assert got["feat/a"].path == "/r/.worktrees/a" and got["main"].branch == "main"
    assert calls[0][:2] == ["git", "-C"] and "--porcelain" in calls[0]
