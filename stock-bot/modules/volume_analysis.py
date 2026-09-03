"""Relative volume (RVOL) scanning — paper/research; yfinance volume on demand."""

from __future__ import annotations

import logging
import time
from typing import Any

import pandas as pd

import config
from modules.scanner_common import nyse_scan_universe

logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, pd.Series]] = {}
_CACHE_TTL_SEC = 3600


def _cache_get(symbol: str) -> pd.Series | None:
    sym = config.normalize_symbol(symbol)
    hit = _CACHE.get(sym)
    if not hit:
        return None
    ts, series = hit
    if time.time() - ts > _CACHE_TTL_SEC:
        _CACHE.pop(sym, None)
        return None
    return series


def _cache_put(symbol: str, series: pd.Series) -> None:
    sym = config.normalize_symbol(symbol)
    _CACHE[sym] = (time.time(), series)


def _fetch_volume_series(symbol: str, *, lookback_days: int) -> pd.Series | None:
    sym = config.normalize_symbol(symbol)
    if not sym or sym.upper() in ("NONE", "NULL", "NAN"):
        return None
    cached = _cache_get(sym)
    if cached is not None:
        return cached
    try:
        import yfinance as yf

        period_days = max(lookback_days + 15, 30)
        hist = yf.Ticker(config.yf_symbol(sym)).history(period=f"{period_days}d", auto_adjust=False)
        if hist is None or hist.empty or "Volume" not in hist.columns:
            return None
        vols = hist["Volume"].dropna()
        vols = vols[vols > 0]
        if vols.empty:
            return None
        _cache_put(sym, vols)
        return vols
    except Exception as exc:
        logger.debug("volume history fetch failed for %s: %s", sym, exc)
        return None


def _volume_series_from_data(data, symbol: str) -> pd.Series | None:
    """Extract as-of volume from the sim matrix when present (MultiIndex or columns)."""
    if data is None:
        return None
    sym = config.normalize_symbol(symbol)
    try:
        if isinstance(getattr(data, "columns", None), pd.MultiIndex):
            # Prefer (symbol, Volume) or (Volume, symbol) layouts.
            for key in ((sym, "Volume"), ("Volume", sym), (sym, "volume"), ("volume", sym)):
                if key in data.columns:
                    s = data[key].dropna()
                    s = s[s > 0]
                    return s if not s.empty else None
            # Flat volume column named like AAPL_Volume
            for col in data.columns:
                c = str(col)
                if c.upper() in (f"{sym}_VOLUME", f"{sym}.VOLUME"):
                    s = data[col].dropna()
                    s = s[s > 0]
                    return s if not s.empty else None
            return None
        if "Volume" in getattr(data, "columns", []):
            # Single-asset OHLCV frame
            s = data["Volume"].dropna()
            s = s[s > 0]
            return s if not s.empty else None
        vol_col = f"{sym}_Volume"
        if vol_col in getattr(data, "columns", []):
            s = data[vol_col].dropna()
            s = s[s > 0]
            return s if not s.empty else None
    except Exception as exc:
        logger.debug("volume-from-data failed for %s: %s", sym, exc)
    return None


def calculate_rvol(data, symbol: str, lookback_days: int = 10) -> float | None:
    """Current session volume vs average of prior *lookback_days* daily bars.

    In backtest / STRICT PIT: use volume from the sim matrix only (no yfinance).
    If volume is missing, return None → callers treat as neutral (no boost).
    """
    if not symbol:
        return None
    lookback = max(2, int(lookback_days or getattr(config, "RVOL_LOOKBACK_DAYS", 10)))
    in_backtest = bool(
        config.backtest_paper_sleeves_context() or config.effective_strict_pit_backtest()
    )
    if in_backtest:
        vols = _volume_series_from_data(data, symbol)
        if vols is None or len(vols) < 2:
            return None
    else:
        vols = _fetch_volume_series(symbol, lookback_days=lookback)
        if vols is None or len(vols) < 2:
            return None
    tail = vols.tail(lookback + 1)
    current = float(tail.iloc[-1])
    prior = tail.iloc[:-1]
    if prior.empty:
        return None
    avg = float(prior.mean())
    if avg <= 0 or current <= 0:
        return None
    return round(current / avg, 3)


