"""Load monthly Wayback web sentiment and forward-fill to daily bars."""

from pathlib import Path

import numpy as np
import pandas as pd

import config

ROOT = Path(__file__).resolve().parents[1]


def wayback_sentiment_path() -> Path:
    primary = ROOT / config.WAYBACK_SENTIMENT_FILE
    legacy = ROOT / "wayback_sentiment.csv"
    return primary if primary.exists() or not legacy.exists() else legacy


def load_monthly_web_sentiment(path: Path | None = None) -> pd.Series:
    """Average archive sources per month -> Series indexed by month-start Timestamp."""
    cache = path or wayback_sentiment_path()
    if not cache.exists():
        return pd.Series(dtype=float, name="web_sentiment")
    df = pd.read_csv(cache, parse_dates=["month"])
    if df.empty:
        return pd.Series(dtype=float, name="web_sentiment")
    monthly = df.groupby("month")["sentiment"].mean().sort_index()
    monthly.name = "web_sentiment"
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
