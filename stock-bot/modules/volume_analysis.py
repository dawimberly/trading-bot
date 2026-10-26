"""Relative volume (RVOL) scanning — paper/research; yfinance volume on demand."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import pandas as pd

import config
from modules.scanner_common import nyse_scan_universe

logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, pd.Series]] = {}
_CACHE_TTL_SEC = 3600
# One Yahoo call must not sit on a dead socket. One RVOL pass (filter plus
# re-rank) shares RVOL_SCAN_BUDGET_SEC. A timeout or a spent budget is
# unknown RVOL: eligible to stay in the sleeve, not a confirmed pass, and
# not inserted into the scored ranking.
_YF_TIMEOUT_SEC = 8.0
RVOL_SCAN_BUDGET_SEC = 45.0


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


def _deadline_at(seconds: float | None) -> float | None:
    if seconds is None:
        return None
    return time.monotonic() + float(seconds)


def _deadline_reached(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _call_with_timeout(fn, timeout: float):
    """Run fn on a daemon thread. Raise TimeoutError if it does not finish."""
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:
            box["error"] = exc

    thread = threading.Thread(target=_run, name="yahoo-history", daemon=True)
    thread.start()
    thread.join(max(0.1, float(timeout)))
    if thread.is_alive():
        raise TimeoutError(f"Yahoo history exceeded {timeout:.0f}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def yahoo_history(symbol: str, *, timeout: float | None = None, **kwargs):
    """yfinance history() with a hard wall-clock cap. Raises on failure."""
    import yfinance as yf

    if timeout is None:
        timeout = _YF_TIMEOUT_SEC
    kwargs.setdefault("timeout", timeout)

    def _call():
        try:
            return yf.Ticker(symbol).history(**kwargs)
        except TypeError:
            kwargs.pop("timeout", None)
            return yf.Ticker(symbol).history(**kwargs)

    return _call_with_timeout(_call, float(timeout) + 1.0)


def _fetch_volume_series(symbol: str, *, lookback_days: int) -> pd.Series | None:
    sym = config.normalize_symbol(symbol)
    if not sym:
        return None
    cached = _cache_get(sym)
    if cached is not None:
        return cached
    try:
        period_days = max(lookback_days + 15, 30)
        hist = yahoo_history(sym, period=f"{period_days}d", auto_adjust=False)
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


def calculate_rvol(data, symbol: str, lookback_days: int = 10) -> float | None:
    """Current session volume vs average of prior *lookback_days* daily bars."""
    del data  # close matrix has no volume; kept for API compatibility
    if not symbol:
        return None
    lookback = max(2, int(lookback_days or getattr(config, "RVOL_LOOKBACK_DAYS", 10)))
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
    *,
    deadline_sec: float | None = RVOL_SCAN_BUDGET_SEC,
) -> list[dict[str, Any]]:
    """Rank symbols in *data* columns by RVOL (desc)."""
    min_rvol = float(min_rvol)
    limit = max(1, int(limit))
    if deadline_sec is None:
        deadline_sec = RVOL_SCAN_BUDGET_SEC
    # Prefer momentum universe (capped) — same source as ORB/catalyst scanners.
    symbols = nyse_scan_universe(data, limit_scan=max(80, limit * 4))
    deadline = _deadline_at(deadline_sec)
    scored: list[tuple[float, str]] = []
    for sym in symbols:
        if _deadline_reached(deadline):
            break
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
    deadline_sec: float | None = RVOL_SCAN_BUDGET_SEC,
    deadline: float | None = None,
) -> list[str]:
    """Drop names whose measured RVOL is below *min_rvol*.

    Three outcomes stay distinct:
    - measured RVOL at or above the threshold: kept
    - measured RVOL below the threshold: dropped
    - unknown RVOL (timeout, delisted, empty history, or budget spent
      before this name): kept, in place

    Keeping an unknown name does not record it as a confirmed pass. The
    re-rank leaves those names unscored.
    """
    if not config.effective_rvol_scanner_enabled():
        return list(symbols)
    threshold = float(min_rvol if min_rvol is not None else config.RVOL_MIN_THRESHOLD)
    if deadline is None:
        deadline = _deadline_at(deadline_sec)
    out: list[str] = []
    for i, sym in enumerate(symbols):
        if _deadline_reached(deadline):
            out.extend(symbols[i:])
            break
        rvol = calculate_rvol(data, sym)
        if rvol is None or rvol >= threshold:
            out.append(sym)
    return out


def prioritize_symbols_by_rvol(
    symbols: list[str],
    data,
    *,
    deadline_sec: float | None = RVOL_SCAN_BUDGET_SEC,
    deadline: float | None = None,
) -> list[str]:
    """Measured RVOL first, high to low. Unknown names stay at the end.

    A timeout and a name not reached before *deadline* are unknown. They
    keep their relative order and are not given a rank as if they had passed.
    *deadline* is an absolute monotonic timestamp shared with the filter.
    """
    if not symbols:
        return []
    if deadline is None:
        if deadline_sec is None:
            deadline_sec = RVOL_SCAN_BUDGET_SEC
        deadline = _deadline_at(deadline_sec)
    scored: list[tuple[float, str]] = []
    unknown: list[str] = []
    for i, sym in enumerate(symbols):
        if _deadline_reached(deadline):
            unknown.extend(symbols[i:])
            break
        rvol = calculate_rvol(data, sym)
        if rvol is None:
            unknown.append(sym)
        else:
            scored.append((rvol, sym))
    scored.sort(reverse=True)
    return [s for _, s in scored] + unknown


def apply_rvol_universe_boost(
    symbols: list[str],
    data,
    *,
    deadline_sec: float | None = RVOL_SCAN_BUDGET_SEC,
) -> list[str]:
    """Filter low-RVOL names, then re-rank, on one shared scan budget.

    The re-rank does not start a second 45s clock. If the filter spends the
    budget, prioritization does not call Yahoo again.

    Existing empty-sleeve fallback: if every name that was actually measured
    is below the threshold, the original list is restored. That avoids an
    empty momentum sleeve. It does not mark those names as RVOL passes.
    """
    if deadline_sec is None:
        deadline_sec = RVOL_SCAN_BUDGET_SEC
    deadline = _deadline_at(deadline_sec)
    cols = filter_symbols_by_rvol(symbols, data, deadline=deadline)
    if not cols and symbols:
        cols = list(symbols)
    return prioritize_symbols_by_rvol(cols, data, deadline=deadline)


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


def rvol_dashboard_rows(
    data=None, *, limit: int = 10, deadline_sec: float | None = None
) -> list[dict[str, str]]:
    if data is None:
        from modules.pipeline_strategies import load_pipeline_data

        data = load_pipeline_data()
    try:
        from modules.orb_strategy import combined_scanner_dashboard_rows

        return combined_scanner_dashboard_rows(
            data, limit=limit, deadline_sec=deadline_sec
        )
    except Exception as exc:
        logger.debug("combined scanner dashboard rows unavailable, using RVOL fallback: %s", exc)
    top = get_high_rvol_stocks(
        data,
        min_rvol=config.RVOL_MIN_THRESHOLD,
        limit=limit,
        deadline_sec=deadline_sec,
    )
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
