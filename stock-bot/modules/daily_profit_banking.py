"""Daily Profit Banking for Realistic Research v1.5+.

Track day-open equity vs current (realized + mark-to-market). When the
session gain reaches ``DAILY_BANK_THRESHOLD_PCT`` (default 0.8%), bank the
win by cutting new-risk sizing to ``DAILY_BANK_RISK_MULT`` and nudging the
VTI core higher for the rest of the day.

Reset each trading day, ``DAILY_BANK_RESET_MINUTES_AFTER_OPEN`` minutes
after the equity open (default 30). Paper / Realistic Research default ON;
live stays off unless ``DAILY_BANK_LIVE_ENABLED``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)

_STATE_PATH_ATTR = "DAILY_BANK_STATE_PATH"


@dataclass
class DailyBankState:
    session_date: str = ""  # YYYY-MM-DD ET
    open_equity: float = 0.0
    banked: bool = False
    banked_at: str = ""
    gain_pct: float = 0.0  # percent points vs open (0.8 = 0.8%)
    locked_gain_pct: float = 0.0
    risk_mult: float = 1.0
    reset_armed: bool = False  # True once past reset window for the day


_state = DailyBankState()
_bank_days = 0  # backtest / session counter


def _now_et(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(ET)
    if now.tzinfo is None:
        return now.replace(tzinfo=ET)
    return now.astimezone(ET)


def _session_date(now: datetime | None = None, bar_date: date | None = None) -> date:
    if bar_date is not None:
        return bar_date
    return _now_et(now).date()


def _reset_minutes() -> int:
    return max(0, int(getattr(config, "DAILY_BANK_RESET_MINUTES_AFTER_OPEN", 30)))


def _threshold_pct() -> float:
    """Threshold in percent points (0.8 => 0.8%)."""
    return max(0.0, float(getattr(config, "DAILY_BANK_THRESHOLD_PCT", 0.8)))


def _bank_risk_mult() -> float:
    return max(0.05, min(1.0, float(getattr(config, "DAILY_BANK_RISK_MULT", 0.4))))


def _vti_boost_pp() -> float:
    return max(0.0, float(getattr(config, "DAILY_BANK_VTI_BOOST_PP", 10.0)))


def _past_reset_window(now: datetime | None, *, bar_mode: bool) -> bool:
    """True when banking may evaluate / stay banked for the session."""
    if bar_mode:
        # Daily bar ≈ full session after the open reset window.
        return True
    et = _now_et(now)
    open_dt = datetime.combine(et.date(), RTH_OPEN, tzinfo=ET)
    return et >= open_dt + timedelta(minutes=_reset_minutes())


def reset_daily_bank_state() -> None:
    """Clear in-memory state (compare legs / tests)."""
    global _state, _bank_days
    _state = DailyBankState()
    _bank_days = 0


def get_daily_bank_state() -> DailyBankState:
    return DailyBankState(**asdict(_state))


def bank_day_count() -> int:
    return int(_bank_days)


def is_banked() -> bool:
    return bool(_state.banked and config.effective_daily_bank_enabled())


def daily_bank_risk_multiplier() -> float:
    if not config.effective_daily_bank_enabled():
        return 1.0
    if _state.banked:
        return float(_state.risk_mult or _bank_risk_mult())
    return 1.0


def daily_bank_vti_boost_pp() -> float:
    """Extra VTI core percentage points when banked (0 when inactive)."""
    if not is_banked():
        return 0.0
    return _vti_boost_pp()


def format_daily_bank_banner() -> str | None:
    if not getattr(config, "DAILY_BANK_ENABLED", False):
        return None
    # Banner when paper aggressive / research may use banking (risk still gated).
    if not (
        config.effective_daily_bank_enabled()
        or getattr(config, "PAPER_AGGRESSIVE_ENABLED", False)
        or getattr(config, "PAPER_TRADING", False)
    ):
        return None
    thr = _threshold_pct()
    if _state.banked:
        return (
            f"Daily Profit Banking: ON ({thr:g}% threshold) "
            f"[BANKED | locked +{_state.locked_gain_pct:.2f}%]"
        )
    return f"Daily Profit Banking: ON ({thr:g}% threshold)"


def heartbeat_daily_bank_payload() -> dict[str, Any] | None:
    if not config.effective_daily_bank_enabled():
        return None
    return {
        "enabled": True,
        "banked": bool(_state.banked),
        "threshold_pct": _threshold_pct(),
        "risk_mult": daily_bank_risk_multiplier(),
        "gain_pct": round(float(_state.gain_pct), 4),
        "locked_gain_pct": round(float(_state.locked_gain_pct), 4),
        "open_equity": round(float(_state.open_equity), 2),
        "session_date": _state.session_date,
        "banked_at": _state.banked_at,
        "vti_boost_pp": daily_bank_vti_boost_pp(),
    }


def update_daily_bank(
    equity: float,
    *,
    now: datetime | None = None,
    bar_date: date | None = None,
    day_open_equity: float | None = None,
    force_reset: bool = False,
) -> DailyBankState:
    """Update banking state from current equity.

    *bar_date*: when set (backtests), treat each calendar day as one session.
    *day_open_equity*: prior close / session open mark (required for daily bars
    so today's MTM can trigger banking; live path uses wall-clock reset instead).
    """
    global _state, _bank_days

    if not config.effective_daily_bank_enabled():
        return get_daily_bank_state()

    eq = float(equity or 0.0)
    if eq <= 0:
        return get_daily_bank_state()

    bar_mode = bar_date is not None
    sess = _session_date(now, bar_date)
    sess_s = sess.isoformat()
    past_reset = _past_reset_window(now, bar_mode=bar_mode)

    # New session: capture day-open equity after the reset window (or immediately
    # on daily bars / force_reset).
    if force_reset or _state.session_date != sess_s:
        open_eq = float(day_open_equity) if day_open_equity and day_open_equity > 0 else eq
        if bar_mode or past_reset or force_reset or not _state.session_date:
            _state = DailyBankState(
                session_date=sess_s,
                open_equity=open_eq,
                banked=False,
                gain_pct=0.0,
                locked_gain_pct=0.0,
                risk_mult=1.0,
                reset_armed=True,
            )
            # Fall through to evaluate gain on the same call (daily close MTM).
        else:
            # Before 10:00 ET: wait to arm open equity.
            _state.session_date = sess_s
            _state.reset_armed = False
            return get_daily_bank_state()

    if not _state.reset_armed:
        if past_reset:
            open_eq = float(day_open_equity) if day_open_equity and day_open_equity > 0 else eq
            _state.open_equity = open_eq
            _state.banked = False
            _state.gain_pct = 0.0
            _state.locked_gain_pct = 0.0
            _state.risk_mult = 1.0
            _state.banked_at = ""
            _state.reset_armed = True
        else:
            return get_daily_bank_state()

    open_eq = float(_state.open_equity or 0.0)
    if open_eq <= 0:
        _state.open_equity = eq
        return get_daily_bank_state()

    gain_pct = (eq / open_eq - 1.0) * 100.0
    _state.gain_pct = gain_pct

    if not _state.banked and gain_pct >= _threshold_pct():
        _state.banked = True
        _state.locked_gain_pct = gain_pct
        _state.risk_mult = _bank_risk_mult()
        _state.banked_at = _now_et(now).isoformat(timespec="seconds")
        _bank_days += 1
        logger.info(
            "Daily profit banked: +%.2f%% vs open (threshold %.2f%%) → risk x%.2f",
            gain_pct,
            _threshold_pct(),
            _state.risk_mult,
        )
    elif _state.banked:
        _state.locked_gain_pct = max(_state.locked_gain_pct, gain_pct)

    return get_daily_bank_state()
