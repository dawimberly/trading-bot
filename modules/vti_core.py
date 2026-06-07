"""Passive VTI core sleeve — index anchor; active bot runs on the remainder."""

from __future__ import annotations

import config


def vti_core_value(executor) -> float:
    pos = executor._find_position(config.VTI_CORE_SYMBOL)
    if pos is None:
        return 0.0
    return executor._position_market_value(pos)


def rebalance_vti_core(executor, *, market_open: bool) -> dict:
    """
    Hold VTI at VTI_CORE_PCT of equity. Rebalance when drift exceeds band.
    Protected from halt trims and stop-loss (see game_plan / position_exits).
    """
    if not config.vti_core_enabled():
        return {"enabled": False}
    if not market_open:
        return {"enabled": True, "skipped": True, "reason": "equity session closed"}

    account = executor._get_account()
    equity = float(account.equity)
    if equity <= 0:
        return {"enabled": True, "skipped": True, "reason": "no equity"}

    core_pct = config.vti_core_allocation_pct()
    target = round(equity * core_pct, 2)
    current = round(vti_core_value(executor), 2)
    min_n = config.effective_min_notional(equity)
    drift_pct = abs(current - target) / equity if equity else 0.0
    band = config.effective_vti_rebalance_drift_pct()

    result = {
        "enabled": True,
        "symbol": config.VTI_CORE_SYMBOL,
        "target_pct": core_pct,
        "paper_aggressive": config.paper_aggressive_context(),
        "target_value": target,
        "current_value": current,
        "drift_pct": round(drift_pct, 4),
    }

    if current > 0 and drift_pct < band:
        result["skipped"] = True
        result["reason"] = f"within {band:.0%} drift band"
        return result

    delta = round(target - current, 2)
    if abs(delta) < min_n:
        result["skipped"] = True
        result["reason"] = "below min notional"
        return result

    symbol = config.VTI_CORE_SYMBOL
    if delta > 0:
        order = executor.execute_order(symbol, "buy", notional=delta)
        result["action"] = "buy"
    else:
        order = executor.execute_reduce_notional(symbol, -delta)
        result["action"] = "sell"

    result["notional"] = abs(delta)
    result["ok"] = order is not None and (
        executor.order_filled(order, max_wait=3.0) if order else False
    )
    if result.get("ok"):
        executor.refresh_cache()
        result["current_value"] = round(vti_core_value(executor), 2)
    return result
