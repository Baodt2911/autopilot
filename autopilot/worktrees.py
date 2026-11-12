"""Discover git worktrees: `git worktree list --porcelain` -> {branch: Worktree}."""
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from .github import run_cmd
from .models import Worktree


def list_worktrees(repo: Path, run: Callable[[list[str], dict], str] = run_cmd) -> dict[str, Worktree]:
    out = run(["git", "-C", str(repo), "worktree", "list", "--porcelain"], dict(os.environ))
    result: dict[str, Worktree] = {}
    path: str | None = None
    branch: str | None = None

    def flush() -> None:
        if path and branch:  # detached / bare entries have no branch line
            result[branch] = Worktree(branch=branch, path=path)

    for line in out.splitlines():
        if line.startswith("worktree "):
            flush()
            path, branch = line[len("worktree "):], None
        elif line.startswith("branch "):
            ref = line[len("branch "):]
            branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    flush()
    return result
