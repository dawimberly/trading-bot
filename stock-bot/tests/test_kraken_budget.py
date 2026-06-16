"""Kraken buy-budget cap tests.

Run: python tests/test_kraken_budget.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from modules import kraken_budget


def _assert_valid(result: float, min_n: float) -> None:
    assert result == 0.0 or result >= min_n, f"invalid cap result: {result}"


def run_tests() -> None:
    min_n = float(config.MIN_NOTIONAL)
    orig_budget = config.KRAKEN_CYCLE_BUDGET_USD
    orig_max = config.KRAKEN_MAX_ORDER_USD

    try:
        config.KRAKEN_MAX_ORDER_USD = 25.0

        kraken_budget.reset_cycle_budget()
        config.KRAKEN_CYCLE_BUDGET_USD = 10.0
        r = kraken_budget.cap_buy_usd(5.0)
        assert r == 0.0, f"sub-min request with budget headroom: expected 0, got {r}"

        kraken_budget.reset_cycle_budget()
        config.KRAKEN_CYCLE_BUDGET_USD = 10.0
        r = kraken_budget.cap_buy_usd(10.0)
        assert r == 10.0, f"at-min request: expected 10, got {r}"

        kraken_budget.reset_cycle_budget()
        config.KRAKEN_CYCLE_BUDGET_USD = 50.0
        kraken_budget.record_buy(42.0)
        r = kraken_budget.cap_buy_usd(15.0)
        assert r == 0.0, f"remaining budget below min: expected 0, got {r}"

        kraken_budget.reset_cycle_budget()
        config.KRAKEN_CYCLE_BUDGET_USD = 0.0
        r = kraken_budget.cap_buy_usd(5.0)
        assert r == 0.0, f"uncapped sub-min request: expected 0, got {r}"

        kraken_budget.reset_cycle_budget()
        config.KRAKEN_CYCLE_BUDGET_USD = 100.0
        config.KRAKEN_MAX_ORDER_USD = 8.0
        r = kraken_budget.cap_buy_usd(15.0)
        assert r == 0.0, f"max order below min: expected 0, got {r}"

        for amount, budget, spent in ((12.0, 22.0, 10.0), (25.0, 50.0, 0.0)):
            kraken_budget.reset_cycle_budget()
            config.KRAKEN_CYCLE_BUDGET_USD = budget
            config.KRAKEN_MAX_ORDER_USD = 25.0
            if spent:
                kraken_budget.record_buy(spent)
            r = kraken_budget.cap_buy_usd(amount)
            _assert_valid(r, min_n)

        print("kraken_budget.cap_buy_usd: all tests passed")
    finally:
        config.KRAKEN_CYCLE_BUDGET_USD = orig_budget
        config.KRAKEN_MAX_ORDER_USD = orig_max
        kraken_budget.reset_cycle_budget()


if __name__ == "__main__":
    run_tests()
