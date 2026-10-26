"""Source dashboard is the default; frozen EXE is opt-in."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.dashboard_launcher import should_use_frozen_monitor


def test_frozen_monitor_opt_in(monkeypatch):
    monkeypatch.delenv("DASHBOARD_USE_FROZEN", raising=False)
    assert should_use_frozen_monitor() is False
    monkeypatch.setenv("DASHBOARD_USE_FROZEN", "true")
    assert should_use_frozen_monitor() is True
    monkeypatch.setenv("DASHBOARD_USE_FROZEN", "1")
    assert should_use_frozen_monitor() is True
    monkeypatch.setenv("DASHBOARD_USE_FROZEN", "no")
    assert should_use_frozen_monitor() is False


def test_launch_monitor_bat_prefers_source():
    bat = Path(__file__).resolve().parents[1] / "launch_monitor.bat"
    text = bat.read_text(encoding="utf-8")
    lower = text.lower()
    src = lower.find("dashboard_app.py")
    exe = lower.find("pythontradingmonitor.exe")
    assert src != -1, "launch_monitor.bat should start the source monitor"
    assert exe == -1 or src < exe
    assert "run_hidden.vbs" in lower

