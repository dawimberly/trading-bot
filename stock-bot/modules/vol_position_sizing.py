"""Top 1% style volatility-based + asymmetric conviction position sizing (research/paper)."""

from __future__ import annotations

import logging
import os
from typing import Any

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)

TOP1_BASE_RISK_PCT = float(os.getenv("TOP1_BASE_RISK_PCT", "0.01"))
TOP1_SPECULATIVE_MAX_RISK_PCT = float(os.getenv("TOP1_SPECULATIVE_MAX_RISK_PCT", "0.005"))
TOP1_MAX_CONVICTION_MULT = float(os.getenv("TOP1_MAX_CONVICTION_MULT", "2.0"))
TOP1_PORTFOLIO_HEAT_MAX = float(os.getenv("TOP1_PORTFOLIO_HEAT_MAX", "0.07"))
TOP1_ATR_BASELINE = float(os.getenv("TOP1_ATR_BASELINE", "0.025"))
TOP1_MIN_VOL_SCALE = float(os.getenv("TOP1_MIN_VOL_SCALE", "0.50"))
TOP1_MAX_VOL_SCALE = float(os.getenv("TOP1_MAX_VOL_SCALE", "1.50"))
TOP1_SPECULATIVE_PRICE = float(os.getenv("TOP1_SPECULATIVE_PRICE", "8.0"))
TOP1_SPECULATIVE_MIN_VOLUME = int(os.getenv("TOP1_SPECULATIVE_MIN_VOLUME", "500000"))
TOP1_SPECULATIVE_SYMBOLS = frozenset(
    s.strip().upper()
    for s in os.getenv(
        "TOP1_SPECULATIVE_SYMBOLS", "SPCX,COIN,PLTR,SMCI,KTOS,HOOD,RIVN"
    ).split(",")
    if s.strip()
)
TOP1_ATR_LOOKBACK = int(os.getenv("TOP1_ATR_LOOKBACK", "14"))
TOP1_CONSERVATIVE_MIN_VOL_SCALE = float(os.getenv("TOP1_CONSERVATIVE_MIN_VOL_SCALE", "0.75"))
TOP1_CONSERVATIVE_MAX_VOL_SCALE = float(os.getenv("TOP1_CONSERVATIVE_MAX_VOL_SCALE", "1.25"))

_vol_sizing_conservative_ctx = False


def set_vol_sizing_conservative(enabled: bool) -> None:
    global _vol_sizing_conservative_ctx
    _vol_sizing_conservative_ctx = bool(enabled)


def vol_sizing_conservative_mode() -> bool:
    if _vol_sizing_conservative_ctx:
        return True
    try:
        return bool(config.effective_top1_vol_conservative())
    except AttributeError:
        pass
    return os.getenv("TOP1_VOL_SIZING_CONSERVATIVE", "").lower() in ("1", "true", "yes")


def vol_position_sizing_enabled() -> bool:
    try:
        return bool(config.effective_vol_position_sizing_enabled())
    except AttributeError:
        return False


def _book(executor) -> Any:
    return getattr(executor, "portfolio", executor)


def _stats(executor) -> dict[str, Any]:
    book = _book(executor)
    if not hasattr(book, "vol_position_sizing_stats"):
        book.vol_position_sizing_stats = {
            "sized_buys": 0,
            "speculative_caps": 0,
            "conviction_boosts": 0,
            "heat_blocks": 0,
            "by_symbol": {},
            "portfolio_heat_samples": [],
        }
    return book.vol_position_sizing_stats


def _sym_stats(stats: dict, symbol: str) -> dict:
    sym = config.normalize_symbol(symbol)
    row = stats.setdefault("by_symbol", {}).setdefault(
        sym,
        {
            "buys": 0,
            "speculative": 0,
            "boosted": 0,
            "avg_risk_pct": 0.0,
        },
    )
    return row


def _held_symbols(book) -> set[str]:
    if hasattr(book, "positions"):
        return {
            config.normalize_symbol(sym)
            for sym, qty in book.positions.items()
            if float(qty) > 0
        }
    if hasattr(book, "_get_positions"):
        try:
            return {
                config.normalize_symbol(pos.symbol)
                for pos in book._get_positions()
                if float(pos.qty) > 0
            }
        except Exception:
            return set()
    return set()


