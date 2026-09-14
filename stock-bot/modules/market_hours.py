"""US equity session helpers via Alpaca clock."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from modules.alpaca_client import call_with_retry

ET = ZoneInfo("America/New_York")


def is_equity_market_open(trading_client):
    """True during regular US equity hours (Alpaca clock)."""
    try:
        clock = call_with_retry(trading_client.get_clock, op_name="get_clock")
        return bool(clock.is_open)
    except Exception as e:
        print(f"Market clock unavailable ({e}); treating equity session as closed")
        return False


def nyse_rth_status(now: datetime | None = None) -> dict:
    """Regular-hours status without Alpaca: 9:30–16:00 ET, Mon–Fri.

    Ignores exchange holidays. Used as a dashboard fallback when the
    heartbeat has no scan_schedule.
    """
    now_et = now or datetime.now(ET)
    if now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=ET)
    else:
        now_et = now_et.astimezone(ET)
    weekday = now_et.weekday()
    open_t = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    if weekday >= 5:
        days = 7 - weekday
        nxt = (now_et + timedelta(days=days)).replace(
            hour=9, minute=30, second=0, microsecond=0
        )
        return {"is_open": False, "next_open": nxt, "session_close": None}
    if open_t <= now_et < close_t:
        return {"is_open": True, "next_open": open_t, "session_close": close_t}
    if now_et < open_t:
        return {"is_open": False, "next_open": open_t, "session_close": close_t}
    days = 3 if weekday == 4 else 1
    nxt = (now_et + timedelta(days=days)).replace(
        hour=9, minute=30, second=0, microsecond=0
    )
    return {"is_open": False, "next_open": nxt, "session_close": close_t}
