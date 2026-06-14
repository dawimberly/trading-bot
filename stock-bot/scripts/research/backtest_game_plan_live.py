"""Backtest the live game plan: game_plan_gld_slv_cper (50/30/20 + yield gate + stress cash)."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backtester import MIN_HISTORY
from backtester_metals import (
    load_fund_with_metals,
    run_fresh_capital_backtest,
    run_metals_backtest,
)
from backtester_wisdom import _slice_data

LIVE_STRATEGY = "game_plan_gld_slv_cper"
FULL_COMPARE = ["baseline", "game_plan_gld", LIVE_STRATEGY, "game_plan_basket"]
FRESH_COMPARE = [
    "baseline",
    "gld_slv_cper",
    LIVE_STRATEGY,
    "game_plan_basket",
    "slv_only",
]


def _print_row(row: dict) -> None:
    name = row["strategy"]
    extra = ""
    if name.startswith("game_plan") or name in ("gld_slv_cper", "gld_only", "slv_only"):
        extra = (
            f"  | gate {row.get('yield_gate_days', 0)}d  "
            f"cash trims {row.get('cash_trims', 0)}  "
            f"metal trades {row.get('metal_trades', 0)}"
        )
    print(f"{name}:")
    print(
        f"  Return {row['total_return_pct']:+.2f}%  "
        f"Sharpe {row['sharpe']:.2f}  "
        f"MaxDD {row['max_drawdown_pct']:.2f}%"
    )
    print(
        f"  Final ${row['final_equity']:,.0f}  "
        f"Metal ${row.get('metal_final', 0):,.0f}{extra}"
    )
    if row.get("benchmark_pct") is not None:
        print(f"  VTI benchmark: {row['benchmark_pct']:+.2f}%")
    print()


def _run_fresh_2022(raw: pd.DataFrame) -> list[dict]:
    """Fresh $10k at Jan 2022; warmup from 2017+ for MA200."""
    data = _slice_data(raw, 2017, 2022)
    print("=== FRESH CAPITAL 2022 STRESS TEST ===")
    print("New $10,000 at 2022-01-01 (90% long / 10% metal sleeve for game plan)")
    print("MA200 warmup from 2017 history — no carryover positions or halt from prior years\n")

    results = []
    for name in FRESH_COMPARE:
        row = run_fresh_capital_backtest(
            data,
            name,
            reset_date="2022-01-01",
            end_date="2022-12-31",
            initial_capital=10_000.0,
        )
        results.append(row)
        _print_row(row)

    live = next(r for r in results if r["strategy"] == LIVE_STRATEGY)
    base = next(r for r in results if r["strategy"] == "baseline")
    print("=== vs BASELINE (fresh 2022) ===")
    print(f"Return delta: {live['total_return_pct'] - base['total_return_pct']:+.2f} pp")
    print(f"Sharpe delta: {live['sharpe'] - base['sharpe']:+.2f}")
    print(f"MaxDD improvement: {live['max_drawdown_pct'] - base['max_drawdown_pct']:+.2f} pp")
    print(
        f"Live blend metal sleeve: ${live['metal_final']:,.0f} "
        f"({live['metal_trades']} deploy/exit trades)"
    )
    return results


def main() -> None:
    raw = load_fund_with_metals(refresh=False)

    data = _slice_data(raw, 2017, 2023)
    print("=== LIVE GAME PLAN BACKTEST (full window) ===")
    print(f"Strategy: {LIVE_STRATEGY}")
    print("Blend: 50% GLD / 30% SLV / 20% CPER + yield gate + stress cash")
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data)} daily bars)\n"
    )

    full_results = []
    for name in FULL_COMPARE:
        row = run_metals_backtest(data, name, initial_capital=10_000.0)
        full_results.append(row)
        _print_row(row)

    live = next(r for r in full_results if r["strategy"] == LIVE_STRATEGY)
    base = next(r for r in full_results if r["strategy"] == "baseline")
    print("=== vs BASELINE (full 2017-2023) ===")
    print(f"Return delta: {live['total_return_pct'] - base['total_return_pct']:+.2f} pp")
    print(f"Sharpe delta: {live['sharpe'] - base['sharpe']:+.2f}")
    print(f"MaxDD delta: {live['max_drawdown_pct'] - base['max_drawdown_pct']:+.2f} pp\n")

    fresh_results = _run_fresh_2022(raw)

    pd.DataFrame(full_results).to_csv("fund_game_plan_live_backtest.csv", index=False)
    pd.DataFrame(fresh_results).to_csv("fund_game_plan_fresh_2022.csv", index=False)
    print("\nSaved -> fund_game_plan_live_backtest.csv")
    print("Saved -> fund_game_plan_fresh_2022.csv")


if __name__ == "__main__":
    main()
