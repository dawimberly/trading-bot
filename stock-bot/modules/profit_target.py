"""Trailing profit targets for normal NYSE/SPY longs (paper optional)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import config

logger = logging.getLogger(__name__)

PROFIT_TARGET_ARM_GAIN_PCT = float(
    __import__("os").getenv("PAPER_PROFIT_TARGET_ARM_GAIN", "0.25")
)
PROFIT_TARGET_TRAIL_PCT = float(
    __import__("os").getenv("PAPER_PROFIT_TARGET_TRAIL", "0.10")
)
PROFIT_TARGET_REBUY_COOLDOWN_DAYS = int(
    __import__("os").getenv("PAPER_PROFIT_TARGET_REBUY_DAYS", "7")
)
PROFIT_TARGET_REBUY_COOLDOWN_ENABLED = __import__("os").getenv(
    "PAPER_PROFIT_TARGET_REBUY_COOLDOWN", "true"
).lower() in ("1", "true", "yes")


def _state_map(executor) -> dict:
    if not hasattr(executor, "_profit_target_state"):
        executor._profit_target_state = {}
    return executor._profit_target_state


def _cooldown_map(executor) -> dict:
    if not hasattr(executor, "_profit_exit_cooldown"):
        executor._profit_exit_cooldown = {}
    return executor._profit_exit_cooldown


def _stats(executor) -> dict:
    if not hasattr(executor, "profit_target_stats"):
        executor.profit_target_stats = {
            "exits": 0,
            "armed": 0,
            "rebuy_blocks": 0,
        }
    return executor.profit_target_stats


def profit_target_enabled() -> bool:
    """Only active during explicit backtest compare (never live or paper bot)."""
    try:
        return bool(
            config.backtest_paper_sleeves_context() and config.PAPER_PROFIT_TARGET_ENABLED
        )
    except AttributeError:
        return False


def is_eligible_symbol(
    symbol: str, *, data=None, bar_idx: int | None = None
) -> bool:
    """Normal NYSE/SPY longs only — not IPO, crypto, VTI core, or metals."""
    sym = config.normalize_symbol(symbol)
    if sym == config.VTI_CORE_SYMBOL:
        return False
    if config.is_crypto(sym):
        return False
    if config.is_metal_symbol(sym):
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


def _position_pnl_pct(entry: float, current: float, qty: float) -> float:
    if entry <= 0 or current <= 0:
        return 0.0
    pnl = (current - entry) / entry
    return -pnl if qty < 0 else pnl


def _update_trailing_state(entry: float, current: float, row: dict) -> dict:
    pnl = _position_pnl_pct(entry, current, 1.0)
    if not row.get("armed") and pnl >= PROFIT_TARGET_ARM_GAIN_PCT:
        row["armed"] = True
        row["hwm"] = float(current)
    if row.get("armed"):
        row["hwm"] = max(float(row.get("hwm") or current), float(current))
    row["entry"] = float(entry)
    return row


def trailing_stop_hit(row: dict, current: float) -> bool:
    if not row.get("armed"):
        return False
    hwm = float(row.get("hwm") or current)
    if hwm <= 0:
        return False
    floor = hwm * (1.0 - PROFIT_TARGET_TRAIL_PCT)
    return float(current) <= floor


def record_profit_exit(executor, symbol: str, *, now=None, bar_idx: int | None = None) -> None:
    sym = config.normalize_symbol(symbol)
    _state_map(executor).pop(sym, None)
    if not PROFIT_TARGET_REBUY_COOLDOWN_ENABLED:
        return
    key = sym
    if bar_idx is not None:
        _cooldown_map(executor)[key] = {"bar_idx": int(bar_idx)}
    else:
        ts = now if isinstance(now, datetime) else datetime.now(timezone.utc)
        _cooldown_map(executor)[key] = {"ts": ts}


def profit_rebuy_blocked(
    executor,
    symbol: str,
    now,
    *,
    cooldown_bars: int | None = None,
) -> bool:
    if not profit_target_enabled() or not PROFIT_TARGET_REBUY_COOLDOWN_ENABLED:
        return False
    sym = config.normalize_symbol(symbol)
    row = _cooldown_map(executor).get(sym)
    if not row:
        return False
    if cooldown_bars is not None and "bar_idx" in row:
        blocked = (now - row["bar_idx"]) < cooldown_bars
    elif "ts" in row:
        ref = now if isinstance(now, datetime) else datetime.now(timezone.utc)
        if getattr(ref, "tzinfo", None) is None:
            ref = ref.replace(tzinfo=timezone.utc)
        ts = row["ts"]
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=timezone.utc)
        blocked = (ref - ts) < timedelta(days=PROFIT_TARGET_REBUY_COOLDOWN_DAYS)
    else:
        blocked = False
    if blocked:
        _stats(executor)["rebuy_blocks"] = _stats(executor).get("rebuy_blocks", 0) + 1
    return blocked


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


def run_profit_target_exits(
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
    """Exit eligible longs on 10% trailing stop after +25% gain."""
    if not profit_target_enabled():
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
            logger.warning("profit_target: failed to get positions: %s", exc)
            return 0

    for pos in items:
        symbol = config.normalize_symbol(pos.symbol)
        if not is_eligible_symbol(symbol, data=full_data, bar_idx=bar_idx):
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

        if not trailing_stop_hit(row, current):
            continue

        try:
            order = executor.execute_full_exit(
                symbol, reason="profit_trailing_stop", sleeve="profit_target"
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
        record_profit_exit(executor, symbol, now=now, bar_idx=bar_idx)
        if risk_manager is not None:
            risk_manager._log_event(
                f"PROFIT TRAIL EXIT: {symbol} pnl={pnl:.2%} hwm={row.get('hwm'):.4f}"
            )
        if journal is not None:
            try:
                eq = float(executor._get_account().equity)
            except Exception:
                eq = 0.0
            exit_kw = {}
            try:
                if config.effective_paper_momentum_quality_fixes():
                    exit_kw["exit_reason"] = "take_profit"
                    from modules.position_exits import _position_entry_hour_et

                    exit_kw["entry_hour"] = _position_entry_hour_et(pos)
            except Exception as exc:
                logger.debug("profit target soft-fail: %s", exc)
            journal.log_exit(
                symbol,
                "sell",
                f"profit_trailing {pnl:.2%}",
                eq,
                journal_path=journal_path,
                **exit_kw,
            )
        logger.info(
            "profit_target exit %s pnl=%.2f%% trail=%.0f%%",
            symbol,
            pnl * 100,
            PROFIT_TARGET_TRAIL_PCT * 100,
        )

    return exits
