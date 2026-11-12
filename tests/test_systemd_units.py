import configparser
from pathlib import Path

UNITS = Path(__file__).resolve().parent.parent / "systemd"


def load(name):
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read(UNITS / name, encoding="utf-8")
    return cp


def test_timer_interval_is_five_minutes():
    t = load("autopilot.timer")
    assert t["Timer"]["OnBootSec"] == "2min"
    assert t["Timer"]["OnUnitActiveSec"] == "5min"
    assert t["Timer"]["Persistent"] == "true"
    assert t["Install"]["WantedBy"] == "timers.target"


def test_service_runs_scan_all():
    s = load("autopilot.service")
    assert s["Service"]["Type"] == "oneshot"
    assert s["Service"]["ExecStart"] == "%h/.claude/scripts/autopilot/.venv/bin/autopilot scan --all"


def test_web_service_restarts_on_failure():
    w = load("autopilot-web.service")
    assert w["Service"]["ExecStart"].endswith("/.venv/bin/autopilot web")
    assert w["Service"]["Restart"] == "on-failure"
    assert w["Install"]["WantedBy"] == "default.target"
