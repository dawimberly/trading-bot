"""Aligned live journal vs fund-backtest comparison (same calendar window).

Run: python scripts/analysis/live_vs_backtest_snapshot.py
     python scripts/analysis/live_vs_backtest_snapshot.py --refresh-eval
     python scripts/analysis/live_vs_backtest_snapshot.py --reconcile
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

import config
from modules.data_loader import load_close_matrix
from modules.wisdom_evaluator import live_metrics, run_evaluation, simulate_modes
from modules.wisdom_journal import load_journal
from scripts.analysis.trade_reconciliation import build_reconciliation_report

BENCHMARK = "VTI"


def _benchmark_return(period_start: date, period_end: date) -> float | None:
    data = load_close_matrix(interval="1d")
    if BENCHMARK not in data.columns:
        return None
    series = data[BENCHMARK].dropna()
    sub = series.loc[str(period_start) : str(period_end)]
    if len(sub) < 2:
        return None
    return round(float((sub.iloc[-1] / sub.iloc[0] - 1) * 100), 3)


def _alpaca_equity() -> dict | None:
    try:
        from modules.alpaca_executor import get_trading_client

        account = get_trading_client().get_account()
        equity = float(account.equity)
        start = 100_000.0
        return {
            "equity": round(equity, 2),
            "return_pct_vs_100k": round((equity / start - 1) * 100, 3),
        }
    except Exception as exc:
        return {"error": str(exc)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Live vs aligned fund backtest snapshot")
    parser.add_argument(
        "--refresh-eval",
        action="store_true",
        help="Regenerate wisdom_scorecard.json with aligned sim windows",
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="Include trade-level journal vs Alpaca fill reconciliation",
    )
    args = parser.parse_args()

    if args.refresh_eval:
        run_evaluation(force=True)

    live = live_metrics(config.WISDOM_EVAL_DAYS)
    if not live:
        print("No wisdom journal data.")
        return

    period_start = date.fromisoformat(str(live["from_date"]))
    period_end = date.fromisoformat(str(live["to_date"]))
    simulated = simulate_modes(
        config.WISDOM_EVAL_DAYS,
        period_start=period_start,
        period_end=period_end,
    )
    active = live.get("mode", config.WISDOM_MODE)
    active_sim = simulated.get(active, {})

    df = load_journal()
    signals = pd.read_csv(config.PAPER_JOURNAL_CSV)
    signals["ts"] = pd.to_datetime(signals["timestamp"])
    mask = (signals["ts"].dt.date >= period_start) & (signals["ts"].dt.date <= period_end)
    trade_signals = signals.loc[mask & (signals["event"] == "signal")]

    report = {
        "live_window": {"from": str(period_start), "to": str(period_end)},
        "data_through": str(load_close_matrix(interval="1d").index.max().date()),
        "live_equity_basis": "daily_last",
        "live": live,
        "alpaca": _alpaca_equity(),
        "benchmark_vti_pct": _benchmark_return(period_start, period_end),
        "active_mode": active,
        "active_mode_sim": active_sim,
        "all_modes_sim": simulated,
        "game_plan_in_sim": config.GAME_PLAN_ENABLED,
        "trade_signals_in_window": int(len(trade_signals)),
    }
    if args.reconcile:
        report["trade_reconciliation"] = build_reconciliation_report(
            period_start=period_start,
            period_end=period_end,
        )
    if "return_pct" in live and "return_pct" in active_sim:
        report["live_minus_active_sim_pp"] = round(
            live["return_pct"] - active_sim["return_pct"], 2
        )

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