def _risk_map(portfolio) -> dict[str, float]:
    if not hasattr(portfolio, "_top1_risk_by_symbol"):
        portfolio._top1_risk_by_symbol = {}
    risk = portfolio._top1_risk_by_symbol
    held = _held_symbols(portfolio)
    for sym in list(risk):
        if sym not in held:
            del risk[sym]
    return risk


def portfolio_heat_pct(executor) -> float:
    book = _book(executor)
    return float(sum(_risk_map(book).values()))


def set_top1_sizing_context(executor, thinking: dict | None) -> None:
    if thinking is None:
        executor._top1_sizing_ctx = {}
        return
    executor._top1_sizing_ctx = {
        "thinking_confidence": float(thinking.get("confidence") or 0.72),
        "market_summary": thinking.get("market_summary") or {},
        "suggested_tilt": thinking.get("suggested_tilt") or {},
    }


def _atr_pct_from_close(close: pd.Series, window: int = TOP1_ATR_LOOKBACK) -> float:
    if close is None or len(close) < window + 2:
        return TOP1_ATR_BASELINE
    series = close.dropna().astype(float)
    if len(series) < window + 2:
        return TOP1_ATR_BASELINE
    prev = series.shift(1)
    tr = (series - prev).abs()
    atr = tr.rolling(window).mean().iloc[-1]
    price = float(series.iloc[-1])
    if not np.isfinite(atr) or price <= 0:
        return TOP1_ATR_BASELINE
    return float(atr / price)


def _price_and_volume(
    symbol: str,
    *,
    data=None,
    bar_idx: int | None = None,
    full_data=None,
    prices=None,
) -> tuple[float, float | None]:
    sym = config.normalize_symbol(symbol)
    frame = full_data if full_data is not None else data
    price = None
    if prices is not None:
        if isinstance(prices, pd.Series):
            if sym in prices.index:
                val = prices.get(sym)
                if val is not None and np.isfinite(val):
                    price = float(val)
        elif isinstance(prices, dict) and sym in prices:
            price = float(prices[sym])
    elif frame is not None and sym in getattr(frame, "columns", []):
        series = frame[sym]
        if bar_idx is not None:
            series = series.iloc[: bar_idx + 1]
        series = series.dropna()
        if len(series):
            price = float(series.iloc[-1])
    if price is None or price <= 0:
        return 0.0, None
    avg_vol = None
    try:
        from modules.dynamic_universe import load_screener_ticker_meta

        meta = load_screener_ticker_meta().get(sym, {})
        if meta.get("avg_volume") is not None:
            avg_vol = float(meta["avg_volume"])
    except Exception:
        pass
    return price, avg_vol


def is_speculative_name(
    symbol: str,
    *,
    data=None,
    bar_idx: int | None = None,
    full_data=None,
    prices=None,
) -> tuple[bool, str]:
    sym = config.normalize_symbol(symbol)
    price, avg_vol = _price_and_volume(
        sym, data=data, bar_idx=bar_idx, full_data=full_data, prices=prices
    )
    if sym in TOP1_SPECULATIVE_SYMBOLS:
        return True, "watchlist hype name"
    try:
        from modules.dynamic_universe import is_ipo_symbol

        if is_ipo_symbol(sym, data=full_data or data, bar_idx=bar_idx):
            return True, "recent IPO window"
    except Exception:
        pass
    if 0 < price < TOP1_SPECULATIVE_PRICE:
        return True, f"price ${price:.2f} < ${TOP1_SPECULATIVE_PRICE:.0f}"
    if avg_vol is not None and avg_vol < TOP1_SPECULATIVE_MIN_VOLUME:
        return True, f"avg vol {avg_vol/1e3:.0f}k below floor"
    return False, ""


def vol_scale_from_atr(atr_pct: float, *, conservative: bool = False) -> float:
    atr = max(float(atr_pct), 1e-4)
    raw = TOP1_ATR_BASELINE / atr
    lo = TOP1_CONSERVATIVE_MIN_VOL_SCALE if conservative else TOP1_MIN_VOL_SCALE
    hi = TOP1_CONSERVATIVE_MAX_VOL_SCALE if conservative else TOP1_MAX_VOL_SCALE
    return float(max(lo, min(hi, raw)))


