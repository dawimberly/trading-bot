"""RTH uptime: open_ok_0935 + missed_rth_minutes (sleep/WiFi gaps)."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from modules.safe_io import read_json_file, write_json_atomic

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")
_STATE = Path(__file__).resolve().parents[1] / "data" / "session_uptime.json"
_RTH_OPEN = (9, 30)
_OPEN_OK_DEADLINE = (9, 40)
_RTH_CLOSE = (16, 0)


def _today_et(now: datetime | None = None) -> str:
    now = now or datetime.now(_ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_ET)
    return now.astimezone(_ET).strftime("%Y-%m-%d")


def _load() -> dict[str, Any]:
    data = read_json_file(_STATE)
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, Any]) -> None:
    try:
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(_STATE, data)
    except Exception:
        logger.debug("session_uptime save failed", exc_info=True)


def _in_rth(now: datetime) -> bool:
    et = now.astimezone(_ET)
    if et.weekday() >= 5:
        return False
    mins = et.hour * 60 + et.minute
    open_m = _RTH_OPEN[0] * 60 + _RTH_OPEN[1]
    close_m = _RTH_CLOSE[0] * 60 + _RTH_CLOSE[1]
    return open_m <= mins < close_m


def note_gap(gap_sec: float, *, now: datetime | None = None) -> dict[str, Any]:
    """Accumulate missed RTH minutes from a cycle gap (sleep/network/stall)."""
    now = now or datetime.now(_ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_ET)
    state = _load()
    today = _today_et(now)
    if state.get("date") != today:
        state = {
            "date": today,
            "missed_rth_minutes": 0,
            "open_ok_0935": None,
            "first_rth_cycle_et": "",
            "gaps_today": 0,
        }
    if _in_rth(now) and gap_sec > 0:
        add = int(max(0.0, float(gap_sec)) // 60)
        # Cap single gap to remaining session (~6.5h)
        add = min(add, 390)
        state["missed_rth_minutes"] = int(state.get("missed_rth_minutes") or 0) + add
        state["gaps_today"] = int(state.get("gaps_today") or 0) + 1
        state["last_gap_sec"] = int(gap_sec)
        state["last_gap_at"] = now.astimezone(_ET).strftime("%Y-%m-%d %H:%M:%S")
        _save(state)
    return snapshot(state)


def pulse(*, market_open: bool, now: datetime | None = None) -> dict[str, Any]:
    """Call once per successful cycle. Sets open_ok_0935 on first RTH sighting."""
    now = now or datetime.now(_ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_ET)
    et = now.astimezone(_ET)
    today = _today_et(et)
    state = _load()
    if state.get("date") != today:
        state = {
            "date": today,
            "missed_rth_minutes": 0,
            "open_ok_0935": None,
            "first_rth_cycle_et": "",
            "gaps_today": 0,
        }
    if market_open and _in_rth(et):
        if not state.get("first_rth_cycle_et"):
            state["first_rth_cycle_et"] = et.strftime("%Y-%m-%d %H:%M:%S")
            deadline_m = _OPEN_OK_DEADLINE[0] * 60 + _OPEN_OK_DEADLINE[1]
            now_m = et.hour * 60 + et.minute
            state["open_ok_0935"] = bool(now_m <= deadline_m)
        _save(state)
    return snapshot(state)


def snapshot(state: dict[str, Any] | None = None) -> dict[str, Any]:
    state = state if state is not None else _load()
    today = _today_et()
    if state.get("date") != today:
        return {
            "date": today,
            "missed_rth_minutes": 0,
            "open_ok_0935": None,
            "first_rth_cycle_et": "",
            "gaps_today": 0,
        }
    return {
        "date": state.get("date") or today,
        "missed_rth_minutes": int(state.get("missed_rth_minutes") or 0),
        "open_ok_0935": state.get("open_ok_0935"),
        "first_rth_cycle_et": state.get("first_rth_cycle_et") or "",
        "gaps_today": int(state.get("gaps_today") or 0),
    }
