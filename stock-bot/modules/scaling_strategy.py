"""Partial take-profits + dip rebuys (backtest compare only — not live/paper bots)."""

from __future__ import annotations

import logging
import os
from typing import Any

import config

logger = logging.getLogger(__name__)

TAKE_LEVELS = tuple(
    float(x)
    for x in os.getenv("SCALING_TAKE_LEVELS", "0.04,0.08,0.12").split(",")
    if x.strip()
)
TAKE_FRACTION = float(os.getenv("SCALING_TAKE_FRACTION", "0.40"))
REBUY_PULLBACK = float(os.getenv("SCALING_REBUY_PULLBACK", "0.04"))
REBUY_LIMIT_BPS = float(os.getenv("SCALING_REBUY_LIMIT_BPS", "20"))
MAX_ROUND_TRIPS_WEEK = int(os.getenv("SCALING_MAX_ROUND_TRIPS_WEEK", "3"))
BARS_PER_WEEK = int(os.getenv("SCALING_BARS_PER_WEEK", "5"))
SPECULATIVE_SYMBOLS = frozenset(
    s.strip().upper()
    for s in os.getenv(
        "SCALING_SPECULATIVE_SYMBOLS", "SPCX,COIN,PLTR,SMCI,KTOS"
    ).split(",")
    if s.strip()
)
SPECULATIVE_SIZE_MULT = float(os.getenv("SCALING_SPECULATIVE_SIZE_MULT", "0.50"))


def scaling_strategy_enabled() -> bool:
    try:
        return bool(
            config.backtest_paper_sleeves_context()
            and getattr(config, "PAPER_SCALING_STRATEGY_ENABLED", False)
        )
    except AttributeError:
        return False


def _book(executor):
    """Persistent book — backtest recreates executor each bar; state lives on portfolio."""
    return getattr(executor, "portfolio", executor)


def _state_map(executor) -> dict[str, dict]:
    book = _book(executor)
    if not hasattr(book, "_scaling_state"):
        book._scaling_state = {}
    return book._scaling_state


def _stats(executor) -> dict[str, Any]:
    book = _book(executor)
    if not hasattr(book, "scaling_strategy_stats"):
        book.scaling_strategy_stats = {
            "partial_sells": 0,
            "rebuys": 0,
            "round_trips": 0,
            "blocked_buys": 0,
            "blocked_weekly_cap": 0,
            "by_symbol": {},
        }
    return book.scaling_strategy_stats


def _sym_stats(stats: dict, symbol: str) -> dict:
    sym = config.normalize_symbol(symbol)
    by = stats.setdefault("by_symbol", {})
    if sym not in by:
        by[sym] = {
            "partial_sells": 0,
            "rebuys": 0,
            "round_trips": 0,
            "blocked_buys": 0,
        }
    return by[sym]


def _position_pnl_pct(entry: float, current: float) -> float:
    if entry <= 0 or current <= 0:
        return 0.0
    return (current - entry) / entry


def _week_key(bar_idx: int) -> int:
    return int(bar_idx) // max(BARS_PER_WEEK, 1)


def _row(executor, symbol: str, bar_idx: int) -> dict:
    sym = config.normalize_symbol(symbol)
    st = _state_map(executor)
    row = st.get(sym)
    wk = _week_key(bar_idx)
    if row is None or row.get("week_key") != wk:
        row = {
            "week_key": wk,
            "round_trips": 0,
            "levels_taken": set(),
            "hwm": 0.0,
            "pending_rebuy": False,
            "rebuy_anchor": 0.0,
            "partial_sold_this_week": False,
        }
        st[sym] = row
    return row


def is_eligible_symbol(symbol: str, *, data=None, bar_idx: int | None = None) -> bool:
    sym = config.normalize_symbol(symbol)
    if sym == config.VTI_CORE_SYMBOL:
        return False
    if config.is_crypto(sym) or config.is_metal_symbol(sym):
        return False
    if sym != config.SPY_BOT_SYMBOL and not config._nyse_eligible_symbol(sym):
        return False
    try:
        from modules.dynamic_universe import ipo_safety_enabled, is_ipo_symbol

        if ipo_safety_enabled() and is_ipo_symbol(sym, data=data, bar_idx=bar_idx):
            return False
    except ImportError:
        pass
    return True


def apply_speculative_size_mult(symbol: str, notional: float) -> float:
    if not scaling_strategy_enabled():
        return notional
    sym = config.normalize_symbol(symbol)
    if sym in SPECULATIVE_SYMBOLS:
        return round(float(notional) * SPECULATIVE_SIZE_MULT, 2)
    return notional


def scaling_strategy_blocks_buy(executor, symbol: str, bar_idx: int) -> bool:
    if not scaling_strategy_enabled():
        return False
    sym = config.normalize_symbol(symbol)
    row = _row(executor, sym, bar_idx)
    if row["round_trips"] >= MAX_ROUND_TRIPS_WEEK:
        _stats(executor)["blocked_weekly_cap"] = (
            _stats(executor).get("blocked_weekly_cap", 0) + 1
        )
        _sym_stats(_stats(executor), sym)["blocked_buys"] += 1
        return True
    if row.get("pending_rebuy"):
        _stats(executor)["blocked_buys"] = _stats(executor).get("blocked_buys", 0) + 1
        _sym_stats(_stats(executor), sym)["blocked_buys"] += 1
        return True
    return False


