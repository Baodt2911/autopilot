"""Project config: <name>.toml (hand written) + <name>.overrides.toml (generated)."""
from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "autopilot" / "projects"

EDITABLE_KEYS = frozenset({
    "limits.thread_rounds", "limits.wakes_per_pr_day",
    "notify.hours", "notify.remind_after_h", "notify.digest_at",
    "reply.decision_template", "max_workers", "debounce_min",
})

DEFAULT_FORBIDDEN = (
    ".autopilot/", "docs/superpowers/", "docs/handoff/",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp",
)

_EXTRA_GIT = ("fetch", "merge", "show", "grep", "log", "rev-parse", "branch --show-current", "restore", "ls-files")
_EXTRA_DENY = ("git rebase", "git reset --hard", "git push --force", "gh")

_DICT_DEFAULTS: dict[str, dict] = {
    "limits": {"thread_rounds": 3, "wakes_per_pr_day": 5},
    "notify": {"hours": "07:00-22:00", "remind_after_h": 4, "digest_at": "08:00"},
    "reply": {},
    "pr_rules": {},
    "tests": {},
    "worker": {
        "claude_bin": "claude",
        "permission_mode": "acceptEdits",
        "allowed_tools": [
            "Edit", "Write", "Read",
            "Bash(git add:*)", "Bash(git commit:*)", "Bash(git status:*)",
            "Bash(git diff:*)", "Bash(echo:*)", "Bash(autopilot:*)",
            "Bash(rtk git add:*)", "Bash(rtk git commit:*)", "Bash(rtk git status:*)",
            "Bash(rtk git diff:*)", "Bash(rtk echo:*)", "Bash(rtk autopilot:*)",
            *(f"Bash({p}git {c}:*)" for c in _EXTRA_GIT for p in ("", "rtk ")),
        ],
        "disallowed_tools": [
            "Bash(git push:*)", "Bash(rtk git push:*)", "Bash(rm:*)", "Bash(rtk rm:*)",
            *(f"Bash({p}{c}:*)" for c in _EXTRA_DENY for p in ("", "rtk ")),
        ],
    },
}

_REQUIRED = ("repo", "author", "gh_user")


@dataclass(frozen=True)
class Config:
    name: str
    repo: Path
    author: str
    gh_user: str
    transport: str = "headless"
    scan_minutes: int = 5
    debounce_min: int = 10
    max_workers: int = 5
    worker_model: str = "claude-sonnet-5-5"
    spec_branch: str = "main"
    spec_sources: tuple[str, ...] = ()
    # Test-only: the author's own comments starting with this prefix count as reviewer comments.
    self_review_prefix: str | None = None
    forbidden: tuple[str, ...] = DEFAULT_FORBIDDEN
    limits: dict = None  # type: ignore[assignment]
    notify: dict = None  # type: ignore[assignment]
    reply: dict = None  # type: ignore[assignment]
    pr_rules: dict = None  # type: ignore[assignment]
    tests: dict = None  # type: ignore[assignment]
    worker: dict = None  # type: ignore[assignment]

    @property
    def data_dir(self) -> Path:
        return self.repo / ".autopilot"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def events_dir(self) -> Path:
        return self.data_dir / "events"

    @property
    def plans_dir(self) -> Path:
        return self.data_dir / "plans"

    @property
    def qa_dir(self) -> Path:
        return self.data_dir / "qa"

    @property
    def screenshots_dir(self) -> Path:
        return self.data_dir / "screenshots"


