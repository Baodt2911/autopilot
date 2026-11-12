"""Transport protocol: how the router runs Claude Code workers."""
from __future__ import annotations

from typing import Protocol

from ..config import Config
from ..models import WorkerStatus, Worktree
from ..store import Store


class Transport(Protocol):
    def status(self, wt: Worktree) -> WorkerStatus: ...

    def start_worker(self, wt: Worktree, prompt: str, batch_id: int) -> str:
        """Start a new session, return its id."""

    def deliver(self, wt: Worktree, session: str, prompt: str, batch_id: int) -> None:
        """One `--resume` turn on an existing session."""

    def stop_worker(self, wt: Worktree, session: str, *, close: bool = False) -> None:
        """Stop the process; close=True means never resume this session again."""

    def read_text(self, wt: Worktree, session: str) -> str:
        """Last turn's result JSON + stderr + last 50 transcript lines."""


def get_transport(cfg: Config, store: Store) -> Transport:
    if cfg.transport == "headless":
        from .headless import HeadlessTransport

        return HeadlessTransport(cfg, store)
    if cfg.transport == "orca":
        raise NotImplementedError("transport orca thuộc giai đoạn 3")
    raise ValueError(f"unknown transport: {cfg.transport}")
