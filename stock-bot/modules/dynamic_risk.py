"""Profit-based risk adjustment (winners treatment) for paper aggressive."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import Any

import config

logger = logging.getLogger(__name__)

PROFIT_PROTECT_WINNER_GAIN_PCT = float(
    os.getenv("PROFIT_PROTECT_WINNER_GAIN_PCT", "0.15")
)
PROFIT_PROTECT_WINNER_RISK_MULT = float(
    os.getenv("PROFIT_PROTECT_WINNER_RISK_MULT", "0.6")
)
PROFIT_PROTECT_PORTFOLIO_YTD_PCT = float(
    os.getenv("PROFIT_PROTECT_PORTFOLIO_YTD_PCT", "0.20")
)
PROFIT_PROTECT_PORTFOLIO_RISK_MULT = float(
    os.getenv("PROFIT_PROTECT_PORTFOLIO_RISK_MULT", "0.75")
)
PROFIT_PROTECT_TRAIL_ARM_PCT = float(os.getenv("PROFIT_PROTECT_TRAIL_ARM_PCT", "0.20"))
PROFIT_PROTECT_TRAIL_PCT = float(os.getenv("PROFIT_PROTECT_TRAIL_PCT", "0.09"))
PROFIT_PROTECT_TRAIL_ENABLED = os.getenv(
    "PROFIT_PROTECT_TRAIL_ENABLED", "true"
).lower() in ("1", "true", "yes")


def profit_protect_enabled() -> bool:
    try:
        return bool(config.effective_paper_profit_protect_enabled())
    except AttributeError:
        return False


def _bar_year(bar_date: date | datetime | None) -> int:
    if bar_date is None:
        return datetime.now(timezone.utc).year
    if hasattr(bar_date, "year"):
        return int(bar_date.year)
    return datetime.now(timezone.utc).year


def update_profit_protect_context(
    *,
    equity: float,
    bar_date: date | datetime | None = None,
) -> dict[str, Any]:
    """Track YTD equity and portfolio-level risk multiplier in dynamic_risk context."""
    ctx = config._dynamic_risk_ctx
    year = _bar_year(bar_date)
    eq = float(equity)

    if ctx.get("ytd_year") != year:
        ctx["ytd_year"] = year
        ctx["ytd_start_equity"] = eq

    start = float(ctx.get("ytd_start_equity") or eq)
    ytd_ret = (eq - start) / start if start > 0 else 0.0
    ctx["portfolio_ytd_return"] = ytd_ret
    if profit_protect_enabled() and ytd_ret >= PROFIT_PROTECT_PORTFOLIO_YTD_PCT:
        ctx["portfolio_risk_mult"] = PROFIT_PROTECT_PORTFOLIO_RISK_MULT
        ctx["portfolio_risk_cut_active"] = True
    else:
        ctx["portfolio_risk_mult"] = 1.0
        ctx["portfolio_risk_cut_active"] = False
    return ctx


def portfolio_risk_multiplier() -> float:
    if not profit_protect_enabled():
        return 1.0
    try:
        return float(config._dynamic_risk_ctx.get("portfolio_risk_mult", 1.0))
    except AttributeError:
        return 1.0


def portfolio_ytd_return() -> float:
    try:
        return float(config._dynamic_risk_ctx.get("portfolio_ytd_return", 0.0))
    except AttributeError:
        return 0.0


def _position_pnl_pct(entry: float, current: float, qty: float) -> float:
    if entry <= 0 or current <= 0:
        return 0.0
    pnl = (current - entry) / entry
    return -pnl if qty < 0 else pnl


def _resolve_price(
    symbol: str,
    executor,
    *,
    prices=None,
    data=None,
    bar_idx: int | None = None,
    full_data=None,
) -> float | None:
    sym = config.normalize_symbol(symbol)
    if prices is not None:
        if isinstance(prices, dict) and sym in prices:
            val = prices[sym]
            if val is not None and float(val) > 0:
                return float(val)
    frame = full_data if full_data is not None else data
    if frame is not None and sym in getattr(frame, "columns", []):
        series = frame[sym]
        if bar_idx is not None:
            series = series.iloc[: bar_idx + 1]
        series = series.dropna()
        if len(series):
            return float(series.iloc[-1])
    if hasattr(executor, "prices") and executor.prices.get(sym) is not None:
        return float(executor.prices[sym])
    return None


def position_unrealized_gain_pct(
    symbol: str,
    executor,
    *,
    prices=None,
    data=None,
    bar_idx: int | None = None,
    full_data=None,
) -> float | None:
    """Return unrealized gain % for an open position, or None if flat."""
    sym = config.normalize_symbol(symbol)
    entry = qty = None
    current = _resolve_price(
        sym, executor, prices=prices, data=data, bar_idx=bar_idx, full_data=full_data
    )

    if hasattr(executor, "portfolio"):
        raw_qty = executor.portfolio.positions.get(sym)
        if raw_qty is None:
            for k, v in executor.portfolio.positions.items():
                if config.normalize_symbol(k) == sym:
                    raw_qty = v
                    break
        if raw_qty is None or float(raw_qty) <= 0:
            return None
        qty = float(raw_qty)
        pos = executor._find_position(sym) if hasattr(executor, "_find_position") else None
        if pos is not None:
            entry = float(pos.avg_entry_price or 0)
            if current is None:
                current = float(pos.current_price or 0)
    else:
        try:
            for pos in executor._get_positions():
                if config.normalize_symbol(pos.symbol) != sym:
                    continue
                qty = float(pos.qty)
                if qty <= 0:
                    return None
                entry = float(pos.avg_entry_price or 0)
                if current is None:
                    current = float(pos.current_price or 0)
                break
        except Exception:
            return None

    if entry is None or qty is None or current is None or entry <= 0 or current <= 0:
        return None
    return _position_pnl_pct(entry, current, qty)


def position_winner_risk_mult(
    symbol: str,
    executor,
    *,
    prices=None,
    data=None,
    bar_idx: int | None = None,
    full_data=None,
) -> float:
    """0.6× risk for positions with unrealized gain ≥ +15%."""
    if not profit_protect_enabled():
        return 1.0
    gain = position_unrealized_gain_pct(
        symbol,
        executor,
        prices=prices,
        data=data,
        bar_idx=bar_idx,
        full_data=full_data,
    )
    if gain is not None and gain >= PROFIT_PROTECT_WINNER_GAIN_PCT:
        stats = getattr(executor, "profit_protect_stats", None)
        if isinstance(stats, dict):
            stats["winner_risk_cuts"] = stats.get("winner_risk_cuts", 0) + 1
        return PROFIT_PROTECT_WINNER_RISK_MULT
    return 1.0


def scale_notional_for_winner_protect(
    symbol: str,
    notional: float | None,
    executor,
    *,
    prices=None,
    data=None,
    bar_idx: int | None = None,
    full_data=None,
) -> float | None:
    if notional is None:
        return None
    mult = position_winner_risk_mult(
        symbol,
        executor,
        prices=prices,
        data=data,
        bar_idx=bar_idx,
        full_data=full_data,
    )
    if mult >= 0.999:
        return notional
    return round(float(notional) * mult, 2)


def _state_map(executor) -> dict:
    if not hasattr(executor, "_profit_protect_state"):
        executor._profit_protect_state = {}
    return executor._profit_protect_state


def _stats(executor) -> dict:
    if not hasattr(executor, "profit_protect_stats"):
        executor.profit_protect_stats = {
            "exits": 0,
            "armed": 0,
            "portfolio_risk_cuts": 0,
            "winner_risk_cuts": 0,
        }
    return executor.profit_protect_stats


def _update_trailing_state(entry: float, current: float, row: dict) -> dict:
    pnl = _position_pnl_pct(entry, current, 1.0)
    if not row.get("armed") and pnl >= PROFIT_PROTECT_TRAIL_ARM_PCT:
        row["armed"] = True
        row["hwm"] = float(current)
    if row.get("armed"):
        row["hwm"] = max(float(row.get("hwm") or current), float(current))
    row["entry"] = float(entry)
    return row


def _trailing_stop_hit(row: dict, current: float) -> bool:
    if not row.get("armed"):
        return False
    hwm = float(row.get("hwm") or current)
    if hwm <= 0:
        return False
    floor = hwm * (1.0 - PROFIT_PROTECT_TRAIL_PCT)
    return float(current) <= floor


def _prune_state(executor) -> None:
    held = set()
    if hasattr(executor, "portfolio"):
        for sym, qty in executor.portfolio.positions.items():
            if float(qty) > 0:
                held.add(config.normalize_symbol(sym))
    else:
        try:
            for pos in executor._get_positions():
                if float(pos.qty) > 0:
                    held.add(config.normalize_symbol(pos.symbol))
        except Exception:
            return
    stale = [s for s in _state_map(executor) if s not in held]
    for s in stale:
        _state_map(executor).pop(s, None)


def _is_eligible_symbol(symbol: str, *, data=None, bar_idx: int | None = None) -> bool:
    from modules.profit_target import is_eligible_symbol

    return is_eligible_symbol(symbol, data=data, bar_idx=bar_idx)


def run_profit_protect_exits(
    executor,
    *,
    risk_manager=None,
    journal=None,
    equity_session_open=True,
    now=None,
    bar_idx: int | None = None,
    full_data=None,
    journal_path=None,
) -> int:
    """Optional trailing stop for high-profit positions (+20% arm, ~9% trail)."""
    if not profit_protect_enabled() or not PROFIT_PROTECT_TRAIL_ENABLED:
        return 0

    exits = 0
    stats = _stats(executor)
    _prune_state(executor)

    if hasattr(executor, "portfolio"):
        items = []
        for sym, qty in executor.portfolio.positions.items():
            if float(qty) <= 0:
                continue
            if not equity_session_open and not config.is_crypto(sym):
                continue
            pos = executor._find_position(sym)
            if pos is None:
                continue
            items.append(pos)
    else:
        if not equity_session_open:
            return 0
        try:
            items = [p for p in executor._get_positions() if float(p.qty) > 0]
        except Exception as exc:
            logger.warning("profit_protect: failed to get positions: %s", exc)
            return 0

    for pos in items:
        symbol = config.normalize_symbol(pos.symbol)
        if not _is_eligible_symbol(symbol, data=full_data, bar_idx=bar_idx):
            continue

        entry = float(pos.avg_entry_price or 0)
        current = float(pos.current_price or 0)
        if hasattr(executor, "prices") and executor.prices.get(pos.symbol) is not None:
            current = float(executor.prices.get(pos.symbol))
        if entry <= 0 or current <= 0:
            continue

        row = _state_map(executor).get(symbol, {})
        prev_armed = bool(row.get("armed"))
        row = _update_trailing_state(entry, current, row)
        _state_map(executor)[symbol] = row
        if row.get("armed") and not prev_armed:
            stats["armed"] = stats.get("armed", 0) + 1

        if not _trailing_stop_hit(row, current):
            continue

        try:
            order = executor.execute_full_exit(
                symbol,
                reason="profit_protect_trailing",
                sleeve="profit_protect",
            )
        except TypeError:
            order = executor.execute_full_exit(symbol)
        if order is None:
            continue
        if hasattr(executor, "order_filled") and not executor.order_filled(order):
            continue
        if isinstance(order, dict) and not order:
            continue

        exits += 1
        stats["exits"] = stats.get("exits", 0) + 1
        pnl = _position_pnl_pct(entry, current, float(pos.qty))
        _state_map(executor).pop(symbol, None)
        if risk_manager is not None:
            risk_manager._log_event(
                f"PROFIT PROTECT EXIT: {symbol} pnl={pnl:.2%} hwm={row.get('hwm'):.4f}"
            )
        if journal is not None:
            try:
                eq = float(executor._get_account().equity)
            except Exception:
                eq = 0.0
            journal.log_exit(
                symbol,
                "sell",
                f"profit_protect_trailing {pnl:.2%}",
                eq,
                journal_path=journal_path,
            )
        logger.info(
            "profit_protect exit %s pnl=%.2f%% trail=%.0f%%",
            symbol,
            pnl * 100,
            PROFIT_PROTECT_TRAIL_PCT * 100,
        )

    return exits


def format_profit_protect_report(stats: dict | None) -> str:
    if not stats:
        return "no profit-protect activity"
    return (
        f"exits {stats.get('exits', 0)} | "
        f"armed {stats.get('armed', 0)} | "
        f"portfolio cuts {stats.get('portfolio_risk_cuts', 0)} | "
        f"winner cuts {stats.get('winner_risk_cuts', 0)}"
    )
