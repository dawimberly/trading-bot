"""Multi-timeframe trend alignment — paper/research entry confirmation.

Compares 5-minute, daily, and weekly trend bias; high alignment boosts NYSE
momentum, ORB, and stat-arb entries and feeds the conviction score.
"""

from __future__ import annotations

import time
from typing import Any

import pandas as pd

import config

_ALIGN_CACHE: dict[str, tuple[float, float]] = {}
_CACHE_TTL_SEC = 180


def _cache_get(key: str) -> float | None:
    hit = _ALIGN_CACHE.get(key)
    if not hit:
        return None
    ts, val = hit
    if time.time() - ts > _CACHE_TTL_SEC:
        _ALIGN_CACHE.pop(key, None)
        return None
    return val


def _cache_put(key: str, val: float) -> None:
    _ALIGN_CACHE[key] = (time.time(), val)


def _symbol_series(data, symbol: str) -> pd.Series | None:
    sym = config.normalize_symbol(symbol)
    if data is None or not hasattr(data, "columns"):
        return None
    if sym not in data.columns:
        return None
    series = data[sym].dropna()
    return series if not series.empty else None


def _load_5m_matrix(*, days: int = 5) -> pd.DataFrame | None:
    try:
        from modules.pipeline_strategies import load_pipeline_data

        frame = load_pipeline_data(interval="5m", days=days)
        return frame if frame is not None and not frame.empty else None
    except Exception:
        try:
            from modules.data_loader import load_close_matrix

            frame = load_close_matrix(interval="5m", days=days)
            return frame if frame is not None and not frame.empty else None
        except Exception:
            return None


def _trend_bias(prices: pd.Series, ma_period: int) -> float | None:
    """Map price vs MA + MA slope to 0.0 (bearish) – 1.0 (bullish)."""
    if prices is None or len(prices) < ma_period + 2:
        return None
    ma = prices.rolling(window=ma_period).mean()
    px = float(prices.iloc[-1])
    ma_val = float(ma.iloc[-1])
    ma_prev = float(ma.iloc[-2])
    if ma_val <= 0 or px <= 0:
        return None
    rel = (px / ma_val) - 1.0
    price_score = max(0.0, min(1.0, 0.5 + rel / 0.05))
    slope = (ma_val / ma_prev) - 1.0 if ma_prev > 0 else 0.0
    slope_score = max(0.0, min(1.0, 0.5 + slope / 0.012))
    return round(0.65 * price_score + 0.35 * slope_score, 4)


def _weekly_bias_from_daily(daily: pd.Series) -> float | None:
    """Weekly trend proxy from daily closes (last ~6 months)."""
    if daily is None or len(daily) < 30:
        return None
    try:
        idx = pd.to_datetime(daily.index, errors="coerce")
        frame = pd.DataFrame({"close": daily.values}, index=idx).dropna()
        weekly = frame["close"].resample("W-FRI").last().dropna()
        if len(weekly) < 6:
            return _trend_bias(daily, min(50, len(daily) - 1))
        return _trend_bias(weekly, min(4, len(weekly) - 1))
    except Exception:
        return _trend_bias(daily, min(50, len(daily) - 1))


def _alignment_from_biases(*biases: float | None) -> float:
    scores = [float(b) for b in biases if b is not None]
    if not scores:
        return 0.5
    if len(scores) == 1:
        return round(scores[0], 4)
    mean = sum(scores) / len(scores)
    spread = max(scores) - min(scores)
    agreement = max(0.0, min(1.0, 1.0 - spread / 0.45))
    return round(agreement * 0.55 + mean * 0.45, 4)


def check_multi_timeframe_alignment(
    symbol: str,
    data_5m=None,
    data_daily=None,
) -> float:
    """Return alignment score 0.0–1.0 from 5m, daily, and weekly trend agreement.

    When called with a single data matrix (``check_multi_timeframe_alignment('SPY', data)``),
    *data* is treated as daily and 5m data is loaded on demand.
    """
    if not config.effective_multi_timeframe_enabled():
        return 0.5

    sym = config.normalize_symbol(symbol)
    if not sym:
        return 0.5

    if data_daily is None and data_5m is not None:
        data_daily = data_5m
        data_5m = None

    cache_key = f"{sym}:{id(data_daily)}:{id(data_5m)}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    daily = _symbol_series(data_daily, sym)
    if daily is None and data_daily is not None:
        return 0.5

    if data_5m is None:
        data_5m = _load_5m_matrix()

    intra = _symbol_series(data_5m, sym) if data_5m is not None else None
    bias_5m = _trend_bias(intra, 20) if intra is not None else None
    bias_daily = _trend_bias(daily, 50) if daily is not None else None
    bias_weekly = _weekly_bias_from_daily(daily) if daily is not None else None

    available = [b for b in (bias_5m, bias_daily, bias_weekly) if b is not None]
    if not available:
        return 0.5

    if bias_5m is None and bias_daily is not None:
        alignment = _alignment_from_biases(bias_daily, bias_weekly)
    elif bias_weekly is None:
        alignment = _alignment_from_biases(bias_5m, bias_daily)
    else:
        alignment = _alignment_from_biases(bias_5m, bias_daily, bias_weekly)

    _cache_put(cache_key, alignment)
    return alignment


