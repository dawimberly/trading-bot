"""Equity scan windows: crypto-only overnight, SPY/NYSE around the US open."""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

import config
from modules.alpaca_client import call_with_retry
from modules.market_hours import is_equity_market_open

ET = ZoneInfo("America/New_York")


def _aware(dt: datetime.datetime) -> datetime.datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _to_et(dt: datetime.datetime) -> datetime.datetime:
    return _aware(dt).astimezone(ET)


def _get_clock(trading_client):
    """Alpaca clock with shared retry / TRANSIENT_NETWORK classification."""
    return call_with_retry(trading_client.get_clock, op_name="get_clock")


def resolve_session_bounds(
    trading_client,
    now: datetime.datetime | None = None,
) -> tuple[datetime.datetime, datetime.datetime]:
    """Return (session_open, session_close) in ET for the active or next session."""
    clock = _get_clock(trading_client)
    now_et = _to_et(now or clock.timestamp)
    next_open_et = _to_et(clock.next_open)
    next_close_et = _to_et(clock.next_close)

    if clock.is_open:
        session_close = next_close_et
        session_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        if session_open > now_et:
            session_open -= datetime.timedelta(days=1)
        return session_open, session_close

    if now_et < next_open_et:
        return next_open_et, next_close_et
    return next_open_et, next_close_et


def equity_scan_state(
    trading_client,
    now: datetime.datetime | None = None,
) -> dict:
    """
    Schedule for run_all.py.

    - crypto_only: overnight — crypto scans only
    - equity_prep: open-5m .. open+5m — refresh equities + regime, no SPY/NYSE orders
    - equity_scans: open+5m .. close — full SPY/NYSE strategy passes
    """
    market_open = is_equity_market_open(trading_client)
    if not config.SCAN_SCHEDULE_ENABLED:
        return {
            "enabled": False,
            "phase": "session" if market_open else "overnight",
            "crypto_only": not market_open,
            "equity_prep": market_open,
            "equity_scans": market_open,
            "market_open": market_open,
            "session_open": None,
            "session_close": None,
            "orders_start": None,
        }

    clock = _get_clock(trading_client)
    now_et = _to_et(now or clock.timestamp)
    session_open, session_close = resolve_session_bounds(trading_client, now_et)

    before = config.EQUITY_SCAN_BEFORE_OPEN_MIN
    after_open = config.EQUITY_SCAN_AFTER_OPEN_MIN

    prep_start = session_open - datetime.timedelta(minutes=before)
    prep_end = session_open + datetime.timedelta(minutes=after_open)
    orders_start = prep_end
    scan_end = session_close

    equity_prep = prep_start <= now_et < prep_end
    equity_scans = orders_start <= now_et <= scan_end and market_open
    crypto_only = not equity_prep and not equity_scans

    if equity_scans:
        phase = "session"
    elif equity_prep:
        phase = "open_prep"
    else:
        phase = "overnight"

    return {
        "enabled": True,
        "phase": phase,
        "crypto_only": crypto_only,
        "equity_prep": equity_prep,
        "equity_scans": equity_scans,
        "market_open": market_open,
        "session_open": session_open.isoformat(),
        "session_close": session_close.isoformat(),
        "orders_start": orders_start.isoformat(),
    }


def cycle_sleep_seconds(state: dict | None) -> int:
    """Seconds to sleep before the next loop iteration."""
    if state and state.get("crypto_only"):
        return config.CRYPTO_ONLY_CYCLE_INTERVAL_SEC
    return config.CYCLE_INTERVAL_SEC


def format_scan_schedule_line(state: dict) -> str:
    if not state.get("enabled"):
        return f"Scan schedule: off | equity session {'OPEN' if state.get('market_open') else 'CLOSED'}"
    phase = state.get("phase", "?")
    if phase == "overnight":
        return "Scan schedule: overnight (crypto only)"
    if phase == "open_prep":
        return (
            f"Scan schedule: open prep (equity refresh; SPY/NYSE start "
            f"{config.EQUITY_SCAN_AFTER_OPEN_MIN}m after bell)"
        )
    return "Scan schedule: equity session (SPY + NYSE active)"
