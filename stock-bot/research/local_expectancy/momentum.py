"""12-to-7 momentum score. Sort key only. No lookahead past the decision close."""

from __future__ import annotations

import bisect
import calendar


def shift_months(day: str, months: int) -> str:
    """Calendar date `months` before `day`. Day-of-month clamps to the target month."""
    year, month, dom = (int(part) for part in day.split("-"))
    month_index = year * 12 + (month - 1) - months
    year, month0 = divmod(month_index, 12)
    month = month0 + 1
    last = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{min(dom, last):02d}"


def last_session_on_or_before(sessions: list[str], target: str) -> str | None:
    index = bisect.bisect_right(sessions, target) - 1
    if index < 0:
        return None
    return sessions[index]


def twelve_seven_return(
    sessions: list[str],
    bars: dict[str, tuple[float, float]],
    decision_day: str,
) -> float | None:
    """Adjusted close at t-7 months over the close at t-12 months, minus one.

    Both endpoints are the last shared session on or before that calendar date.
    A missing bar makes the name ineligible. The decision close is not an input.
    """
    earlier = last_session_on_or_before(sessions, shift_months(decision_day, 12))
    later = last_session_on_or_before(sessions, shift_months(decision_day, 7))
    if earlier is None or later is None or later <= earlier:
        return None
    if later > decision_day or earlier > decision_day:
        return None
    old = bars.get(earlier)
    new = bars.get(later)
    if old is None or new is None or old[1] <= 0:
        return None
    return new[1] / old[1] - 1.0
