"""Per-cycle buy budget for Kraken autopilot (caps total USD deployed on buys)."""

from __future__ import annotations

import config

_cycle_buy_spent = 0.0


def reset_cycle_budget() -> None:
    global _cycle_buy_spent
    _cycle_buy_spent = 0.0


def cycle_budget_usd() -> float:
    return float(getattr(config, "KRAKEN_CYCLE_BUDGET_USD", 0) or 0)


def cycle_buy_spent() -> float:
    return _cycle_buy_spent


def cap_buy_usd(amount: float) -> float:
    """Cap a buy to max order size and remaining cycle budget.

    Returns 0.0 when capped below MIN_NOTIONAL (invalid Kraken order size).
    """
    min_n = float(config.MIN_NOTIONAL)
    amount = min(max(float(amount), 0), float(config.KRAKEN_MAX_ORDER_USD))
    budget = cycle_budget_usd()
    if budget > 0:
        remaining = budget - _cycle_buy_spent
        if remaining < min_n:
            return 0.0
        amount = min(amount, remaining)
    if amount < min_n:
        return 0.0
    return round(amount, 2)


def record_buy(usd: float) -> None:
    global _cycle_buy_spent
    if usd > 0:
        _cycle_buy_spent = round(_cycle_buy_spent + float(usd), 2)
