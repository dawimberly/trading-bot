"""Backtest the live stack: game_plan_gld_slv_cper + wisdom modes.

Compares what you run in paper (game plan + governor) vs simpler variants.

Run:
  python scripts/research/backtest_live_stack.py
  python scripts/research/backtest_live_stack.py --from 2022 --to 2022 --fresh
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backtester import MIN_HISTORY
from backtester_metals import (
    load_fund_with_metals,
    run_fresh_capital_backtest,
    run_metals_backtest,
)
from backtester_wisdom import _slice_data
from modules.wayback_sentiment import load_monthly_web_sentiment

LIVE_STRATEGY = "game_plan_gld_slv_cper"
OUT_FULL = "fund_live_stack_backtest.csv"
OUT_FRESH = "fund_live_stack_fresh_2022.csv"

STACKS = (
    ("baseline", None),
    ("game_plan + price", None),
    ("game_plan + arbitrage", "arbitrage"),
    ("game_plan + governor (LIVE)", "governor"),
    ("game_plan + wisdom_pause", "wisdom_pause"),
)


def _run_window(
    data: pd.DataFrame,
    monthly_web: pd.Series,
    *,
    fresh: bool,
    reset_date: str,
    end_date: str,
) -> list[dict]:
    rows = []
    for label, wisdom_mode in STACKS:
        strategy = "baseline" if label == "baseline" else LIVE_STRATEGY
        if fresh:
            row = run_fresh_capital_backtest(
                data,
                strategy,
                reset_date=reset_date,
                end_date=end_date,
                initial_capital=10_000.0,
                wisdom_mode=wisdom_mode,
                monthly_web=monthly_web,
            )
        else:
            row = run_metals_backtest(
                data,
                strategy,
                initial_capital=10_000.0,
                wisdom_mode=wisdom_mode,
                monthly_web=monthly_web,
            )
        row["label"] = label
        rows.append(row)
    return rows


def _print_table(rows: list[dict], title: str) -> None:
    print(f"\n=== {title} ===")
    header = f"{'Stack':<28} {'Return':>9} {'Sharpe':>7} {'MaxDD':>8} {'Pause':>6} {'Metal$':>8}"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['label']:<28} "
            f"{row['total_return_pct']:+8.2f}% "
            f"{row['sharpe']:7.2f} "
            f"{row['max_drawdown_pct']:7.2f}% "
            f"{row.get('wisdom_pause_days', 0):6d} "
            f"{row.get('metal_final', 0):8,.0f}"
        )
    bench = rows[0].get("benchmark_pct")
    if bench is not None:
        print(f"\nVTI buy & hold: {bench:+.2f}%")


def _live_vs_baseline(rows: list[dict]) -> None:
    live = next(r for r in rows if "LIVE" in r["label"])
    base = next(r for r in rows if r["label"] == "baseline")
    gp_price = next(r for r in rows if r["label"] == "game_plan + price")
    print("\n--- LIVE stack vs references ---")
    print(
        f"  vs baseline:     return {live['total_return_pct'] - base['total_return_pct']:+.2f} pp  "
        f"MaxDD {live['max_drawdown_pct'] - base['max_drawdown_pct']:+.2f} pp"
    )
    print(
        f"  vs game_plan only: return {live['total_return_pct'] - gp_price['total_return_pct']:+.2f} pp  "
        f"MaxDD {live['max_drawdown_pct'] - gp_price['max_drawdown_pct']:+.2f} pp  "
        f"pause days {live.get('wisdom_pause_days', 0)} vs {gp_price.get('wisdom_pause_days', 0)}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Live stack backtest (game plan + wisdom)")
    parser.add_argument("--from", dest="year_from", type=int, default=2017)
    parser.add_argument("--to", dest="year_to", type=int, default=2023)
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Fresh $10k at year_from-01-01 (warmup from 2017 if year_from=2022)",
    )
    args = parser.parse_args()

    monthly_web = load_monthly_web_sentiment()
    if monthly_web.empty:
        print("Missing wayback_sentiment.csv — run simulate_wayback_sentiment.py first.")
        return

    raw = load_fund_with_metals(refresh=False)
    if args.fresh:
        warmup_from = 2017 if args.year_from >= 2022 else args.year_from
        data = _slice_data(raw, warmup_from, args.year_to)
        reset = f"{args.year_from}-01-01"
        end = f"{args.year_to}-12-31"
        title = f"FRESH ${10_000:,} {args.year_from} stress (game plan + wisdom)"
    else:
        data = _slice_data(raw, args.year_from, args.year_to)
        reset = end = ""
        title = f"FULL {args.year_from}-{args.year_to} (game plan + wisdom)"

    print("=== LIVE STACK BACKTEST ===")
    print(f"Strategy: {LIVE_STRATEGY} | 50/30/20 GLD/SLV/CPER + yield gate + stress cash")
    print(f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} ({len(data)} bars)")

    rows = _run_window(
        data,
        monthly_web,
        fresh=args.fresh,
        reset_date=reset or f"{args.year_from}-01-01",
        end_date=end or f"{args.year_to}-12-31",
    )
    _print_table(rows, title)
    _live_vs_baseline(rows)

    out = OUT_FRESH if args.fresh else OUT_FULL
    pd.DataFrame(rows).to_csv(ROOT / out, index=False)
    print(f"\nSaved -> {out}")


if __name__ == "__main__":
    main()
