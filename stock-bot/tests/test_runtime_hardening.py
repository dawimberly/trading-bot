"""Allocator room, dotted SQL tickers, cycle faults, stale health."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from modules import bot_health, data_loader, error_autofix, error_watcher


def test_daily_table_name_sanitizes_brk_b():
    assert data_loader.daily_table_name("BRK.B") == "BRK_B_daily"
    assert data_loader.safe_sql_table("BRK.B_daily") == "BRK_B_daily"
    assert data_loader.safe_sql_table("BRK_B_daily") == "BRK_B_daily"


def test_fund_allocation_never_raises_and_fits_100():
    alloc = config.fund_allocation_pct()
    assert abs(sum(alloc.values()) - 1.0) <= 1e-4 or alloc["cash_buffer"] == 0.0
    long_plus_core = (
        alloc["vti_core"]
        + alloc["spy"]
        + alloc["crypto"]
        + alloc["nyse"]
        + alloc["stat_arb"]
        + alloc["metal"]
    )
    assert long_plus_core <= 1.0 + 1e-6
    assert config.active_sleeve_scale() >= 0.0


def test_classify_invalid_sql_table_name():
    msg = "Invalid SQL table name: 'BRK.B'"
    assert error_watcher.classify_error_class(msg) == "data_symbol"
    plan = error_autofix.deterministic_plan(msg)
    assert plan.action == "skip_cycle"
    assert plan.error_class == "data_symbol"


def test_stale_heartbeat_fails_health():
    old = (dt.datetime.now() - dt.timedelta(minutes=40)).isoformat()
    result = bot_health.calculate_health_score(
        hb={"timestamp": old, "regime": "RHYME_D"},
        regime="RHYME_D",
    )
    assert result["score"] <= 49
    assert result["grade"] == "Needs attention"
    assert any("STALE" in n for n in result["notes"])
