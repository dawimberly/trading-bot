"""Opening Range Breakout (ORB) scanner — paper/research; yfinance intraday on demand."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

import config
from modules.scanner_common import bump_boost_for_insider_cluster, nyse_scan_universe

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_ORB_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}
_CACHE_TTL_SEC = 300
_SCAN_UNIVERSE_CAP = 50


def _cache_get(key: str) -> dict[str, Any] | None:
    hit = _ORB_CACHE.get(key)
    if not hit:
        return None
    ts, payload = hit
    if time.time() - ts > _CACHE_TTL_SEC:
        _ORB_CACHE.pop(key, None)
        return None
    return payload


def _cache_put(key: str, payload: dict[str, Any] | None) -> None:
    _ORB_CACHE[key] = (time.time(), payload)


def _fetch_intraday_bars(symbol: str) -> pd.DataFrame | None:
    sym = config.normalize_symbol(symbol)
    if not sym:
        return None
    day_key = datetime.now(_ET).date().isoformat()
    cache_key = f"{sym}:{day_key}"
    cached = _cache_get(cache_key)
    if cached is not None and "_bars" in cached:
        bars = cached.get("_bars")
        return bars if isinstance(bars, pd.DataFrame) else None
    try:
        import yfinance as yf

        hist = yf.Ticker(sym).history(period="1d", interval="5m", auto_adjust=False)
        if hist is None or hist.empty:
            _cache_put(cache_key, {"_bars": None})
            return None
        _cache_put(cache_key, {"_bars": hist})
        return hist
    except Exception as exc:
        logger.debug("intraday bar fetch failed for %s: %s", sym, exc)
        return None


def _session_bars_today(bars: pd.DataFrame) -> pd.DataFrame:
    idx = bars.index
    if getattr(idx, "tz", None) is None:
        idx = idx.tz_localize("UTC")
    localized = bars.copy()
    localized.index = idx.tz_convert(_ET)
    today = datetime.now(_ET).date()
    session = localized[localized.index.date == today]
    if session.empty:
        return session
    try:
        return session.between_time("09:30", "16:00")
    except Exception as exc:
        logger.debug("session time filter failed, using full session: %s", exc)
        return session


def _current_price(data, symbol: str, fallback: float | None = None) -> float | None:
    sym = config.normalize_symbol(symbol)
    if data is not None and hasattr(data, "columns") and sym in data.columns:
        prices = data[sym].dropna()
        if not prices.empty:
            px = float(prices.iloc[-1])
            if px > 0:
                return px
    return fallback


def calculate_opening_range(
    data,
    symbol: str,
    minutes: int = 30,
) -> dict[str, Any] | None:
    """High/low of the first *minutes* of today's regular session."""
    sym = config.normalize_symbol(symbol)
    if not sym:
        return None
    minutes = max(5, int(minutes or getattr(config, "ORB_BREAKOUT_MINUTES", 30)))
    cache_key = f"or:{sym}:{minutes}:{datetime.now(_ET).date().isoformat()}"
    cached = _cache_get(cache_key)
    if cached is not None and "_bars" not in cached:
        return cached

    bars = _fetch_intraday_bars(sym)
    if bars is None or bars.empty:
        return None
    session = _session_bars_today(bars)
    if session.empty:
        return None

    bar_count = max(1, minutes // 5)
    opening = session.iloc[:bar_count]
    if opening.empty or "High" not in opening.columns or "Low" not in opening.columns:
        return None

    or_high = float(opening["High"].max())
    or_low = float(opening["Low"].min())
    if or_high <= 0 or or_low <= 0:
        return None

    last_close = float(session["Close"].iloc[-1]) if "Close" in session.columns else None
    current = _current_price(data, sym, fallback=last_close)
    if current is None:
        return None

    payload = {
        "symbol": sym,
        "minutes": minutes,
        "or_high": round(or_high, 4),
        "or_low": round(or_low, 4),
        "current": round(current, 4),
        "breakout_up": current > or_high,
        "breakout_down": current < or_low,
    }
    _cache_put(cache_key, payload)
    return payload


def get_orb_signals(
    data,
    minutes: int = 30,
    volume_filter: bool = True,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return ORB breakout signals from the momentum universe (list, may be empty)."""
    if not config.effective_orb_enabled():
        return []

    minutes = max(5, int(minutes or getattr(config, "ORB_BREAKOUT_MINUTES", 30)))
    limit = max(1, int(limit))
    from modules.volume_analysis import calculate_rvol

    upside: list[dict[str, Any]] = []
    downside: list[dict[str, Any]] = []
    for sym in nyse_scan_universe(data, limit_scan=_SCAN_UNIVERSE_CAP):
        or_info = calculate_opening_range(data, sym, minutes=minutes)
        if not or_info:
            continue
        rvol = calculate_rvol(data, sym)
        base = {
            "symbol": sym,
            "minutes": minutes,
            "or_high": or_info["or_high"],
            "or_low": or_info["or_low"],
            "price": or_info["current"],
            "rvol": rvol,
        }
        if or_info["breakout_up"]:
            if volume_filter:
                min_rvol = float(config.ORB_RVOL_MIN)
                if rvol is None or rvol < min_rvol:
                    continue
            ext = (or_info["current"] / or_info["or_high"] - 1.0) * 100.0
            upside.append(
                {
                    **base,
                    "type": "up_breakout",
                    "break_price": or_info["or_high"],
                    "breakout_pct": round(ext, 2),
                }
            )
        elif or_info["breakout_down"]:
            ext = (1.0 - or_info["current"] / or_info["or_low"]) * 100.0
            downside.append(
                {
                    **base,
                    "type": "down_breakout",
                    "break_price": or_info["or_low"],
                    "breakout_pct": round(ext, 2),
                }
            )

    upside.sort(
        key=lambda r: (float(r.get("rvol") or 0.0), float(r.get("breakout_pct") or 0.0)),
        reverse=True,
    )
    downside.sort(key=lambda r: float(r.get("breakout_pct") or 0.0), reverse=True)
    signals = upside + downside
    return signals[:limit]


def orb_momentum_rank_boost(symbol: str, data=None) -> float:
    """Momentum rank boost for OR-high breakout with elevated RVOL."""
    if not config.effective_orb_enabled():
        return 0.0
    or_info = calculate_opening_range(
        data, symbol, minutes=int(getattr(config, "ORB_BREAKOUT_MINUTES", 30))
    )
    if not or_info or not or_info.get("breakout_up"):
        return 0.0
    from modules.volume_analysis import calculate_rvol

    rvol = calculate_rvol(data, symbol)
    if rvol is None or rvol < float(config.ORB_RVOL_MIN):
        return 0.0
    boost = float(config.ORB_BOOST_FACTOR)
    boost = bump_boost_for_insider_cluster(boost, symbol, cap_mult=1.3)
    try:
        from modules.multi_timeframe import multi_timeframe_momentum_rank_boost

        boost = round(min(boost + multi_timeframe_momentum_rank_boost(symbol, data), boost * 1.35), 4)
    except Exception as exc:
        logger.debug("multi-timeframe ORB rank bump skipped for %s: %s", symbol, exc)
    return round(boost, 4)


def orb_insider_cluster_extra(symbol: str, data=None) -> float:
    """Extra insider cluster boost when ORB upside + RVOL align."""
    if not config.effective_orb_enabled():
        return 0.0
    or_info = calculate_opening_range(
        data, symbol, minutes=int(getattr(config, "ORB_BREAKOUT_MINUTES", 30))
    )
    if not or_info or not or_info.get("breakout_up"):
        return 0.0
    from modules.volume_analysis import calculate_rvol

    rvol = calculate_rvol(data, symbol)
    if rvol is None or rvol < float(config.ORB_RVOL_MIN):
        return 0.0
    return 0.04


def format_orb_scanner_banner() -> str | None:
    if not config.effective_orb_enabled():
        return ">>> ORB Scanner: OFF"
    return f">>> ORB Scanner: ON ({int(config.ORB_BREAKOUT_MINUTES)}min)"


def format_telegram_weekly_orb_block(data=None) -> str:
    if not config.effective_orb_enabled():
        return ""
    if data is None:
        try:
            from modules.pipeline_strategies import load_pipeline_data

            data = load_pipeline_data()
        except Exception as exc:
            logger.debug("pipeline data unavailable for ORB telegram block: %s", exc)
            return ""
    signals = get_orb_signals(
        data,
        minutes=int(config.ORB_BREAKOUT_MINUTES),
        volume_filter=True,
        limit=12,
    )
    upside = [s for s in signals if s.get("type") == "up_breakout"]
    if not upside:
        return f"\n\nORB ({config.ORB_BREAKOUT_MINUTES}m): no upside breakouts w/ RVOL ≥ {config.ORB_RVOL_MIN:.1f}x."
    lines = [f"\n\nORB setups ({config.ORB_BREAKOUT_MINUTES}m, RVOL ≥ {config.ORB_RVOL_MIN:.1f}x):"]
    for row in upside[:5]:
        rvol_s = f"{float(row['rvol']):.1f}x" if row.get("rvol") is not None else "—"
        lines.append(
            f"  {row['symbol']} ↑ OR-high {row['or_high']:.2f} | "
            f"px {row['price']:.2f} | RVOL {rvol_s}"
        )
    downside = [s for s in signals if s.get("type") == "down_breakout"]
    if downside:
        lines.append("  Downside watch:")
        for row in downside[:3]:
            lines.append(f"    {row['symbol']} ↓ OR-low {row['or_low']:.2f}")
    return "\n".join(lines)


def combined_scanner_dashboard_rows(data=None, *, limit: int = 10) -> list[dict[str, str]]:
    """RVOL leaders plus ORB upside breakouts for dashboard."""
    if data is None:
        from modules.pipeline_strategies import load_pipeline_data

        data = load_pipeline_data()
    from modules.volume_analysis import get_high_rvol_stocks

    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    orb = get_orb_signals(
        data,
        minutes=int(config.ORB_BREAKOUT_MINUTES),
        volume_filter=True,
        limit=limit,
    )
    for item in orb:
        if item.get("type") != "up_breakout":
            continue
        sym = str(item["symbol"])
        seen.add(sym)
        rvol = item.get("rvol")
        rvol_s = f"{float(rvol):.2f}x" if rvol is not None else "—"
        rows.append(
            {
                "Symbol": sym,
                "RVOL": rvol_s,
                "ORB": f"↑ {item['or_high']:.2f}",
                "Signal": "ORB breakout",
                "_tag": "orb_up",
            }
        )

    for item in get_high_rvol_stocks(data, min_rvol=config.RVOL_MIN_THRESHOLD, limit=limit):
        sym = str(item["symbol"])
        if sym in seen:
            continue
        seen.add(sym)
        rvol = float(item["rvol"])
        tag = "rvol_strong" if rvol >= config.RVOL_STRONG_THRESHOLD else "rvol_high"
        rows.append(
            {
                "Symbol": sym,
                "RVOL": f"{rvol:.2f}x",
                "ORB": "—",
                "Signal": "High RVOL",
                "_tag": tag,
            }
        )
        if len(rows) >= limit:
            break
    try:
        from modules.catalyst_scoring import extend_combined_scanner_rows

        rows = extend_combined_scanner_rows(rows, seen, data, limit=limit)
    except Exception as exc:
        logger.debug("catalyst scanner row merge skipped in combined dashboard: %s", exc)
    return rows[:limit]
