"""Live game plan: yield gate, metal blend sleeve, stress cash trim."""

from __future__ import annotations

import config
from modules.macro_signals import evaluate, load_daily_matrix


def _metal_position_value(executor, symbol: str) -> float:
    pos = executor._find_position(symbol)
    if pos is None:
        return 0.0
    return executor._position_market_value(pos)


def metal_sleeve_value(executor) -> float:
    return sum(_metal_position_value(executor, s) for s in config.LIVE_METAL_SYMBOLS)


def _trim_long_sleeves_for_cash(executor, need: float) -> list[dict]:
    """Sell from SPY/crypto/NYSE (not metals) to raise cash."""
    actions = []
    if need < config.MIN_NOTIONAL:
        return actions
    preds = (
        executor._is_spy_position,
        executor._is_crypto_position,
        executor._is_nyse_sleeve_position,
    )
    positions = []
    for pos in executor._get_positions():
        if any(pred(pos) for pred in preds) and not config.is_metal_symbol(pos.symbol):
            positions.append(pos)
    total = sum(executor._position_market_value(p) for p in positions)
    if total <= 0:
        return actions
    remaining = need
    for pos in positions:
        if remaining < config.MIN_NOTIONAL:
            break
        mv = executor._position_market_value(pos)
        sell = min(mv, remaining / 0.999)
        sym = config.normalize_symbol(pos.symbol)
        order = executor.execute_reduce_notional(sym, sell)
        if order:
            actions.append({"symbol": sym, "notional": round(sell, 2), "phase": "sell"})
            remaining -= sell * 0.999
    return actions


def trim_to_stress_cash(executor, *, stress: bool) -> list[dict]:
    if not stress or not config.GAME_PLAN_ENABLED:
        return []
    account = executor._get_account()
    equity = float(account.equity)
    cash = float(account.cash)
    target = equity * config.STRESS_CASH_PCT
    if cash >= target:
        return []
    need = target - cash
    return _trim_long_sleeves_for_cash(executor, need)


def rebalance_metal_sleeve(executor, *, stress: bool, market_open: bool) -> list[dict]:
    """Deploy 50/30/20 GLD/SLV/CPER on stress; exit on calm."""
    if not config.GAME_PLAN_ENABLED or not market_open:
        return []

    actions = []
    account = executor._get_account()
    equity = float(account.equity)
    cap = equity * config.METAL_SLEEVE_CAP_PCT
    weights = config.metal_blend_weights()

    if not stress:
        for symbol in weights:
            if _metal_position_value(executor, symbol) >= config.MIN_NOTIONAL:
                order = executor.execute_full_exit(symbol)
                if order:
                    actions.append({"symbol": symbol, "phase": "exit_metal"})
        return actions

    target_total = cap * config.METAL_SLEEVE_DEPLOY_PCT
    for symbol, w in weights.items():
        target = target_total * w
        current = _metal_position_value(executor, symbol)
        if current >= target * 0.95:
            continue
        buy = min(target - current, float(executor._get_account().cash) * 0.95)
        buy = round(max(config.MIN_NOTIONAL, buy), 2)
        if buy < config.MIN_NOTIONAL:
            continue
        order = executor.execute_order(symbol, "buy", notional=buy)
        if order and executor.order_filled(order):
            actions.append({"symbol": symbol, "notional": buy, "phase": "buy_metal"})
    return actions


def run_game_plan_cycle(
    executor,
    regime: str,
    *,
    market_open: bool,
    refresh_daily: bool = False,
    signals: dict | None = None,
) -> dict:
    """One cycle: signals + optional metal rebalance + stress cash trim."""
    if not config.GAME_PLAN_ENABLED:
        return {"enabled": False}

    daily = None
    if signals is None:
        daily = load_daily_matrix(days=450, refresh=refresh_daily)
        signals = evaluate(daily, regime)
    elif not signals.get("ok"):
        daily = load_daily_matrix(days=450, refresh=refresh_daily)
        signals = evaluate(daily, regime)
    actions = []
    if market_open:
        # Match backtest order: stress cash trim before metal sleeve deploy
        actions.extend(trim_to_stress_cash(executor, stress=signals["stress"]))
        actions.extend(rebalance_metal_sleeve(executor, stress=signals["stress"], market_open=True))

    return {
        "enabled": True,
        "signals": signals,
        "metal_value": round(metal_sleeve_value(executor), 2),
        "metal_cap": round(float(executor._get_account().equity) * config.METAL_SLEEVE_CAP_PCT, 2),
        "blend": config.metal_blend_weights(),
        "actions": actions,
    }
