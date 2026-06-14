"""Scheduled US macro release calendar (hardcoded; no per-cycle scraping)."""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

import config

ET = ZoneInfo("America/New_York")

# High-impact releases through Dec 2026 (BLS/Fed published schedules + typical release times ET).
# release_hour_et: 8.5 = 08:30, 10.0 = 10:00, 14.0 = 14:00 (FOMC statement)
MACRO_EVENTS: tuple[dict, ...] = (
    # --- June 2026 ---
    {"date": "2026-06-05", "name": "NFP", "release_hour_et": 8.5},
    {"date": "2026-06-11", "name": "CPI", "release_hour_et": 8.5},
    {"date": "2026-06-12", "name": "PPI", "release_hour_et": 8.5},
    {"date": "2026-06-18", "name": "FOMC", "release_hour_et": 14.0},
    {"date": "2026-06-25", "name": "GDP", "release_hour_et": 8.5},
    # --- July 2026 ---
    {"date": "2026-07-03", "name": "NFP", "release_hour_et": 8.5},
    {"date": "2026-07-15", "name": "CPI", "release_hour_et": 8.5},
    {"date": "2026-07-16", "name": "PPI", "release_hour_et": 8.5},
    {"date": "2026-07-30", "name": "GDP", "release_hour_et": 8.5},
    {"date": "2026-07-30", "name": "FOMC", "release_hour_et": 14.0},
    # --- August 2026 ---
    {"date": "2026-08-07", "name": "NFP", "release_hour_et": 8.5},
    {"date": "2026-08-12", "name": "CPI", "release_hour_et": 8.5},
    {"date": "2026-08-13", "name": "PPI", "release_hour_et": 8.5},
    # --- September 2026 ---
    {"date": "2026-09-04", "name": "NFP", "release_hour_et": 8.5},
    {"date": "2026-09-10", "name": "CPI", "release_hour_et": 8.5},
    {"date": "2026-09-11", "name": "PPI", "release_hour_et": 8.5},
    {"date": "2026-09-17", "name": "FOMC", "release_hour_et": 14.0},
    {"date": "2026-09-25", "name": "GDP", "release_hour_et": 8.5},
    # --- October 2026 ---
    {"date": "2026-10-02", "name": "NFP", "release_hour_et": 8.5},
    {"date": "2026-10-14", "name": "CPI", "release_hour_et": 8.5},
    {"date": "2026-10-15", "name": "PPI", "release_hour_et": 8.5},
    {"date": "2026-10-29", "name": "GDP", "release_hour_et": 8.5},
    # --- November 2026 ---
    {"date": "2026-11-06", "name": "NFP", "release_hour_et": 8.5},
    {"date": "2026-11-12", "name": "CPI", "release_hour_et": 8.5},
    {"date": "2026-11-13", "name": "PPI", "release_hour_et": 8.5},
    {"date": "2026-11-05", "name": "FOMC", "release_hour_et": 14.0},
    # --- December 2026 ---
    {"date": "2026-12-04", "name": "NFP", "release_hour_et": 8.5},
    {"date": "2026-12-10", "name": "CPI", "release_hour_et": 8.5},
    {"date": "2026-12-11", "name": "PPI", "release_hour_et": 8.5},
    {"date": "2026-12-17", "name": "FOMC", "release_hour_et": 14.0},
    {"date": "2026-12-22", "name": "GDP", "release_hour_et": 8.5},
)


def _event_datetime(event: dict) -> datetime.datetime:
    hour = float(event.get("release_hour_et", 8.5))
    hour_int = int(hour)
    minute = int(round((hour - hour_int) * 60))
    day = datetime.date.fromisoformat(event["date"])
    return datetime.datetime(day.year, day.month, day.day, hour_int, minute, tzinfo=ET)


def _now_et() -> datetime.datetime:
    return datetime.datetime.now(ET)


def get_upcoming_macro_risk(within_days: int = 2) -> list[dict]:
    """Events releasing within `within_days` calendar days (ET), including today."""
    now = _now_et()
    horizon = now + datetime.timedelta(days=within_days)
    out: list[dict] = []
    for event in MACRO_EVENTS:
        evt_dt = _event_datetime(event)
        if evt_dt < now - datetime.timedelta(hours=4):
            continue
        if evt_dt.date() > horizon.date():
            continue
        hours_until = (evt_dt - now).total_seconds() / 3600.0
        out.append(
            {
                "name": event["name"],
                "date": event["date"],
                "release_et": evt_dt.isoformat(),
                "hours_until": round(hours_until, 2),
            }
        )
    return sorted(out, key=lambda x: x["hours_until"])


def is_macro_event_window(hours_before: float | None = None) -> tuple[bool, dict | None]:
    """
    True when a major release is same-day (ET) or within `hours_before` of release.
    Returns (active, event_detail).
    """
    if not config.MACRO_EVENT_GUARD_ENABLED:
        return False, None

    hours_before = (
        float(hours_before)
        if hours_before is not None
        else float(config.MACRO_EVENT_HOURS_BEFORE)
    )
    now = _now_et()
    best: dict | None = None
    best_hours = None

    for event in MACRO_EVENTS:
        evt_dt = _event_datetime(event)
        hours_until = (evt_dt - now).total_seconds() / 3600.0
        same_day = now.date() == evt_dt.date()
        # Guard window: same calendar day, or release still ahead within hours_before
        if same_day or (0 <= hours_until <= hours_before):
            if best is None or abs(hours_until) < abs(best_hours or 9999):
                best = {
                    "name": event["name"],
                    "date": event["date"],
                    "release_et": evt_dt.isoformat(),
                    "hours_until": round(hours_until, 2),
                    "same_day": same_day,
                    "sizing_scale": config.MACRO_EVENT_SIZING_SCALE,
                }
                best_hours = hours_until

    return best is not None, best


def next_macro_event() -> dict | None:
    """Next scheduled release after now (ET), for dashboard / heartbeat."""
    now = _now_et()
    upcoming = []
    for event in MACRO_EVENTS:
        evt_dt = _event_datetime(event)
        if evt_dt >= now - datetime.timedelta(hours=4):
            upcoming.append(
                {
                    "name": event["name"],
                    "date": event["date"],
                    "release_et": evt_dt.isoformat(),
                    "hours_until": round((evt_dt - now).total_seconds() / 3600.0, 2),
                }
            )
    if not upcoming:
        return None
    return min(upcoming, key=lambda x: x["hours_until"])


def macro_event_context() -> dict:
    """Single call site for run_all: active guard, scale, next event."""
    active, detail = is_macro_event_window()
    return {
        "active": active,
        "event": detail,
        "sizing_scale": config.MACRO_EVENT_SIZING_SCALE if active else 1.0,
        "next": next_macro_event(),
        "upcoming_2d": get_upcoming_macro_risk(within_days=2),
    }