def _sell_fraction(executor, pos, fraction: float, *, reason: str, bar_idx: int) -> bool:
    price = float(pos.current_price or 0)
    if price <= 0:
        return False
    notional = float(pos.qty) * price * fraction
    if notional < 1:
        return False
    order = executor.execute_reduce_notional(
        pos.symbol,
        notional,
        reason=reason,
        sleeve="scaling",
    )
    if order is None:
        return False
    if hasattr(executor, "order_filled") and not executor.order_filled(order):
        return False
    stats = _stats(executor)
    stats["partial_sells"] = stats.get("partial_sells", 0) + 1
    sym = config.normalize_symbol(pos.symbol)
    ss = _sym_stats(stats, sym)
    ss["partial_sells"] += 1
    row = _row(executor, sym, bar_idx)
    row["pending_rebuy"] = True
    row["rebuy_anchor"] = max(float(row.get("rebuy_anchor") or 0), price)
    row["partial_sold_this_week"] = True
    return True


def _try_rebuy(executor, symbol: str, price: float, bar_idx: int) -> bool:
    sym = config.normalize_symbol(symbol)
    row = _row(executor, sym, bar_idx)
    if not row.get("pending_rebuy"):
        return False
    anchor = float(row.get("rebuy_anchor") or price)
    if anchor <= 0:
        return False
    trigger = anchor * (1.0 - REBUY_PULLBACK)
    if price > trigger:
        return False
    limit_px = trigger * (1.0 - REBUY_LIMIT_BPS / 10000.0)
    fill_px = min(price, limit_px) if price <= trigger else price
    if fill_px <= 0:
        return False
    eq = 0.0
    if hasattr(executor, "portfolio") and hasattr(executor, "prices"):
        eq = float(executor.portfolio.equity(executor.prices))
    min_n = config.effective_min_notional(eq)
    notional = None
    if hasattr(executor, "compute_nyse_notional"):
        notional = executor.compute_nyse_notional()
    if notional is None:
        notional = max(min_n * 2, min_n)
    notional = apply_speculative_size_mult(sym, float(notional))
    if notional < min_n:
        return False
    order = executor.execute_order(
        sym,
        "buy",
        notional=round(notional, 2),
        reason="scaling_dip_rebuy",
        sleeve="NYSE",
    )
    if order is None:
        return False
    if hasattr(executor, "order_filled") and not executor.order_filled(order):
        return False
    stats = _stats(executor)
    stats["rebuys"] = stats.get("rebuys", 0) + 1
    ss = _sym_stats(stats, sym)
    ss["rebuys"] += 1
    if row.get("partial_sold_this_week"):
        stats["round_trips"] = stats.get("round_trips", 0) + 1
        ss["round_trips"] += 1
        row["round_trips"] = row.get("round_trips", 0) + 1
        row["partial_sold_this_week"] = False
    row["pending_rebuy"] = False
    row["rebuy_anchor"] = 0.0
    row["levels_taken"] = set()
    return True


def run_scaling_strategy(
    executor,
    *,
    bar_idx: int | None = None,
    full_data=None,
    equity_session_open: bool = True,
) -> int:
    """Partial profit takes + limit-style dip rebuys on NYSE/SPY longs."""
    if not scaling_strategy_enabled() or not equity_session_open:
        return 0

    actions = 0
    stats = _stats(executor)

    if hasattr(executor, "portfolio"):
        items = []
        for sym, qty in executor.portfolio.positions.items():
            if float(qty) <= 0:
                continue
            pos = executor._find_position(sym)
            if pos is not None:
                items.append(pos)
    else:
        return 0

    for pos in items:
        sym = config.normalize_symbol(pos.symbol)
        if not is_eligible_symbol(sym, data=full_data, bar_idx=bar_idx):
            continue
        entry = float(pos.avg_entry_price or 0)
        current = float(pos.current_price or 0)
        if hasattr(executor, "prices") and executor.prices.get(pos.symbol) is not None:
            current = float(executor.prices.get(pos.symbol))
        if entry <= 0 or current <= 0:
            continue

        row = _row(executor, sym, bar_idx or 0)
        row["hwm"] = max(float(row.get("hwm") or 0), current)
        pnl = _position_pnl_pct(entry, current)

        for level in TAKE_LEVELS:
            if level in row["levels_taken"]:
                continue
            if pnl < level:
                continue
            if _sell_fraction(
                executor,
                pos,
                TAKE_FRACTION,
                reason=f"scaling_take_{level:.0%}",
                bar_idx=bar_idx or 0,
            ):
                row["levels_taken"].add(level)
                actions += 1
                pos = executor._find_position(sym)
                if pos is None or float(pos.qty) <= 0:
                    break

        if _try_rebuy(executor, sym, current, bar_idx or 0):
            actions += 1

    return actions


def reset_scaling_stats() -> None:
    pass


def format_symbol_report(stats: dict, symbol: str) -> str:
    sym = config.normalize_symbol(symbol)
    row = (stats or {}).get("by_symbol", {}).get(sym) or {}
    if not row:
        return f"{sym}: no scaling activity"
    return (
        f"{sym}: partial sells {row.get('partial_sells', 0)} | "
        f"rebuys {row.get('rebuys', 0)} | "
        f"round trips {row.get('round_trips', 0)} | "
        f"blocked buys {row.get('blocked_buys', 0)}"
    )