def get_high_rvol_stocks(
    data,
    min_rvol: float = 2.0,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Rank symbols in *data* columns by RVOL (desc)."""
    min_rvol = float(min_rvol)
    limit = max(1, int(limit))
    # Prefer momentum universe (capped) — same source as ORB/catalyst scanners.
    symbols = nyse_scan_universe(data, limit_scan=max(80, limit * 4))
    scored: list[tuple[float, str]] = []
    for sym in symbols:
        rvol = calculate_rvol(data, sym)
        if rvol is not None and rvol >= min_rvol:
            scored.append((rvol, sym))
    scored.sort(reverse=True)
    return [{"symbol": sym, "rvol": rvol} for rvol, sym in scored[:limit]]


def filter_symbols_by_rvol(
    symbols: list[str],
    data,
    *,
    min_rvol: float | None = None,
) -> list[str]:
    """Drop names below *min_rvol*; keep when RVOL unknown."""
    if not config.effective_rvol_scanner_enabled():
        return list(symbols)
    threshold = float(min_rvol if min_rvol is not None else config.RVOL_MIN_THRESHOLD)
    out: list[str] = []
    for sym in symbols:
        rvol = calculate_rvol(data, sym)
        if rvol is None or rvol >= threshold:
            out.append(sym)
    return out


def prioritize_symbols_by_rvol(symbols: list[str], data) -> list[str]:
    """High-RVOL names first; unknown RVOL kept at the end in original order."""
    if not symbols:
        return []
    scored: list[tuple[float, str]] = []
    unknown: list[str] = []
    for sym in symbols:
        rvol = calculate_rvol(data, sym)
        if rvol is None:
            unknown.append(sym)
        else:
            scored.append((rvol, sym))
    scored.sort(reverse=True)
    return [s for _, s in scored] + unknown


def apply_rvol_universe_boost(symbols: list[str], data) -> list[str]:
    """Filter low-RVOL names, then prioritize high-RVOL for momentum sleeve."""
    cols = filter_symbols_by_rvol(symbols, data)
    min_pool = max(10, int(getattr(config, "STAT_ARB_MIN_UNIVERSE", 20) or 20))
    # Never collapse a healthy momentum pool to 0-1 names (idle NYSE + excess cash).
    if len(cols) < min_pool and len(symbols) >= min_pool:
        cols = list(symbols)
    elif not cols and symbols:
        cols = list(symbols)
    return prioritize_symbols_by_rvol(cols, data)


def rvol_momentum_rank_boost(symbol: str, data=None) -> float:
    if not config.effective_rvol_scanner_enabled():
        return 0.0
    rvol = calculate_rvol(data, symbol)
    if rvol is None:
        return 0.0
    threshold = float(getattr(config, "RVOL_MOMENTUM_BOOST_THRESHOLD", 2.5))
    if rvol < threshold:
        return 0.0
    boost = float(config.RVOL_BOOST_FACTOR)
    if rvol >= float(config.RVOL_STRONG_THRESHOLD):
        boost = min(boost * 1.25, boost + 0.05)
    return round(boost, 4)


def stat_arb_long_rvol_mult(symbol: str, data=None) -> float:
    if not config.effective_rvol_scanner_enabled():
        return 1.0
    rvol = calculate_rvol(data, symbol)
    if rvol is None:
        return 1.0
    threshold = float(getattr(config, "RVOL_MOMENTUM_BOOST_THRESHOLD", 2.5))
    if rvol < threshold:
        return 1.0
    mult = 1.0 + float(config.RVOL_BOOST_FACTOR)
    if rvol >= float(config.RVOL_STRONG_THRESHOLD):
        mult = min(mult + 0.03, 1.25)
    return round(mult, 4)


def insider_cluster_rvol_momentum_extra(symbol: str, data=None) -> float:
    """Extra momentum boost for insider cluster when RVOL elevated."""
    if not config.effective_rvol_scanner_enabled():
        return 0.0
    rvol = calculate_rvol(data, symbol)
    if rvol is None:
        return 0.0
    if rvol >= float(config.RVOL_STRONG_THRESHOLD):
        return 0.05
    if rvol >= float(getattr(config, "RVOL_MOMENTUM_BOOST_THRESHOLD", 2.5)):
        return 0.03
    return 0.0


def insider_cluster_rvol_stat_arb_extra(symbol: str, data=None) -> float:
    """Additive mult bump for stat arb on insider + high RVOL."""
    if not config.effective_rvol_scanner_enabled():
        return 0.0
    rvol = calculate_rvol(data, symbol)
    if rvol is None:
        return 0.0
    if rvol >= float(config.RVOL_STRONG_THRESHOLD):
        return 0.06
    if rvol >= float(getattr(config, "RVOL_MOMENTUM_BOOST_THRESHOLD", 2.5)):
        return 0.04
    return 0.0


def format_rvol_scanner_banner() -> str | None:
    if not config.effective_rvol_scanner_enabled():
        return ">>> RVOL Scanner: OFF"
    return ">>> RVOL Scanner: ON"


def format_telegram_weekly_rvol_block(data=None) -> str:
    if not config.effective_rvol_scanner_enabled():
        return ""
    if data is None:
        try:
            from modules.pipeline_strategies import load_pipeline_data

            data = load_pipeline_data()
        except Exception as exc:
            logger.debug("pipeline data unavailable for RVOL telegram block: %s", exc)
            return ""
    top = get_high_rvol_stocks(data, min_rvol=config.RVOL_MIN_THRESHOLD, limit=8)
    block = ""
    if not top:
        block = f"\n\nRVOL: no names above {config.RVOL_MIN_THRESHOLD:.1f}x this week."
    else:
        lines = [f"\n\nRVOL leaders (vs {config.RVOL_LOOKBACK_DAYS}d avg):"]
        for row in top:
            lines.append(f"  {row['symbol']} {row['rvol']:.2f}x")
        block = "\n".join(lines)
    try:
        from modules.orb_strategy import format_telegram_weekly_orb_block

        block += format_telegram_weekly_orb_block(data)
    except Exception as exc:
        logger.debug("ORB telegram block append skipped: %s", exc)
    try:
        from modules.catalyst_scoring import format_telegram_weekly_catalyst_block

        block += format_telegram_weekly_catalyst_block(data)
    except Exception as exc:
        logger.debug("catalyst telegram block append skipped: %s", exc)
    return block


def rvol_dashboard_rows(data=None, *, limit: int = 10) -> list[dict[str, str]]:
    if data is None:
        from modules.pipeline_strategies import load_pipeline_data

        data = load_pipeline_data()
    try:
        from modules.orb_strategy import combined_scanner_dashboard_rows

        return combined_scanner_dashboard_rows(data, limit=limit)
    except Exception as exc:
        logger.debug("combined scanner dashboard rows unavailable, using RVOL fallback: %s", exc)
    top = get_high_rvol_stocks(data, min_rvol=config.RVOL_MIN_THRESHOLD, limit=limit)
    rows: list[dict[str, str]] = []
    for row in top:
        rvol = float(row["rvol"])
        tag = "rvol_strong" if rvol >= config.RVOL_STRONG_THRESHOLD else "rvol_high"
        rows.append(
            {
                "Symbol": row["symbol"],
                "RVOL": f"{rvol:.2f}x",
                "vs Avg": f"{config.RVOL_LOOKBACK_DAYS}d",
                "_tag": tag,
            }
        )
    return rows


# ORB helpers (implemented in orb_strategy; re-exported for tests / legacy imports)
from modules.orb_strategy import (  # noqa: E402
    calculate_opening_range,
    combined_scanner_dashboard_rows,
    format_orb_scanner_banner,
    format_telegram_weekly_orb_block,
    get_orb_signals,
    orb_momentum_rank_boost,
)