def multi_timeframe_entry_boost(
    symbol: str,
    data_daily=None,
    data_5m=None,
) -> float:
    """Rank / sizing boost when alignment meets the configured minimum."""
    if not config.effective_multi_timeframe_enabled():
        return 0.0
    alignment = check_multi_timeframe_alignment(symbol, data_5m, data_daily)
    min_align = float(getattr(config, "MULTI_TIMEFRAME_MIN_ALIGNMENT", 0.65))
    if alignment < min_align:
        return 0.0
    factor = float(getattr(config, "MULTI_TIMEFRAME_BOOST_FACTOR", 0.22))
    excess = (alignment - min_align) / max(0.01, 1.0 - min_align)
    return round(factor * (0.55 + 0.45 * excess), 4)


def multi_timeframe_momentum_rank_boost(symbol: str, data=None) -> float:
    """Alias used by NYSE / ORB / stat-arb rank boost hooks."""
    return multi_timeframe_entry_boost(symbol, data_daily=data)


def multi_timeframe_alignment_ok(symbol: str, data=None) -> bool:
    if not config.effective_multi_timeframe_enabled():
        return True
    return (
        check_multi_timeframe_alignment(symbol, data)
        >= float(getattr(config, "MULTI_TIMEFRAME_MIN_ALIGNMENT", 0.65))
    )


def format_multi_timeframe_banner() -> str | None:
    if not config.effective_multi_timeframe_enabled():
        return ">>> Multi-Timeframe Confirmation: OFF"
    min_align = float(getattr(config, "MULTI_TIMEFRAME_MIN_ALIGNMENT", 0.65))
    return f">>> Multi-Timeframe Confirmation: ON (min {min_align:.2f}) <<<"


def format_weekly_multi_timeframe_note() -> str:
    if not config.effective_multi_timeframe_enabled():
        return ""
    try:
        from modules.pipeline_strategies import load_pipeline_data

        data = load_pipeline_data()
    except Exception:
        return "Multi-timeframe: ON (data unavailable)"
    leaders = multi_timeframe_leader_rows(data, limit=5)
    if not leaders:
        return "Multi-timeframe: ON (no alignment samples)"
    bits = [f"{r['symbol']} {r['alignment']}" for r in leaders[:5]]
    spy_align = check_multi_timeframe_alignment(config.SPY_BOT_SYMBOL, data)
    return (
        f"Multi-timeframe: ON (min {config.MULTI_TIMEFRAME_MIN_ALIGNMENT:.2f}) | "
        f"SPY {spy_align:.2f} | leaders: {', '.join(bits)}"
    )


def format_telegram_weekly_mtf_block() -> str:
    note = format_weekly_multi_timeframe_note()
    if not note:
        return ""
    return f"\n\n{note}"


def multi_timeframe_leader_rows(data=None, *, limit: int = 8) -> list[dict[str, Any]]:
    """Top symbols by multi-timeframe alignment (for dashboard)."""
    if not config.effective_multi_timeframe_enabled():
        return []
    if data is None:
        try:
            from modules.pipeline_strategies import load_pipeline_data

            data = load_pipeline_data()
        except Exception:
            return []
    if data is None or data.empty:
        return []

    min_align = float(getattr(config, "MULTI_TIMEFRAME_MIN_ALIGNMENT", 0.65))
    data_5m = _load_5m_matrix()
    rows: list[dict[str, Any]] = []
    cols = list(data.columns)[: min(40, len(data.columns))]
    for sym in cols:
        align = check_multi_timeframe_alignment(sym, data_5m, data)
        boost = multi_timeframe_entry_boost(sym, data, data_5m)
        rows.append(
            {
                "symbol": config.normalize_symbol(sym),
                "alignment": f"{align:.2f}",
                "boost": f"{boost:.2f}" if boost > 0 else "—",
                "confirmed": "YES" if align >= min_align else "no",
                "_align": align,
            }
        )
    rows.sort(key=lambda r: float(r.get("_align") or 0), reverse=True)
    return rows[:limit]


def multi_timeframe_dashboard_summary(data=None) -> str:
    """One-line summary for Strategy Performance panel header."""
    if not config.effective_multi_timeframe_enabled():
        return ""
    if data is None:
        try:
            from modules.pipeline_strategies import load_pipeline_data

            data = load_pipeline_data()
        except Exception:
            return "MTF: data unavailable"
    leaders = multi_timeframe_leader_rows(data, limit=3)
    spy = check_multi_timeframe_alignment(config.SPY_BOT_SYMBOL, data)
    min_align = float(getattr(config, "MULTI_TIMEFRAME_MIN_ALIGNMENT", 0.65))
    if not leaders:
        return f"MTF align SPY {spy:.2f} (min {min_align:.2f})"
    top = ", ".join(f"{r['symbol']} {r['alignment']}" for r in leaders[:3])
    return f"MTF align SPY {spy:.2f} | {top} (min {min_align:.2f})"
