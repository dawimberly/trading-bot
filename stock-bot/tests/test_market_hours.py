"""NYSE regular-hours clock used by the dashboard MARKET tile."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from modules.market_hours import nyse_rth_status

ET = ZoneInfo("America/New_York")


def test_weekday_midday_is_open():
    now = datetime(2026, 9, 14, 14, 12, tzinfo=ET)
    status = nyse_rth_status(now)
    assert status["is_open"] is True
    assert status["session_close"].hour == 16


def test_weekday_preopen_counts_to_930():
    now = datetime(2026, 9, 14, 8, 0, tzinfo=ET)
    status = nyse_rth_status(now)
    assert status["is_open"] is False
    assert status["next_open"].hour == 9
    assert status["next_open"].minute == 30
    assert status["next_open"].date() == now.date()


def test_friday_after_close_skips_weekend():
    now = datetime(2026, 9, 11, 17, 0, tzinfo=ET)
    status = nyse_rth_status(now)
    assert status["is_open"] is False
    nxt = status["next_open"]
    assert nxt.weekday() == 0
    assert nxt.hour == 9 and nxt.minute == 30


def test_saturday_points_at_monday():
    now = datetime(2026, 9, 12, 12, 0, tzinfo=ET)
    status = nyse_rth_status(now)
    assert status["is_open"] is False
    assert status["next_open"].weekday() == 0
