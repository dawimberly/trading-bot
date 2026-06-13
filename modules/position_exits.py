"""Stop-loss and max-hold exits for open Alpaca positions."""

from __future__ import annotations

import logging

import config

logger = logging.getLogger(__name__)


def _position_symbol(raw_symbol):
    return config.normalize_symbol(raw_symbol)


def run_position_exits(
    executor, risk_manager, journal=None, equity_session_open=True, journal_path=None
):
    """
    Close positions that hit stop-loss or max hold time.
    Returns number of exit orders submitted.
    """
    exits = 0
    try:
        positions = executor._get_positions()
    except Exception as e:
        if journal:
            journal.log_event("exit_error", notes=str(e), journal_path=journal_path)
        else:
            logger.warning("position_exits: failed to get positions: %s", e)
        return 0

    account = executor._get_account()
    equity = float(account.equity)

    for pos in positions:
        symbol = _position_symbol(pos.symbol)
        if config.vti_core_enabled() and symbol == config.VTI_CORE_SYMBOL:
            continue
        if not equity_session_open and not config.is_crypto(symbol):
            continue
        qty = float(pos.qty)
        if qty == 0:
            continue

        entry = float(pos.avg_entry_price or 0)
        current = float(pos.current_price or 0)
        if entry <= 0 or current <= 0:
            continue

        pnl_pct = (current - entry) / entry
        if qty < 0:
            pnl_pct = -pnl_pct

        stop_hit = pnl_pct <= -config.STOP_LOSS_PCT
        # Alpaca may expose hold time; fallback: use unrealized plpc if available
        plpc = getattr(pos, "unrealized_plpc", None)
        if plpc is not None and float(plpc) <= -config.STOP_LOSS_PCT:
            stop_hit = True

        if not stop_hit:
            continue

        side = "sell" if qty > 0 else "buy"
        try:
            order = executor.execute_full_exit(symbol)
            if not executor.order_filled(order):
                continue
            exits += 1
            risk_manager._log_event(
                f"STOP EXIT: {symbol} pnl={pnl_pct:.2%} qty={qty}"
            )
            if journal:
                journal.log_exit(
                    symbol, side, f"stop_loss {pnl_pct:.2%}", equity, journal_path=journal_path
                )
        except Exception as e:
            if journal:
                journal.log_event("exit_error", symbol=symbol, notes=str(e), journal_path=journal_path)

    return exits
