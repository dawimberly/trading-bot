"""NYSE session calendar for finalized daily bars.

Full holidays follow the exchange's observed-weekday rules. Early closes are
1:00 p.m. ET. This module is not used by the trading loop.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
_REGULAR_CLOSE = (16, 0)
_EARLY_CLOSE = (13, 0)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    shift = (weekday - first.weekday()) % 7
    return first + timedelta(days=shift + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    while cursor.weekday() != weekday:
        cursor -= timedelta(days=1)
    return cursor


def _easter(year: int) -> date:
    """Gregorian Easter Sunday."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observe(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def nyse_holidays(year: int) -> set[date]:
    """Dates the NYSE is fully closed, including weekend observance."""
    closed = {
        _observe(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observe(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observe(date(year, 12, 25)),
    }
    if year >= 2022:
        closed.add(_observe(date(year, 6, 19)))
    new_year = date(year + 1, 1, 1)
    if new_year.weekday() == 5:
        closed.add(date(year, 12, 31))
    return closed


def nyse_early_closes(year: int) -> set[date]:
    """Weekday sessions that close at 1:00 p.m. ET."""
    closed = nyse_holidays(year) | nyse_holidays(year - 1) | nyse_holidays(year + 1)
    early: set[date] = set()
    black_friday = _nth_weekday(year, 11, 3, 4) + timedelta(days=1)
    if black_friday.weekday() < 5 and black_friday not in closed:
        early.add(black_friday)
    july4 = date(year, 7, 4)
    if july4.weekday() < 5:
        eve = july4 - timedelta(days=1)
        if eve.weekday() < 5 and eve not in closed:
            early.add(eve)
    christmas = date(year, 12, 25)
    if christmas.weekday() == 6:
        eve = date(year, 12, 23)
    elif christmas.weekday() < 5:
        eve = date(year, 12, 24)
    else:
        eve = None
    if eve is not None and eve.weekday() < 5 and eve not in closed:
        early.add(eve)
    return early


def session_close(day: date) -> datetime | None:
    """ET close of a real session. None on weekends and full holidays."""
    if day.weekday() >= 5 or day in nyse_holidays(day.year):
        return None
    hour, minute = _EARLY_CLOSE if day in nyse_early_closes(day.year) else _REGULAR_CLOSE
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)


def is_finalized_session(day: date, as_of: datetime) -> bool:
    """True after that session's close. Early-close days finalize at 1:00 p.m. ET."""
    close = session_close(day)
    if close is None:
        return False
    clock = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=ET)
    return clock.astimezone(ET) >= close
