"""Load monthly Wayback web sentiment and forward-fill to daily bars."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

import config

ROOT = Path(__file__).resolve().parents[1]
_WAYBACK_SERIES_CACHE: tuple[float, Path, pd.Series] | None = None
_WAYBACK_CACHE_TTL_SEC = 3600.0


def wayback_sentiment_path() -> Path:
    primary = ROOT / config.WAYBACK_SENTIMENT_FILE
    legacy = ROOT / "wayback_sentiment.csv"
    return primary if primary.exists() or not legacy.exists() else legacy


def clear_wayback_sentiment_cache() -> None:
    """Drop in-process Wayback CSV cache (e.g. after sentiment file refresh)."""
    global _WAYBACK_SERIES_CACHE
    _WAYBACK_SERIES_CACHE = None


def load_monthly_web_sentiment(
    path: Path | None = None,
    *,
    force_refresh: bool = False,
) -> pd.Series:
    """Average archive sources per month -> Series indexed by month-start Timestamp."""
    global _WAYBACK_SERIES_CACHE
    cache_path = path or wayback_sentiment_path()
    now = time.monotonic()
    if (
        not force_refresh
        and _WAYBACK_SERIES_CACHE is not None
        and _WAYBACK_SERIES_CACHE[1] == cache_path
        and now - _WAYBACK_SERIES_CACHE[0] < _WAYBACK_CACHE_TTL_SEC
    ):
        return _WAYBACK_SERIES_CACHE[2]
    if not cache_path.exists():
        monthly = pd.Series(dtype=float, name="web_sentiment")
    else:
        df = pd.read_csv(cache_path, parse_dates=["month"])
        if df.empty:
            monthly = pd.Series(dtype=float, name="web_sentiment")
        else:
            monthly = df.groupby("month")["sentiment"].mean().sort_index()
            monthly.name = "web_sentiment"
    _WAYBACK_SERIES_CACHE = (now, cache_path, monthly)
    return monthly


def web_sentiment_for_date(monthly: pd.Series, ts: pd.Timestamp) -> float:
    """Latest monthly web score on or before ts (no lookahead)."""
    if monthly.empty:
        return float("nan")
    ts = pd.Timestamp(ts)
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    eligible = monthly.loc[monthly.index <= ts.replace(day=1)]
    if eligible.empty:
        return float("nan")
    return float(eligible.iloc[-1])


def normalize_price_sentiment(price_sentiment: float, scale: float = 15.0) -> float:
    """Map small price-momentum scores onto [-1, 1] for gap vs web."""
    return float(np.clip(price_sentiment * scale, -1.0, 1.0))
