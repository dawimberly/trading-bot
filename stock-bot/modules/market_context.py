"""Market regime, volatility, and sentiment helpers for the trading pipeline."""

from __future__ import annotations

import logging
import time

import numpy as np
import pandas as pd

import config
from modules.wayback_sentiment import normalize_price_sentiment

_logger = logging.getLogger(__name__)

# Live regime hysteresis state (reset via reset_regime_hysteresis for backtests).
_last_regime: str | None = None
_regime_since: float | None = None
_regime_since_bar: int | None = None
_regime_bar_index: int | None = None
_last_announced_regime: str | None = None


def reset_regime_hysteresis() -> None:
    """Clear regime dwell timer (call at backtest start)."""
    global _last_regime, _regime_since, _last_announced_regime, _regime_since_bar, _regime_bar_index
    _last_regime = None
    _regime_since = None
    _last_announced_regime = None
    _regime_since_bar = None
    _regime_bar_index = None


def set_regime_bar_index(bar_index: int | None) -> None:
    """Backtest bar clock for REGIME_MIN_DWELL_BARS hysteresis.

    Live may pass datetime; coerce to epoch-day so int() never raises.
    """
    global _regime_bar_index
    if bar_index is None:
        _regime_bar_index = None
        return
    if isinstance(bar_index, bool):
        _regime_bar_index = int(bar_index)
        return
    if isinstance(bar_index, (int, float)):
        _regime_bar_index = int(bar_index)
        return
    ts = getattr(bar_index, "timestamp", None)
    if callable(ts):
        try:
            _regime_bar_index = int(ts() // 86400)
            return
        except Exception as exc:
            _logger.debug("set_regime_bar_index timestamp coerce failed: %s", exc)
    try:
        _regime_bar_index = int(bar_index)
    except (TypeError, ValueError):
        _regime_bar_index = None
        _logger.debug(
            "set_regime_bar_index could not coerce bar_index=%r", type(bar_index).__name__
        )

def announce_regime_change(regime: str) -> str:
    """Log and print when the active RHYME label changes (live cycles)."""
    global _last_announced_regime
    if _last_announced_regime is not None and _last_announced_regime != regime:
        msg = f"REGIME CHANGE: {_last_announced_regime} -> {regime}"
        _logger.info(msg)
        print(f"--- {msg} ---")
    _last_announced_regime = regime
    return regime


def cross_asset_vol_score(data) -> float:
    """Mean cross-asset return stdev (regime vol input)."""
    if data is None or data.empty or len(data) < 2:
        return 0.0
    vol = data.pct_change().dropna().std().mean()
    return float(vol) if vol == vol else 0.0  # NaN guard


def infer_bar_interval(data) -> str:
    """Return '1d' or '5m' from index spacing (daily backtests vs intraday live)."""
    if data is None or data.empty or len(data) < 3:
        return "5m"
    idx = pd.to_datetime(data.index, errors="coerce")
    deltas = idx.to_series().diff().dropna()
    if deltas.empty:
        return "5m"
    median_sec = float(deltas.median().total_seconds())
    # >= 12h between bars treats mixed/daily matrices as daily regime input.
    if median_sec >= 12 * 3600:
        return "1d"
    return "5m"


def regime_vol_threshold(interval: str) -> float:
    """Bar-frequency-aware High/Low cutoff (5m stdev is much smaller than daily)."""
    if interval == "1d":
        return config.REGIME_VOL_THRESHOLD_DAILY
    return config.REGIME_VOL_THRESHOLD_5M


def get_volatility(data, *, interval: str | None = None):
    """Classify cross-asset volatility as High or Low."""
    bar_interval = interval or infer_bar_interval(data)
    thresh = regime_vol_threshold(bar_interval)
    return "High" if cross_asset_vol_score(data) > thresh else "Low"


def get_price_sentiment(data):
    """Price-momentum sentiment: last 5 bars vs prior 15 (days on daily, ~25m on 5m)."""
    if data is None or data.empty or len(data) < 20:
        return 0.0
    recent = data.iloc[-5:].mean()
    older = data.iloc[-20:-5].mean()
    return float((recent / older).mean() - 1.0)


def normalize_regime_sentiment(sentiment: float) -> float:
    """
    Map sentiment onto [-1, 1] for regime classification.

    Raw price momentum from get_price_sentiment is a small fraction (~±0.20 on daily).
    Web/wisdom blends are already on [-1, 1] and are passed through unchanged.
    """
    s = float(sentiment)
    if s != s:  # NaN
        return 0.0
    if abs(s) <= config.REGIME_RAW_SENTIMENT_MAX:
        return normalize_price_sentiment(s)
    return float(np.clip(s, -1.0, 1.0))


def regime_dataframe(data_5m=None) -> tuple[pd.DataFrame | None, str]:
    """
    Prefer daily closes for regime detection (stable); fall back to intraday matrix.

    Backtests pass daily windows — use them directly. Live passes 5m — load *_daily tables.
    """
    if data_5m is not None and not data_5m.empty and infer_bar_interval(data_5m) == "1d":
        return data_5m, "1d"
    try:
        from modules.data_loader import load_close_matrix

        daily = load_close_matrix(interval="1d", days=config.REGIME_DAILY_LOOKBACK_DAYS)
        if daily is not None and not daily.empty and len(daily) >= 20:
            return daily, "1d"
    except Exception as exc:
        _logger.debug("daily regime matrix load failed, falling back to intraday: %s", exc)
    if data_5m is not None and not data_5m.empty:
        interval = infer_bar_interval(data_5m)
        return data_5m, interval
    return data_5m, "5m"


def get_regime_inputs(data_5m=None) -> dict:
    """Daily-stable regime inputs; trading logic may still use the 5m matrix elsewhere."""
    regime_data, interval = regime_dataframe(data_5m)
    if regime_data is None or regime_data.empty:
        return {
            "data": regime_data,
            "interval": interval,
            "price_sentiment": 0.0,
            "volatility": "Low",
            "vol_score": 0.0,
        }
    return {
        "data": regime_data,
        "interval": interval,
        "price_sentiment": get_price_sentiment(regime_data),
        "volatility": get_volatility(regime_data, interval=interval),
        "vol_score": cross_asset_vol_score(regime_data),
    }


def _get_tavily_sentiment():
    """Optional paid news search — only when SENTIMENT_SOURCE=tavily."""
    import tavily

    api_key = config.get_tavily_api_key()
    if not api_key:
        raise ValueError("TAVILY_API_KEY not set")
    client = tavily.TavilyClient(api_key=api_key)
    results = client.search("stock market crypto sentiment today", max_results=5)
    text = " ".join(
        r.get("content", "") for r in results.get("results", [])
    ).lower()
    bullish = (
        text.count("bullish")
        + text.count("rally")
        + text.count("surge")
        + text.count("gains")
        + text.count("upbeat")
    )
    bearish = (
        text.count("bearish")
        + text.count("crash")
        + text.count("plunge")
        + text.count("decline")
        + text.count("fear")
    )
    total = bullish + bearish
    if total == 0:
        return 0.0
    return round((bullish - bearish) / total, 2)


def get_sentiment(data):
    """Regime sentiment: price momentum by default (free, unlimited)."""
    source = config.SENTIMENT_SOURCE
    if source == "price":
        return get_price_sentiment(data)
    if source == "tavily":
        try:
            return _get_tavily_sentiment()
        except Exception as e:
            print("Tavily error: " + str(e))
            return get_price_sentiment(data)
    print(f"Unknown SENTIMENT_SOURCE={source!r}; using price sentiment")
    return get_price_sentiment(data)


def _sentiment_thresholds(prior_regime: str | None) -> tuple[float, float]:
    """Return (bullish_min, bearish_max) sentiment cutoffs with hysteresis."""
    base = config.REGIME_SENTIMENT_THRESHOLD
    bump = config.REGIME_HYSTERESIS_SENTIMENT_BUMP
    bull = base
    bear = -base
    if not prior_regime:
        return bull, bear
    if any(tag in prior_regime for tag in ("RHYME_A", "RHYME_C")):
        bear = -(base + bump)
    elif any(tag in prior_regime for tag in ("RHYME_B", "RHYME_E")):
        bull = base + bump
    elif "RHYME_D" in prior_regime:
        bull = base + bump * 0.5
        bear = -(base + bump * 0.5)
    return bull, bear


def _classify_regime(
    normalized_sentiment: float,
    volatility: str,
    *,
    prior_regime: str | None = None,
) -> str:
    """Core 2×2 RHYME matrix on normalized sentiment (optional entry/exit hysteresis)."""
    bull_thresh, bear_thresh = _sentiment_thresholds(prior_regime)
    s = normalized_sentiment
    if s > bull_thresh and volatility == "High":
        return "RHYME_A: Euphoric_Volatility"
    if s < bear_thresh and volatility == "High":
        return "RHYME_B: Panic_Volatility"
    if s > bull_thresh and volatility == "Low":
        return "RHYME_C: Steady_Bullish_Growth"
    if s < bear_thresh and volatility == "Low":
        return "RHYME_E: Steady_Bearish_Decline"
    return "RHYME_D: Range_Bound_Neutral"


def _apply_regime_hysteresis(candidate: str) -> str:
    """Hold prior regime until dwell elapses (seconds live, bars in backtest)."""
    global _last_regime, _regime_since, _regime_since_bar
    now = time.time()
    if _last_regime is None:
        _last_regime = candidate
        _regime_since = now
        _regime_since_bar = _regime_bar_index
        return candidate
    if candidate == _last_regime:
        return candidate
    min_bars = int(getattr(config, "REGIME_MIN_DWELL_BARS", 0) or 0)
    if (
        min_bars > 0
        and _regime_bar_index is not None
        and _regime_since_bar is not None
        and (_regime_bar_index - _regime_since_bar) < min_bars
    ):
        return _last_regime
    dwell = config.REGIME_MIN_DWELL_SEC
    if _regime_bar_index is None and (_regime_since or now) and (now - (_regime_since or now)) < dwell:
        return _last_regime
    _last_regime = candidate
    _regime_since = now
    _regime_since_bar = _regime_bar_index
    return candidate


def get_market_regime(sentiment, volatility, *, apply_hysteresis: bool | None = None):
    """
    Classify market into one of five regime 'rhymes'.

    Sentiment is normalized before thresholding. Hysteresis uses dwell time (live)
    or dwell bars (backtest when set_regime_bar_index is active).
    """
    norm = normalize_regime_sentiment(sentiment)
    prior = _last_regime if config.REGIME_HYSTERESIS_ENABLED else None
    classified = _classify_regime(norm, volatility, prior_regime=prior)
    use_hysteresis = (
        config.REGIME_HYSTERESIS_ENABLED
        if apply_hysteresis is None
        else apply_hysteresis
    )
    if use_hysteresis:
        result = _apply_regime_hysteresis(classified)
    else:
        result = classified
    if use_hysteresis:
        announce_regime_change(result)
    return result


def current_regime_from_data(data) -> str | None:
    """Classify regime from a price matrix (startup banner / diagnostics)."""
    if data is None or getattr(data, "empty", True) or len(data) < 20:
        return None
    sentiment = get_price_sentiment(data)
    vol = get_volatility(data)
    return get_market_regime(sentiment, vol, apply_hysteresis=True)
