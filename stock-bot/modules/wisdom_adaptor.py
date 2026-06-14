"""Dynamic wisdom adaptor — single intelligent mode replacing discrete WISDOM_MODE variants."""

from __future__ import annotations

import numpy as np
import pandas as pd

import config
from modules.market_context import get_market_regime
from modules.wayback_sentiment import normalize_price_sentiment

BEAR_REGIME = "RHYME_E: Steady_Bearish_Decline"
PANIC_REGIME = "RHYME_B: Panic_Volatility"
BULL_REGIME = "RHYME_C: Steady_Bullish_Growth"


def _spy_ma200_distance(data: pd.DataFrame) -> float | None:
    """Fraction SPY is above MA200 (negative if below). None if unavailable."""
    symbol = config.SPY_BOT_SYMBOL
    if symbol not in data.columns:
        return None
    prices = data[symbol].dropna()
    window = config.SPY_MA_WINDOW
    if len(prices) < min(window, 50):
        return None
    ma = prices.rolling(window=min(window, len(prices))).mean().iloc[-1]
    current = float(prices.iloc[-1])
    if not np.isfinite(ma) or ma <= 0:
        return None
    return (current - ma) / ma


def _resolve_macro_stress(
    data: pd.DataFrame,
    vol: str,
    regime: str,
    macro_stress: bool | None,
) -> bool:
    if macro_stress is not None:
        return macro_stress
    if vol == "High" or regime in (BEAR_REGIME, PANIC_REGIME):
        return True
    if not config.GAME_PLAN_ENABLED:
        return False
    try:
        from modules.macro_signals import ensure_macro_daily, evaluate, load_daily_matrix

        ensure_macro_daily(refresh=False)
        daily = load_daily_matrix(days=450)
        if daily is None or daily.empty:
            return False
        return bool(evaluate(daily, regime).get("stress"))
    except Exception:
        return False


def get_dynamic_wisdom_signal(
    data: pd.DataFrame,
    *,
    price_sentiment: float,
    web_sentiment: float | None,
    gap: float | None,
    vol: str,
    macro_stress: bool | None = None,
) -> dict:
    """
    Compute dynamic wisdom: blended sentiment, pause flag, sizing multiplier.
    Light-weight — no extra network calls beyond existing web fetch.
    """
    min_mult = config.DYNAMIC_SIZING_MULTIPLIER_MIN
    max_mult = config.DYNAMIC_SIZING_MULTIPLIER_MAX
    math_n = normalize_price_sentiment(price_sentiment)

    if web_sentiment is None or (
        isinstance(web_sentiment, float) and np.isnan(web_sentiment)
    ):
        price_regime = get_market_regime(price_sentiment, vol)
        stress = _resolve_macro_stress(data, vol, price_regime, macro_stress)
        mult = 1.0
        ma_dist = _spy_ma200_distance(data)
        if ma_dist is not None and ma_dist > config.DYNAMIC_SPY_TREND_STRONG_PCT:
            mult = min(
                max_mult,
                1.0
                + (ma_dist - config.DYNAMIC_SPY_TREND_STRONG_PCT)
                * config.DYNAMIC_SPY_TREND_BOOST_SCALE,
            )
        if stress:
            mult = min(mult, min_mult)
        return {
            "effective_sentiment": price_sentiment,
            "web_weight": 0.0,
            "gap_tier": "no_web",
            "wisdom_paused": False,
            "sizing_multiplier": round(float(np.clip(mult, min_mult, max_mult)), 3),
            "macro_stress": stress,
            "spy_ma200_distance": ma_dist,
        }

    gap_abs = abs(gap) if gap is not None and not np.isnan(gap) else 0.0
    agg = config.SENTIMENT_GAP_THRESHOLD_AGGRESSIVE
    normal = config.SENTIMENT_GAP_THRESHOLD_NORMAL
    defn = config.SENTIMENT_GAP_THRESHOLD_DEFENSIVE

    if gap_abs < agg:
        gap_tier = "aggressive"
        web_weight = 0.5
    elif gap_abs < normal:
        gap_tier = "normal"
        span = max(normal - agg, 1e-9)
        t = (gap_abs - agg) / span
        web_weight = 0.35 * (1.0 - t) + 0.15 * t
    elif gap_abs <= defn:
        gap_tier = "normal"
        span = max(defn - normal, 1e-9)
        t = (gap_abs - normal) / span
        web_weight = 0.15 * (1.0 - t)
    else:
        gap_tier = "defensive"
        web_weight = 0.0

    if vol == "High":
        web_weight *= config.DYNAMIC_HIGH_VOL_WEB_SCALE

    web_n = float(web_sentiment)
    effective = web_weight * web_n + (1.0 - web_weight) * math_n

    eff_regime = get_market_regime(effective, vol)
    stress = _resolve_macro_stress(data, vol, eff_regime, macro_stress)

    mult = 1.0
    ma_dist = _spy_ma200_distance(data)
    if ma_dist is not None and ma_dist > config.DYNAMIC_SPY_TREND_STRONG_PCT:
        mult = min(
            max_mult,
            1.0
            + (ma_dist - config.DYNAMIC_SPY_TREND_STRONG_PCT)
            * config.DYNAMIC_SPY_TREND_BOOST_SCALE,
        )

    if vol == "Low" and eff_regime == BULL_REGIME:
        mult = min(max_mult, mult * config.DYNAMIC_LOW_VOL_TREND_BOOST)

    paused = False
    if gap_tier == "defensive":
        if stress:
            paused = True
        else:
            mult = min(mult, min_mult)
    elif stress:
        mult = min(mult, min_mult)

    mult = float(np.clip(mult, min_mult, max_mult))

    return {
        "effective_sentiment": effective,
        "web_weight": round(web_weight, 3),
        "gap_tier": gap_tier,
        "wisdom_paused": paused,
        "sizing_multiplier": round(mult, 3),
        "macro_stress": stress,
        "spy_ma200_distance": ma_dist,
    }
