"""Frozen three-feature block from the Lab Next ranker.

The arithmetic matches study_engine/model.py on the overnight branch.
The mean uses the last 50 closes, including the current close.
"""

from __future__ import annotations

import math

# Live entries need a quote at least this fresh. Do not raise it.
ENTRY_QUOTE_MAX_AGE_SECONDS = 7 * 60


def features(history: list[float]) -> list[float] | None:
    prices = [float(v) for v in history]
    if len(prices) < 51:
        return None
    window = prices[-51:]
    changes = [b / a - 1 for a, b in zip(window, window[1:])]
    avg = sum(window[-50:]) / 50
    mu = sum(changes) / 50
    vol = math.sqrt(sum((r - mu) ** 2 for r in changes) / 49)
    diffs = [b - a for a, b in zip(window[-15:], window[-14:])]
    gain = sum(max(x, 0) for x in diffs) / 14
    loss = sum(max(-x, 0) for x in diffs) / 14
    if gain == loss == 0:
        rsi = 50.0
    elif loss == 0:
        rsi = 100.0
    else:
        rsi = 100 - 100 / (1 + gain / loss)
    return [window[-1] / avg - 1, vol, rsi / 100]


def entry_quote_is_fresh(quote_age_seconds: float) -> bool:
    return float(quote_age_seconds) <= ENTRY_QUOTE_MAX_AGE_SECONDS