def conviction_score(
    symbol: str,
    executor,
    *,
    data=None,
    bar_idx: int | None = None,
) -> float:
    ctx = getattr(executor, "_top1_sizing_ctx", {}) or {}
    conf = float(ctx.get("thinking_confidence") or 0.72)
    score = max(0.0, min(1.0, (conf - 0.70) / 0.20))

    summary = ctx.get("market_summary") or {}
    try:
        from modules.sector_rotation import ticker_sector

        sym_sector = ticker_sector(symbol)
        leader_sectors = {
            str(r.get("sector", ""))
            for r in (summary.get("sector_leaders") or [])[:3]
        }
        if sym_sector in leader_sectors:
            score = min(1.0, score + 0.20)
    except Exception:
        pass

    if config.effective_pattern_awareness_enabled() and data is not None:
        try:
            from modules.chart_patterns import detect_patterns, pattern_score

            sym = config.normalize_symbol(symbol)
            if sym in data.columns:
                series = data[sym].dropna()
                if bar_idx is not None:
                    series = series.iloc[: bar_idx + 1]
                hits = detect_patterns(series, symbol=sym)
                ps = pattern_score(hits)
                if ps > 0.12:
                    score = min(1.0, score + min(0.25, ps))
        except Exception:
            pass

    return float(max(0.0, min(1.0, score)))


def compute_top1_risk_pct(
    symbol: str,
    executor,
    *,
    data=None,
    bar_idx: int | None = None,
    full_data=None,
    prices=None,
) -> tuple[float, dict[str, Any]]:
    sym = config.normalize_symbol(symbol)
    frame = full_data if full_data is not None else data
    atr_pct = TOP1_ATR_BASELINE
    if frame is not None and sym in getattr(frame, "columns", []):
        series = frame[sym]
        if bar_idx is not None:
            series = series.iloc[: bar_idx + 1]
        atr_pct = _atr_pct_from_close(series.dropna())

    conservative = vol_sizing_conservative_mode()
    vol_scale = vol_scale_from_atr(atr_pct, conservative=conservative)
    speculative, spec_reason = is_speculative_name(
        sym,
        data=data,
        bar_idx=bar_idx,
        full_data=full_data,
        prices=prices,
    )
    if conservative:
        conviction = 0.0
        conviction_mult = 1.0
        risk_pct = TOP1_BASE_RISK_PCT * vol_scale
        if speculative:
            risk_pct = min(risk_pct, TOP1_SPECULATIVE_MAX_RISK_PCT)
        else:
            risk_pct = min(risk_pct, TOP1_BASE_RISK_PCT)
    else:
        conviction = conviction_score(sym, executor, data=data, bar_idx=bar_idx)
        conviction_mult = 1.0 + conviction * (TOP1_MAX_CONVICTION_MULT - 1.0)
        risk_pct = TOP1_BASE_RISK_PCT * vol_scale * conviction_mult
        if speculative:
            risk_pct = min(risk_pct, TOP1_SPECULATIVE_MAX_RISK_PCT)
        risk_pct = min(risk_pct, TOP1_BASE_RISK_PCT * TOP1_MAX_CONVICTION_MULT)

    meta = {
        "atr_pct": round(atr_pct, 4),
        "vol_scale": round(vol_scale, 3),
        "conviction": round(conviction, 3),
        "conviction_mult": round(conviction_mult, 3),
        "speculative": speculative,
        "spec_reason": spec_reason,
        "risk_pct": round(risk_pct, 4),
    }
    return risk_pct, meta


def release_top1_risk_on_sell(portfolio, symbol: str, qty_before: float, qty_sold: float) -> None:
    if qty_before <= 0 or qty_sold <= 0:
        return
    risk = _risk_map(portfolio)
    sym = config.normalize_symbol(symbol)
    pct = risk.get(sym)
    if pct is None:
        return
    remaining = qty_before - qty_sold
    if remaining <= 1e-9:
        risk.pop(sym, None)
    else:
        risk[sym] = pct * (remaining / qty_before)


