"""Sync Alpaca holdings with local ledger and trim sleeves to fund caps."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime

import config
from modules.alpaca_executor import AlpacaExecutor


def normalize_symbol(symbol: str) -> str:
    return config.normalize_symbol(symbol)


def _position_value(pos) -> float:
    mv = getattr(pos, "market_value", None)
    if mv is not None:
        return abs(float(mv))
    return abs(float(pos.qty) * float(pos.current_price or 0))


def holdings_audit(executor: AlpacaExecutor) -> dict:
    account = executor.client.get_account()
    equity = float(account.equity)
    sleeves = executor.sleeve_snapshot()
    positions = executor.client.get_all_positions()
    return {
        "equity": equity,
        "cash": float(account.cash),
        "positions": [
            {
                "symbol": p.symbol,
                "universe": normalize_symbol(p.symbol),
                "qty": float(p.qty),
                "value": round(_position_value(p), 2),
            }
            for p in positions
        ],
        "sleeves": sleeves,
        "over_cap": {
            "spy": max(0.0, sleeves["spy_value"] - sleeves["spy_cap"]),
            "crypto": max(0.0, sleeves["crypto_value"] - sleeves["crypto_cap"]),
            "nyse": max(0.0, sleeves["nyse_value"] - sleeves["nyse_cap"]),
        },
    }


def rebuild_ledger(executor: AlpacaExecutor, portfolio_manager) -> dict:
    """Replace stale ledger with Alpaca ground truth."""
    path = portfolio_manager.ledger_file
    if os.path.exists(path):
        backup = f"{path}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(path, backup)
    else:
        backup = None

    positions = executor.client.get_all_positions()
    lines = [
        json.dumps(
            {
                "event": "ledger_rebuilt",
                "at": datetime.now().isoformat(timespec="seconds"),
                "backup": backup,
            }
        )
    ]
    for pos in positions:
        lines.append(
            json.dumps(
                {
                    "pair": normalize_symbol(pos.symbol),
                    "qty": float(pos.qty),
                    "price": float(pos.avg_entry_price or pos.current_price or 0),
                    "status": "open",
                    "source": "alpaca_reconcile",
                }
            )
        )

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    return {"ledger": path, "backup": backup, "open_positions": len(positions)}


def trim_over_cap_sleeves(executor: AlpacaExecutor) -> list[dict]:
    """Sell down sleeves that exceed config caps (proportional within sleeve)."""
    account = executor.client.get_account()
    equity = float(account.equity)
    actions = []

    sleeve_defs = (
        ("crypto", config.CRYPTO_SLEEVE_CAP_PCT, executor.crypto_sleeve_value, AlpacaExecutor._is_crypto_position),
        ("spy", config.SPY_SLEEVE_CAP_PCT, executor.spy_sleeve_value, AlpacaExecutor._is_spy_position),
        ("nyse", config.NYSE_SLEEVE_CAP_PCT, executor.nyse_sleeve_value, AlpacaExecutor._is_nyse_sleeve_position),
    )

    for name, cap_pct, value_fn, pred in sleeve_defs:
        cap = equity * cap_pct
        value = value_fn()
        excess = round(value - cap, 2)
        if excess < config.MIN_NOTIONAL:
            continue

        positions = [p for p in executor.client.get_all_positions() if pred(p)]
        if not positions:
            continue

        total = sum(_position_value(p) for p in positions)
        remaining = excess
        for pos in positions:
            if remaining < config.MIN_NOTIONAL:
                break
            mv = _position_value(pos)
            if mv <= 0 or total <= 0:
                continue
            sell_notional = round(min(remaining, excess * (mv / total), mv), 2)
            if sell_notional < config.MIN_NOTIONAL:
                continue
            sym = normalize_symbol(pos.symbol)
            order = executor.execute_reduce_notional(sym, sell_notional)
            actions.append(
                {
                    "sleeve": name,
                    "symbol": sym,
                    "sell_notional": sell_notional,
                    "ok": order is not None,
                }
            )
            remaining = round(remaining - sell_notional, 2)

    return actions


def reconcile(
    executor: AlpacaExecutor,
    portfolio_manager,
    *,
    rebuild: bool = True,
    trim: bool = False,
) -> dict:
    before = holdings_audit(executor)
    result = {"before": before, "trim_actions": [], "ledger": None}

    if trim:
        result["trim_actions"] = trim_over_cap_sleeves(executor)

    if rebuild:
        result["ledger"] = rebuild_ledger(executor, portfolio_manager)

    result["after"] = holdings_audit(executor)
    return result
