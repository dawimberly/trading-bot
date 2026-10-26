"""Paper restart loop: bounded Yahoo scans, heartbeat liveness, supervisor stops."""

from __future__ import annotations

import sys
import time
import types

from modules.heartbeat_watchdog import EXIT_WATCHDOG, touch_engine_heartbeat
from modules.volume_analysis import (
    _fetch_volume_series,
    filter_symbols_by_rvol,
    get_high_rvol_stocks,
)
from run_paper_bot import engine_exit_action


def test_exit_1_stops_after_three_quick_failures():
    assert engine_exit_action(1, autorestart=True, quick_failures=0) == "restart"
    assert engine_exit_action(1, autorestart=True, quick_failures=2) == "restart"
    assert engine_exit_action(1, autorestart=True, quick_failures=3) == "stop"


def test_watchdog_stall_restarts_and_clean_exit_stops():
    assert engine_exit_action(EXIT_WATCHDOG, autorestart=True, quick_failures=5) == "restart"
    assert engine_exit_action(0, autorestart=True, quick_failures=0) == "stop"
    assert engine_exit_action(EXIT_WATCHDOG, autorestart=False, quick_failures=0) == "stop"


def test_filter_keeps_unscanned_names_when_budget_is_spent(monkeypatch):
    monkeypatch.setattr(
        "modules.volume_analysis.config.effective_rvol_scanner_enabled",
        lambda: True,
    )
    calls: list[str] = []

    def _rvol(_data, sym):
        calls.append(sym)
        return 0.1

    monkeypatch.setattr("modules.volume_analysis.calculate_rvol", _rvol)
    out = filter_symbols_by_rvol(["AAA", "BBB", "CCC"], None, deadline_sec=0)
    assert out == ["AAA", "BBB", "CCC"]
    assert calls == []


def test_high_rvol_stops_when_budget_is_spent(monkeypatch):
    monkeypatch.setattr(
        "modules.volume_analysis.nyse_scan_universe",
        lambda data, limit_scan=50: ["AAA", "BBB"],
    )
    calls: list[str] = []

    def _rvol(_data, sym):
        calls.append(sym)
        return 5.0

    monkeypatch.setattr("modules.volume_analysis.calculate_rvol", _rvol)
    assert get_high_rvol_stocks(object(), min_rvol=2.0, limit=5, deadline_sec=0) == []
    assert calls == []


def test_volume_fetch_timeout_returns_none(monkeypatch):
    yf = types.ModuleType("yfinance")

    class _Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, **kwargs):
            time.sleep(1.5)
            return None

    yf.Ticker = _Ticker
    monkeypatch.setitem(sys.modules, "yfinance", yf)
    monkeypatch.setattr("modules.volume_analysis._YF_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr("modules.volume_analysis._cache_get", lambda symbol: None)
    assert _fetch_volume_series("HES", lookback_days=10) is None


def test_touch_heartbeat_refreshes_timestamp_without_dropping_fields(monkeypatch, tmp_path):
    path = tmp_path / "bot_heartbeat.json"
    path.write_text('{"timestamp": "old", "status": "starting", "equity": 10}\n', encoding="utf-8")
    monkeypatch.setattr("config.HEARTBEAT_FILE", str(path))
    monkeypatch.setattr("config.ensure_heartbeat_path_writable", lambda: str(path))
    assert touch_engine_heartbeat() is True
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "running"
    assert payload["equity"] == 10
    assert payload["timestamp"] != "old"


def test_morning_health_flags_stale_starting_heartbeat(monkeypatch, tmp_path):
    import importlib.util
    from datetime import datetime
    from zoneinfo import ZoneInfo

    hb = tmp_path / "bot_heartbeat.json"
    hb.write_text('{"status": "starting"}\n', encoding="utf-8")
    old = time.time() - 2000
    import os

    os.utime(hb, (old, old))
    script = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "scripts"
        / "ops"
        / "morning_bot_health.py"
    )
    spec = importlib.util.spec_from_file_location("morning_bot_health_stall", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    monkeypatch.setattr("modules.portal_bot.bot_pid", lambda *a, **k: 42)
    monkeypatch.setattr(
        "modules.portal_bot._process_cmdline",
        lambda pid: "python run_paper_bot.py",
    )
    monkeypatch.setattr("modules.portal_paths.book_heartbeat_path", lambda *a, **k: hb)
    now = datetime.now(ZoneInfo("America/New_York"))
    ok, detail = mod._book_ok("dawimberly", "alpaca_paper_v2", now)
    assert ok is False
    assert "stale" in detail

    fresh = time.time()
    os.utime(hb, (fresh, fresh))
    ok_fresh, detail_fresh = mod._book_ok("dawimberly", "alpaca_paper_v2", now)
    assert ok_fresh is True
    assert "heartbeat" in detail_fresh


def test_skip_live_scan_does_not_call_yahoo(monkeypatch):
    from modules.bot_health import _scanner_health_snapshot

    monkeypatch.setenv("BOT_HEALTH_SKIP_LIVE_SCAN", "1")
    monkeypatch.setattr(
        "modules.bot_health._market_quiet_tape",
        lambda: False,
    )
    monkeypatch.setattr(
        "modules.bot_health.config.effective_rvol_scanner_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "modules.bot_health.config.effective_orb_enabled",
        lambda: False,
    )
    monkeypatch.setattr(
        "modules.bot_health.config.effective_catalyst_scoring_enabled",
        lambda: False,
    )

    def _boom(*args, **kwargs):
        raise AssertionError("live scan should not run")

    monkeypatch.setattr("modules.pipeline_strategies.load_pipeline_data", _boom)
    snap = _scanner_health_snapshot()
    assert snap["rvol_hits"] == 0
    assert snap["scanners_on"] is True