def apply_top1_nyse_notional(
    symbol: str,
    sleeve_notional: float | None,
    equity: float,
    executor,
    *,
    data=None,
    bar_idx: int | None = None,
    full_data=None,
) -> float | None:
    """Return NYSE buy notional using Top1 vol + conviction rules."""
    if not vol_position_sizing_enabled() or sleeve_notional is None:
        return sleeve_notional
    if equity <= 0:
        return sleeve_notional

    prices = getattr(executor, "prices", None)
    conservative = vol_sizing_conservative_mode()
    if conservative:
        speculative, _ = is_speculative_name(
            symbol,
            data=data,
            bar_idx=bar_idx,
            full_data=full_data,
            prices=prices,
        )
        if not speculative:
            return sleeve_notional

    risk_pct, meta = compute_top1_risk_pct(
        symbol,
        executor,
        data=data,
        bar_idx=bar_idx,
        full_data=full_data,
        prices=prices,
    )

    heat = portfolio_heat_pct(executor)
    if not conservative and heat + risk_pct > TOP1_PORTFOLIO_HEAT_MAX + 1e-9:
        room = max(0.0, TOP1_PORTFOLIO_HEAT_MAX - heat)
        stats = _stats(executor)
        stats["heat_blocks"] = stats.get("heat_blocks", 0) + 1
        if room < TOP1_SPECULATIVE_MAX_RISK_PCT * 0.5:
            logger.info(
                "Top1 sizing: portfolio heat %.1f%% - block %s (need %.2f%%, cap %.0f%%)",
                heat * 100,
                config.normalize_symbol(symbol),
                risk_pct * 100,
                TOP1_PORTFOLIO_HEAT_MAX * 100,
            )
            return None
        risk_pct = room
        meta["heat_trimmed"] = True

    target = round(float(equity) * risk_pct, 2)
    min_n = config.effective_min_notional(equity)
    max_n = config.effective_max_notional_per_order(equity)
    notional = min(float(sleeve_notional), target, max_n)
    if notional < min_n:
        return None

    book = _book(executor)
    _risk_map(book)[config.normalize_symbol(symbol)] = risk_pct

    stats = _stats(executor)
    stats["sized_buys"] = stats.get("sized_buys", 0) + 1
    sym_row = _sym_stats(stats, symbol)
    sym_row["buys"] += 1
    sym_row["avg_risk_pct"] = round(
        (sym_row["avg_risk_pct"] * (sym_row["buys"] - 1) + risk_pct) / sym_row["buys"],
        4,
    )
    if meta.get("speculative"):
        stats["speculative_caps"] = stats.get("speculative_caps", 0) + 1
        sym_row["speculative"] += 1
    if meta.get("conviction_mult", 1.0) > 1.15:
        stats["conviction_boosts"] = stats.get("conviction_boosts", 0) + 1
        sym_row["boosted"] += 1
    stats.setdefault("portfolio_heat_samples", []).append(round(heat + risk_pct, 4))

    logger.info(
        "Top1 sizing %s: risk %.2f%% (base 1%% x vol %.2f x conv %.2f) "
        "-> $%.0f notional%s%s",
        config.normalize_symbol(symbol),
        risk_pct * 100,
        meta["vol_scale"],
        meta["conviction_mult"],
        notional,
        f" | SPECULATIVE {meta['spec_reason']}" if meta.get("speculative") else "",
        f" | heat {heat*100:.1f}%->{(heat+risk_pct)*100:.1f}%"
        if meta.get("heat_trimmed")
        else "",
    )
    return round(notional, 2)


def format_vol_sizing_report(stats: dict | None) -> str:
    if not stats:
        return "no Top1 sizing activity"
    heat_samples = stats.get("portfolio_heat_samples") or []
    peak_heat = max(heat_samples) if heat_samples else 0.0
    by = stats.get("by_symbol") or {}
    spcx = by.get("SPCX", {})
    spcx_line = ""
    if spcx:
        spcx_line = (
            f" | SPCX: {spcx.get('buys', 0)} buys "
            f"avg risk {float(spcx.get('avg_risk_pct', 0))*100:.2f}% "
            f"spec={spcx.get('speculative', 0)}"
        )
    return (
        f"sized {stats.get('sized_buys', 0)} | "
        f"spec caps {stats.get('speculative_caps', 0)} | "
        f"conviction boosts {stats.get('conviction_boosts', 0)} | "
        f"heat blocks {stats.get('heat_blocks', 0)} | "
        f"peak heat {peak_heat*100:.1f}%"
        f"{spcx_line}"
    )
