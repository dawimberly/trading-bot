"""Wisdom layer: combine live/Wayback web mood with price math for RHYME regimes."""

from __future__ import annotations

import datetime
import warnings

import numpy as np
import pandas as pd

import config
from modules.market_context import get_market_regime, get_price_sentiment, get_volatility
from modules.wayback_sentiment import normalize_price_sentiment, web_sentiment_for_date
from modules.web_sentiment_live import get_live_web_sentiment
from modules.wisdom_adaptor import get_dynamic_wisdom_signal

LIVE_MODES = ("baseline", "dynamic")
DEPRECATED_MODES = ("web_regime", "arbitrage", "wisdom_pause", "governor")
MODES = LIVE_MODES + DEPRECATED_MODES
PAUSE_REGIME = "RHYME_E: Steady_Bearish_Decline"
BEAR_REGIME = "RHYME_E: Steady_Bearish_Decline"
PANIC_REGIME = "RHYME_B: Panic_Volatility"


def normalize_wisdom_mode(mode: str | None) -> str:
    """Resolve WISDOM_MODE for live bot; map config-level deprecated modes to dynamic."""
    raw = (mode or config.WISDOM_MODE).strip().lower()
    from_config = mode is None or raw == config.WISDOM_MODE.strip().lower()
    if raw in DEPRECATED_MODES and from_config:
        warnings.warn(
            f"WISDOM_MODE={raw!r} is deprecated; using dynamic.",
            stacklevel=2,
        )
        return "dynamic"
    if raw not in MODES:
        print(f"Unknown WISDOM_MODE '{raw}', falling back to baseline.")
        return "baseline"
    return raw


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

    if mode == "dynamic":
        vol = get_volatility(data)
        dyn = get_dynamic_wisdom_signal(
            data,
            price_sentiment=price,
            web_sentiment=web,
            gap=gap,
            vol=vol,
        )
        return dyn["effective_sentiment"], web, gap

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
    stress_confirmed: bool | None = None,
    dynamic_signal: dict | None = None,
) -> bool:
    if mode == "dynamic":
        if dynamic_signal is not None:
            return bool(dynamic_signal.get("wisdom_paused"))
        if data is None or vol is None:
            return False
        price = get_price_sentiment(data)
        dyn = get_dynamic_wisdom_signal(
            data,
            price_sentiment=price,
            web_sentiment=web,
            gap=gap,
            vol=vol,
        )
        return bool(dyn.get("wisdom_paused"))
    if web is None or np.isnan(web) or not _gap_exceeds(gap, gap_threshold):
        return False
    if mode == "wisdom_pause":
        return True
    if mode == "governor":
        if stress_confirmed is not None:
            return stress_confirmed
        if data is None or vol is None:
            return False
        return governor_stress_confirmed(data, vol)
    return False


def _resolve_dynamic(
    data: pd.DataFrame,
    *,
    price_sent: float,
    web: float | None,
    gap: float | None,
    vol: str,
    macro_stress: bool | None = None,
) -> dict:
    return get_dynamic_wisdom_signal(
        data,
        price_sentiment=price_sent,
        web_sentiment=web,
        gap=gap,
        vol=vol,
        macro_stress=macro_stress,
    )


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
    mode = normalize_wisdom_mode(mode)
    gap_threshold = gap_threshold if gap_threshold is not None else config.WISDOM_GAP_THRESHOLD
    ts = pd.Timestamp(ts or datetime.datetime.now())

    vol = get_volatility(data)
    price_sent = get_price_sentiment(data)

    web: float | None = None
    if mode != "baseline":
        if mode == "dynamic" and not config.AUTO_DYNAMIC_ENABLED:
            web = None
        else:
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

    dynamic_signal = None
    sizing_multiplier = 1.0
    gap_tier = None
    macro_stress = None

    if mode == "dynamic" and config.AUTO_DYNAMIC_ENABLED:
        dynamic_signal = _resolve_dynamic(
            data, price_sent=price_sent, web=web_used, gap=gap, vol=vol
        )
        sent = dynamic_signal["effective_sentiment"]
        sizing_multiplier = dynamic_signal["sizing_multiplier"]
        gap_tier = dynamic_signal["gap_tier"]
        macro_stress = dynamic_signal["macro_stress"]

    regime = get_market_regime(sent, vol)
    stress_confirmed = (
        governor_stress_confirmed(data, vol) if mode == "governor" else None
    )
    paused = entries_paused(
        mode,
        web_used,
        gap,
        gap_threshold,
        data=data,
        vol=vol,
        stress_confirmed=stress_confirmed,
        dynamic_signal=dynamic_signal,
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
        "governor_stress": stress_confirmed if mode == "governor" else macro_stress,
        "sizing_multiplier": sizing_multiplier,
        "gap_tier": gap_tier,
        "dynamic_stress": macro_stress,
    }


def resolve_backtest_regime(
    data: pd.DataFrame,
    ts: pd.Timestamp,
    monthly_web: pd.Series | None,
    *,
    wisdom_mode: str | None = None,
    gap_threshold: float | None = None,
) -> tuple[str, str, bool, float]:
    """Regime for daily backtests (optional Wayback web + wisdom pause)."""
    vol = get_volatility(data)
    if not wisdom_mode:
        price_sent = get_price_sentiment(data)
        return get_market_regime(price_sent, vol), vol, False, 1.0

    mode = wisdom_mode.strip().lower()
    if mode not in MODES:
        mode = "baseline"
    gap_threshold = gap_threshold if gap_threshold is not None else config.WISDOM_GAP_THRESHOLD
    web_series = monthly_web if monthly_web is not None else pd.Series(dtype=float)

    price_sent = get_price_sentiment(data)
    sent, web, gap = regime_sentiment(
        data, ts, web_series, mode=mode, gap_threshold=gap_threshold
    )

    dynamic_signal = None
    sizing_multiplier = 1.0
    if mode == "dynamic" and config.AUTO_DYNAMIC_ENABLED:
        dynamic_signal = _resolve_dynamic(
            data, price_sent=price_sent, web=web, gap=gap, vol=vol
        )
        sent = dynamic_signal["effective_sentiment"]
        sizing_multiplier = dynamic_signal["sizing_multiplier"]

    regime = get_market_regime(sent, vol)
    stress_confirmed = governor_stress_confirmed(data, vol) if mode == "governor" else None
    paused = entries_paused(
        mode,
        web,
        gap,
        gap_threshold,
        data=data,
        vol=vol,
        stress_confirmed=stress_confirmed,
        dynamic_signal=dynamic_signal,
    )
    if paused:
        regime = PAUSE_REGIME
    return regime, vol, paused, sizing_multiplier
