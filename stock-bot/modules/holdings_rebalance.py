"""Rebalance Alpaca holdings toward fund sleeve targets + strategy gates."""

from __future__ import annotations

from datetime import datetime

import config
from modules.alpaca_executor import AlpacaExecutor
from modules.holdings_reconcile import holdings_audit, rebuild_ledger, normalize_symbol
from modules.crypto_vol_gate import crypto_target_allowed
from modules.pipeline_strategies import (
    PAUSED_REGIMES,
    _equity_momentum_candidates,
    _spy_market_up_signal,
)
from modules.portfolio_manager import PortfolioManager

SLEEVE_ORDER = ("crypto", "spy", "nyse")


def _min_notional(executor: AlpacaExecutor) -> float:
    return config.effective_min_notional(float(executor.client.get_account().equity))


def _position_value(pos) -> float:
    mv = getattr(pos, "market_value", None)
    if mv is not None:
        return abs(float(mv))
    return abs(float(pos.qty) * float(pos.current_price or 0))


def _sleeve_pred(name: str):
    if name == "crypto":
        return AlpacaExecutor._is_crypto_position
    if name == "spy":
        return AlpacaExecutor._is_spy_position
    return AlpacaExecutor._is_nyse_sleeve_position


def _sleeve_value(executor: AlpacaExecutor, name: str) -> float:
    if name == "crypto":
        return executor.crypto_sleeve_value()
    if name == "spy":
        return executor.spy_sleeve_value()
    return executor.nyse_sleeve_value()


def target_sleeves(
    equity: float,
    *,
    volatility: str,
    regime: str,
    spacex_snapshot: dict | None = None,
) -> dict[str, float]:
    """Target market value per sleeve (strategy-aware crypto target)."""
    crypto_target = equity * config.effective_sleeve_cap(config.CRYPTO_SLEEVE_CAP_PCT)
    if config.effective_crypto_vol_only() and not crypto_target_allowed(
        volatility, regime, spacex_snapshot=spacex_snapshot
    ):
        crypto_target = 0.0
    if regime in PAUSED_REGIMES:
        crypto_target = 0.0

    return {
        "spy": equity * config.effective_sleeve_cap(config.SPY_SLEEVE_CAP_PCT),
        "crypto": crypto_target,
        "nyse": equity * config.effective_sleeve_cap(config.NYSE_SLEEVE_CAP_PCT),
        "cash_buffer": equity * config.effective_cash_buffer_pct(),
    }


def _reduce_sleeve(
    executor: AlpacaExecutor,
    sleeve: str,
    target_value: float,
    *,
    dry_run: bool,
) -> list[dict]:
    pred = _sleeve_pred(sleeve)
    positions = [p for p in executor.client.get_all_positions() if pred(p)]
    current = sum(_position_value(p) for p in positions)
    excess = round(current - target_value, 2)
    min_n = _min_notional(executor)
    if excess < min_n or not positions:
        return []

    liquidate = target_value < min_n
    actions = []
    if liquidate:
        for pos in positions:
            sym = normalize_symbol(pos.symbol)
            mv = _position_value(pos)
            action = {
                "phase": "sell",
                "sleeve": sleeve,
                "symbol": sym,
                "notional": round(mv, 2),
                "reason": f"liquidate sleeve (target ${target_value:,.0f})",
            }
            if not dry_run:
                action["ok"] = executor.execute_full_exit(sym) is not None
            actions.append(action)
        return actions

    total = current
    remaining = excess
    for pos in positions:
        if remaining < min_n:
            break
        mv = _position_value(pos)
        if mv <= 0 or total <= 0:
            continue
        sell_notional = round(min(remaining, excess * (mv / total), mv), 2)
        if sell_notional < min_n:
            continue
        sym = normalize_symbol(pos.symbol)
        action = {
            "phase": "sell",
            "sleeve": sleeve,
            "symbol": sym,
            "notional": sell_notional,
            "reason": f"above target ${target_value:,.0f}",
        }
        if not dry_run:
            action["ok"] = (
                executor.execute_reduce_notional(sym, sell_notional) is not None
            )
        actions.append(action)
        remaining = round(remaining - sell_notional, 2)
    return actions


