"""NYSE session clock for Lab Next.

Close times come from the copied Alpaca calendar where that file has a row.
Older dates come from the research session list and are trading days only:
their 16:00 close is not verified, so those days cannot become complete
sessions. A holiday that is absent from both files is not a session.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]
OPEN = time(9, 30)
DATA = ROOT / "data"


def _load() -> tuple[dict[str, time | None], str]:
    sessions = json.loads((DATA / "exchange_sessions.json").read_text(encoding="utf-8"))
    closes = json.loads((DATA / "exchange_closes.json").read_text(encoding="utf-8"))
    out: dict[str, time | None] = {}
    for day in sessions:
        out[str(day)] = None
    for row in closes:
        hour, minute = str(row["session_close_et"]).split(":")
        out[str(row["date"])] = time(int(hour), int(minute))
    blob = json.dumps({"sessions": sessions, "closes": closes}, sort_keys=True).encode()
    return out, hashlib.sha256(blob).hexdigest()


_DAYS, CALENDAR_HASH = _load()
_ORDERED = tuple(sorted(_DAYS))


def is_session(day: str) -> bool:
    return day in _DAYS


def close_verified(day: str) -> bool:
    return _DAYS.get(day) is not None


def close_clock(day: str) -> time:
    """Verified close, or 16:00 when only the session date is known."""
    if day not in _DAYS:
        raise ValueError(f"{day} is not an exchange session")
    return _DAYS[day] or time(16, 0)


def session_bounds(day: str) -> tuple[datetime, datetime]:
    clock = close_clock(day)
    start = datetime.combine(date.fromisoformat(day), OPEN, tzinfo=ET)
    end = datetime.combine(date.fromisoformat(day), clock, tzinfo=ET)
    return start, end


def bar_starts(day: str) -> list[str]:
    start, end = session_bounds(day)
    last = end - timedelta(minutes=5)
    out = []
    cur = start
    while cur <= last:
        out.append(cur.strftime("%H:%M"))
        cur += timedelta(minutes=5)
    return out


def previous_session(day: str) -> str:
    for item in reversed(_ORDERED):
        if item < day:
            return item
    raise ValueError(f"no session before {day}")


def next_session(day: str) -> str:
    for item in _ORDERED:
        if item > day:
            return item
    raise ValueError(f"no session after {day}")


def prior_bar(dt: datetime) -> datetime:
    dt = dt.astimezone(ET).replace(second=0, microsecond=0)
    day = dt.date().isoformat()
    start, _end = session_bounds(day)
    if dt > start:
        return dt - timedelta(minutes=5)
    if dt == start:
        prev = previous_session(day)
        _pstart, pend = session_bounds(prev)
        return pend - timedelta(minutes=5)
    raise ValueError("timestamp is not a session bar start")


def first_open_after(moment: datetime) -> datetime:
    """First regular-session bar start strictly after ``moment``.

    This is the earliest open a forward decision is allowed to use. An open
    that is already printing at ``moment`` is not eligible.
    """
    moment = moment.astimezone(ET)
    day = moment.date().isoformat()
    if day in _DAYS:
        cur, end = session_bounds(day)
        last = end - timedelta(minutes=5)
        while cur <= last:
            if cur > moment:
                return cur
            cur += timedelta(minutes=5)
        day = next_session(day)
    else:
        for item in _ORDERED:
            if item > day:
                day = item
                break
        else:
            raise ValueError(f"no session after {moment.date().isoformat()}")
    return datetime.combine(date.fromisoformat(day), OPEN, tzinfo=ET)


def next_bar(dt: datetime) -> datetime:
    dt = dt.astimezone(ET).replace(second=0, microsecond=0)
    day = dt.date().isoformat()
    _start, end = session_bounds(day)
    nxt = dt + timedelta(minutes=5)
    if nxt < end:
        return nxt
    following = next_session(day)
    return datetime.combine(date.fromisoformat(following), OPEN, tzinfo=ET)


def hold_sessions(entry_day: str, today: str) -> int:
    """Exchange sessions strictly after the entry date, through today."""
    return sum(1 for day in _ORDERED if entry_day < day <= today)


def sessions_after(day: str, count: int) -> list[str]:
    found = [item for item in _ORDERED if item > day]
    return found[:count]
