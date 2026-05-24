"""Generate a static buy plan JSON for target allocations.

Run: python scripts/dev/market_screener.py
"""

import json

import config
from modules.alpaca_executor import get_trading_client


def run_screener():
    targets = {"VTI": 0.33, "VXUS": 0.66}
    deployment_size = 150.00
    plan = {}
    for symbol, target_weight in targets.items():
        amount = deployment_size * target_weight
        plan[symbol] = {"action": "buy", "amount_needed": amount}
    with open("plan.json", "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=4)
    _ = get_trading_client()  # verify credentials
    print(f"Intelligence Layer: Plan generated for ${deployment_size:.2f}")


if __name__ == "__main__":
    run_screener()
