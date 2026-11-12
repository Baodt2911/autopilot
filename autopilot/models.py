"""Shared dataclasses used by every module."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Comment:
    id: str
    author: str
    is_bot: bool
    body: str
    created_at: datetime
    updated_at: datetime
    url: str
    thread_id: str | None = None
    path: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class Thread:
    id: str
    is_resolved: bool
    comments: tuple[Comment, ...]


@dataclass(frozen=True)
class Review:
    id: str
    author: str
    state: str
    submitted_at: datetime
    body: str
    url: str


@dataclass(frozen=True)
class PRSnapshot:
    number: int
    title: str
    url: str
    is_draft: bool
    state: str  # OPEN | MERGED | CLOSED
    head_ref: str
    head_sha: str
    base_ref: str
    base_sha: str
    mergeable: str  # MERGEABLE | CONFLICTING | UNKNOWN
    review_decision: str | None  # APPROVED | CHANGES_REQUESTED | REVIEW_REQUIRED | None
    threads: tuple[Thread, ...]
    reviews: tuple[Review, ...]
    comments: tuple[Comment, ...]
    failed_checks: tuple[str, ...]
    changed_files: tuple[str, ...]
    merged_at: datetime | None = None


@dataclass(frozen=True)
class Event:
    id: str
    pr: int
    kind: str  # thread | comment | review | conflict | base | ci | merged | answer | noop
    priority: int  # 0 highest: CHANGES_REQUESTED=0, conflict=1, base=2, ci=3, rest=4
    created_at: datetime
    payload: dict


@dataclass(frozen=True)
class WorkerStatus:
    # none | idle | working | waiting | dead | limited | owner_busy
    state: str
    session: str | None
    updated_at: datetime | None
    last_text: str = ""


@dataclass(frozen=True)
class Worktree:
    branch: str
    path: str