def _read(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(name: str, config_dir: Path = DEFAULT_CONFIG_DIR) -> Config:
    config_dir = Path(config_dir)
    base = config_dir / f"{name}.toml"
    if not base.exists():
        raise FileNotFoundError(f"project config not found: {base}")
    data = _read(base)
    ovr = config_dir / f"{name}.overrides.toml"
    if ovr.exists():
        data = _merge(data, _read(ovr))
    for key in _REQUIRED:
        if not data.get(key):
            raise ValueError(f"missing required key '{key}' in {base}")
    groups = {g: _merge(d, data.get(g, {})) for g, d in _DICT_DEFAULTS.items()}
    kw: dict = {
        "name": name,
        "repo": Path(data["repo"]).expanduser(),
        "author": data["author"],
        "gh_user": data["gh_user"],
    }
    for k in ("transport", "scan_minutes", "debounce_min", "max_workers",
              "worker_model", "spec_branch", "self_review_prefix"):
        if k in data:
            kw[k] = data[k]
    if "spec_sources" in data:
        kw["spec_sources"] = tuple(data["spec_sources"])
    if "forbidden" in data:
        kw["forbidden"] = tuple(data["forbidden"])
    return Config(**kw, **groups)


def list_projects(config_dir: Path = DEFAULT_CONFIG_DIR) -> list[str]:
    config_dir = Path(config_dir)
    if not config_dir.is_dir():
        return []
    return sorted(p.name[:-5] for p in config_dir.glob("*.toml")
                  if not p.name.endswith(".overrides.toml"))


def _scalar(v: int | str | list[str]) -> str:
    if isinstance(v, bool):
        raise TypeError("bool not supported")
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return "[" + ", ".join(json.dumps(x, ensure_ascii=False) for x in v) + "]"
    raise TypeError(f"unsupported override value type: {type(v).__name__}")


def _dump(data: dict) -> str:
    lines = [f"{k} = {_scalar(v)}" for k, v in data.items() if not isinstance(v, dict)]
    for table, sub in data.items():
        if isinstance(sub, dict):
            lines.append("")
            lines.append(f"[{table}]")
            lines += [f"{k} = {_scalar(v)}" for k, v in sub.items()]
    return "# generated by autopilot; do not edit by hand\n" + "\n".join(lines).lstrip("\n") + "\n"


_INT_KEYS = frozenset({"limits.thread_rounds", "limits.wakes_per_pr_day", "max_workers",
                       "debounce_min", "notify.remind_after_h"})
_HM = re.compile(r"(\d{2}):(\d{2})")


def _valid_hm(s: str) -> bool:
    m = _HM.fullmatch(s)
    return bool(m) and int(m[1]) < 24 and int(m[2]) < 60


def validate_value(key: str, value) -> None:
    """Per-key type check for EDITABLE_KEYS; raises ValueError with a Vietnamese message."""
    if key not in EDITABLE_KEYS:
        raise KeyError(f"key not editable: {key}")
    if key in _INT_KEYS:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{key} phải là số nguyên dương.")
    elif key == "notify.hours":
        parts = value.split("-") if isinstance(value, str) else []
        if len(parts) != 2 or not all(_valid_hm(x) for x in parts):
            raise ValueError("notify.hours phải có dạng HH:MM-HH:MM.")
    elif key == "notify.digest_at":
        if not isinstance(value, str) or not _valid_hm(value):
            raise ValueError("notify.digest_at phải có dạng HH:MM.")
    elif key == "reply.decision_template":
        if not isinstance(value, str):
            raise ValueError("reply.decision_template phải là chuỗi.")


def set_override(name: str, key: str, value: int | str | list[str],
                 config_dir: Path = DEFAULT_CONFIG_DIR) -> None:
    if key not in EDITABLE_KEYS:
        raise KeyError(f"key not editable: {key}")
    _scalar(value)  # validate type
    validate_value(key, value)
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / f"{name}.overrides.toml"
    lock_fd = os.open(config_dir / f".{name}.overrides.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)  # CLI and web must not race read-modify-write
        data = _read(path) if path.exists() else {}
        if "." in key:
            table, sub = key.split(".", 1)
            data.setdefault(table, {})[sub] = value
        else:
            data[key] = value
        fd, tmp = tempfile.mkstemp(dir=config_dir, prefix=f".{name}.overrides.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(_dump(data))
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    finally:
        os.close(lock_fd)


def get_setting(cfg: Config, key: str):
    head, _, rest = key.partition(".")
    v = getattr(cfg, head, None)
    if rest:
        v = v.get(rest) if isinstance(v, dict) else None
    return v
