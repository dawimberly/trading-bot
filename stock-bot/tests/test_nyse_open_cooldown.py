"""Open cooldown is 9:30–10:15 America/New_York, including Central naive clocks."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from modules.pipeline_strategies import _nyse_open_cooldown_active
from modules.scan_schedule import _to_et

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")


def test_et_open_window_is_forty_five_minutes():
    assert _nyse_open_cooldown_active(datetime(2026, 9, 18, 9, 30, tzinfo=ET)) is True
    assert _nyse_open_cooldown_active(datetime(2026, 9, 18, 9, 43, tzinfo=ET)) is True
    assert _nyse_open_cooldown_active(datetime(2026, 9, 18, 10, 0, tzinfo=ET)) is True
    assert _nyse_open_cooldown_active(datetime(2026, 9, 18, 10, 15, tzinfo=ET)) is True
    assert _nyse_open_cooldown_active(datetime(2026, 9, 18, 10, 16, tzinfo=ET)) is False


def test_central_aware_open_maps_to_et_cooldown():
    # 8:43 CDT = 9:43 EDT — inside 45-minute freeze.
    assert _nyse_open_cooldown_active(datetime(2026, 9, 18, 8, 43, tzinfo=CT)) is True
    # 9:03 CDT = 10:03 EDT — still inside 45 minutes (not 30).
    assert _nyse_open_cooldown_active(datetime(2026, 9, 18, 9, 3, tzinfo=CT)) is True
    et = datetime(2026, 9, 18, 9, 3, tzinfo=CT).astimezone(ET).time()
    assert et.hour == 10 and et.minute == 3


def test_naive_local_central_is_not_treated_as_et(monkeypatch):
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 18, 8, 43, tzinfo=CT)

    monkeypatch.setattr("modules.market_hours.datetime", _DT)
    naive = datetime(2026, 9, 18, 8, 43, 0)
    assert _nyse_open_cooldown_active(naive) is True


def test_scan_schedule_naive_central_is_local_not_utc(monkeypatch):
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 18, 8, 43, tzinfo=CT)

    monkeypatch.setattr("modules.market_hours.datetime", _DT)
    et = _to_et(datetime(2026, 9, 18, 8, 43, 0))
    assert et.hour == 9 and et.minute == 43


def test_today_medium_tape_forty_five_vs_sixty():
    """Sep 18 local CDT stamps → ET. Wired 45 min vs a 1-hour freeze (not enabled)."""
    local = [
        ("KTOS", datetime(2026, 9, 18, 8, 43)),
        ("PLTR", datetime(2026, 9, 18, 8, 43)),
        ("SNOW", datetime(2026, 9, 18, 8, 43)),
        ("PFE", datetime(2026, 9, 18, 8, 44)),
        ("LMT", datetime(2026, 9, 18, 8, 53)),
        ("BB", datetime(2026, 9, 18, 8, 54)),
        ("ARM", datetime(2026, 9, 18, 9, 3)),
        ("NVDA", datetime(2026, 9, 18, 9, 17)),
        ("AMD", datetime(2026, 9, 18, 9, 17)),
        ("MU", datetime(2026, 9, 18, 9, 26)),
        ("AMZN", datetime(2026, 9, 18, 10, 6)),
    ]
    blocked_45 = []
    blocked_60 = []
    for sym, naive in local:
        et = naive.replace(tzinfo=CT).astimezone(ET)
        if _nyse_open_cooldown_active(et):
            blocked_45.append(sym)
        start = et.replace(hour=9, minute=30, second=0, microsecond=0)
        if start <= et <= start + timedelta(minutes=60):
            blocked_60.append(sym)
    assert blocked_45 == [
        "KTOS",
        "PLTR",
        "SNOW",
        "PFE",
        "LMT",
        "BB",
        "ARM",
    ]
    assert blocked_60 == [
        "KTOS",
        "PLTR",
        "SNOW",
        "PFE",
        "LMT",
        "BB",
        "ARM",
        "NVDA",
        "AMD",
        "MU",
    ]
