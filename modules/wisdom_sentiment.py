"""Wisdom layer: combine live/Wayback web mood with price math for RHYME regimes."""

from __future__ import annotations

import datetime

import numpy as np
import pandas as pd

import config
from modules.market_context import get_market_regime, get_price_sentiment, get_volatility
from modules.wayback_sentiment import normalize_price_sentiment, web_sentiment_for_date
from modules.web_sentiment_live import get_live_web_sentiment

MODES = ("baseline", "web_regime", "arbitrage", "wisdom_pause", "governor")
PAUSE_REGIME = "RHYME_E: Steady_Bearish_Decline"
BEAR_REGIME = "RHYME_E: Steady_Bearish_Decline"
PANIC_REGIME = "RHYME_B: Panic_Volatility"


def regime_sentiment(
    data,
    ts,
    monthly_web,
    mode: str = "baseline",
    gap_threshold: float = 0.25,
    web_override: float | None = None,
) -> tuple[float, float | None, float | None]:
    """Return (sentiment_for_regime, web_sentiment or None, gap or None)."""
    price = get_price_sentiment(data)
    if web_override is not None:
        web = web_override
    elif monthly_web is not None and not monthly_web.empty:
        web = web_sentiment_for_date(monthly_web, ts)
    else:
        web = float("nan")

    math_n = normalize_price_sentiment(price)
    gap = web - math_n if not np.isnan(web) else None

    if mode == "baseline" or np.isnan(web):
        return price, (None if np.isnan(web) else web), gap

    if mode == "web_regime":
        return web, web, gap

    if mode in ("arbitrage", "governor"):
        if gap is not None and abs(gap) >= gap_threshold:
            return price, web, gap
        return (web + math_n) / 2.0, web, gap

    if mode == "wisdom_pause":
        return price, web, gap

    return price, web, gap


def _gap_exceeds(gap, gap_threshold: float) -> bool:
    return gap is not None and not np.isnan(gap) and abs(gap) >= gap_threshold


def governor_stress_confirmed(data, vol: str) -> bool:
    """True when vol/macro/game-plan stress confirms a headline-price gap is dangerous."""
    price_regime = get_market_regime(get_price_sentiment(data), vol)
    if vol == "High" or price_regime in (BEAR_REGIME, PANIC_REGIME):
        return True
    if not config.GAME_PLAN_ENABLED:
        return False
    try:
        from modules.macro_signals import ensure_macro_daily, evaluate, load_daily_matrix

        ensure_macro_daily(refresh=False)
        daily = load_daily_matrix(days=450)
        if daily is None or daily.empty:
            return False
        return bool(evaluate(daily, price_regime).get("stress"))
    except Exception:
        return False


def entries_paused(
    mode: str,
    web,
    gap,
    gap_threshold: float = 0.25,
    *,
    data=None,
    vol: str | None = None,
) -> bool:
    if web is None or np.isnan(web) or not _gap_exceeds(gap, gap_threshold):
        return False
    if mode == "wisdom_pause":
        return True
    if mode == "governor":
        if data is None or vol is None:
            return False
        return governor_stress_confirmed(data, vol)
    return False


def resolve_wisdom_regime(
    data: pd.DataFrame,
    *,
    ts: datetime.datetime | pd.Timestamp | None = None,
    monthly_web: pd.Series | None = None,
    mode: str | None = None,
    gap_threshold: float | None = None,
) -> dict:
    """
    Full wisdom cycle: vol + web + price -> RHYME regime (optional entry pause).
    Live bot uses cached web fetch; backtests pass monthly_web from Wayback CSV.
    """
    mode = (mode or config.WISDOM_MODE).strip().lower()
    if mode not in MODES:
        mode = "baseline"
    gap_threshold = gap_threshold if gap_threshold is not None else config.WISDOM_GAP_THRESHOLD
    ts = pd.Timestamp(ts or datetime.datetime.now())

    vol = get_volatility(data)
    price_sent = get_price_sentiment(data)

    web: float | None = None
    if mode != "baseline":
        web = get_live_web_sentiment()
        if web is None and monthly_web is not None and not monthly_web.empty:
            w = web_sentiment_for_date(monthly_web, ts)
            web = None if np.isnan(w) else w

    sent, web_used, gap = regime_sentiment(
        data,
        ts,
        monthly_web if monthly_web is not None else pd.Series(dtype=float),
        mode=mode,
        gap_threshold=gap_threshold,
        web_override=web,
    )
    regime = get_market_regime(sent, vol)
    stress_confirmed = governor_stress_confirmed(data, vol) if mode == "governor" else None
    paused = entries_paused(
        mode, web_used, gap, gap_threshold, data=data, vol=vol
    )
    if paused:
        regime = PAUSE_REGIME

    return {
        "regime": regime,
        "volatility": vol,
        "price_sentiment": price_sent,
        "web_sentiment": web_used,
        "sentiment_gap": gap,
        "effective_sentiment": sent,
        "wisdom_mode": mode,
        "wisdom_paused": paused,
        "governor_stress": stress_confirmed,
    }