def _deploy_spy(
    executor: AlpacaExecutor,
    data,
    regime: str,
    target_value: float,
    *,
    dry_run: bool,
    yield_gated: bool = False,
) -> list[dict]:
    if regime in PAUSED_REGIMES or yield_gated:
        return []
    symbol = config.SPY_BOT_SYMBOL
    bullish, momentum = _spy_market_up_signal(data, symbol, config.SPY_MA_WINDOW)
    if not bullish:
        return []

    min_n = _min_notional(executor)
    current = executor.spy_sleeve_value()
    room = round(target_value - current, 2)
    if room < min_n:
        return []

    notional = min(
        room,
        executor.compute_spy_notional() or 0,
        round(float(executor.client.get_account().cash) * 0.95, 2),
    )
    if notional < min_n:
        return []

    action = {
        "phase": "buy",
        "sleeve": "spy",
        "symbol": symbol,
        "notional": round(notional, 2),
        "reason": f"toward target ${target_value:,.0f} (momentum {momentum:.2%})",
    }
    if not dry_run:
        action["ok"] = executor.execute_order(symbol, "buy", notional=notional) is not None
    return [action]


def _deploy_nyse(
    executor: AlpacaExecutor,
    data,
    regime: str,
    target_value: float,
    *,
    dry_run: bool,
    max_names: int = 3,
) -> list[dict]:
    if regime in PAUSED_REGIMES:
        return []

    min_n = _min_notional(executor)
    current = executor.nyse_sleeve_value()
    room = round(target_value - current, 2)
    if room < min_n:
        return []

    equity_cols = [
        c
        for c in data.columns
        if not config.is_crypto(c)
        and c != config.SPY_BOT_SYMBOL
        and not config.is_metal_symbol(c)
    ]
    ranked = _equity_momentum_candidates(data, equity_cols)
    if not ranked:
        return []

    actions = []
    remaining_room = room
    cash = float(executor.client.get_account().cash)
    for symbol in ranked[:max_names]:
        if remaining_room < min_n:
            break
        per = executor.compute_nyse_notional()
        if per is None:
            break
        notional = round(min(remaining_room, per, cash * 0.95), 2)
        if notional < min_n:
            break
        action = {
            "phase": "buy",
            "sleeve": "nyse",
            "symbol": symbol,
            "notional": notional,
            "reason": f"toward target ${target_value:,.0f} (MA50 momentum)",
        }
        if not dry_run:
            action["ok"] = executor.execute_order(symbol, "buy", notional=notional) is not None
        actions.append(action)
        remaining_room = round(remaining_room - notional, 2)
        cash -= notional
    return actions


def rebalance_to_targets(
    executor: AlpacaExecutor,
    data,
    *,
    regime: str,
    volatility: str,
    market_open: bool = True,
    portfolio_manager: PortfolioManager | None = None,
    spacex_snapshot: dict | None = None,
    dry_run: bool = False,
    should_rebuild_ledger: bool = True,
    yield_gated: bool = False,
) -> dict:
    """
    1) Sell sleeves above strategy-aware targets (e.g. crypto -> 0 when vol is Low).
    2) Buy SPY / NYSE toward targets when signals allow (equity session required).
    Crypto entries stay with pair strategy — rebalance only reduces crypto, does not add.
    """
    before = holdings_audit(executor)
    equity = before["equity"]
    targets = target_sleeves(
        equity, volatility=volatility, regime=regime, spacex_snapshot=spacex_snapshot
    )
    actions: list[dict] = []

    for sleeve in SLEEVE_ORDER:
        actions.extend(
            _reduce_sleeve(executor, sleeve, targets[sleeve], dry_run=dry_run)
        )

    if market_open:
        actions.extend(
            _deploy_spy(
                executor,
                data,
                regime,
                targets["spy"],
                dry_run=dry_run,
                yield_gated=yield_gated,
            )
        )
        actions.extend(
            _deploy_nyse(executor, data, regime, targets["nyse"], dry_run=dry_run)
        )
    else:
        actions.append(
            {
                "phase": "skip",
                "reason": "equity session closed — sells only; deploy SPY/NYSE when open",
            }
        )

    ledger_info = None
    if not dry_run and should_rebuild_ledger and portfolio_manager is not None:
        ledger_info = rebuild_ledger(executor, portfolio_manager)

    after = holdings_audit(executor) if not dry_run else before
    return {
        "at": datetime.now().isoformat(timespec="seconds"),
        "dry_run": dry_run,
        "regime": regime,
        "volatility": volatility,
        "market_open": market_open,
        "targets": {k: round(v, 2) for k, v in targets.items()},
        "before": before,
        "actions": actions,
        "ledger": ledger_info,
        "after": after,
    }
