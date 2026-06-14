"""Aligned live journal vs fund-backtest comparison (same calendar window).

Run: python scripts/analysis/live_vs_backtest_snapshot.py --refresh-eval --reconcile --live-only

When PAPER_TRADING=false (or --live-only), uses post-switch journal rows only and
small-account sim ($100, 90% VTI, 1% risk, $10 max) so paper history cannot skew gaps.
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
from modules.wisdom_evaluator import (
    default_book_type,
    default_min_equity,
    filter_paper_journal,
    live_metrics,
    resolve_live_only,
    run_evaluation,
    simulate_modes,
    use_small_account_sim,
)
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


def _alpaca_equity(*, deposit_basis: float | None = None) -> dict | None:
    try:
        from modules.alpaca_executor import get_trading_client

        account = get_trading_client().get_account()
        equity = float(account.equity)
        basis = deposit_basis if deposit_basis and deposit_basis > 0 else equity
        return {
            "equity": round(equity, 2),
            "deposit_basis": round(basis, 2),
            "return_pct_vs_basis": round((equity / basis - 1) * 100, 3),
        }
    except Exception as exc:
        return {"error": str(exc)}


def _load_trade_signals(
    period_start: date,
    period_end: date,
    *,
    live_only: bool,
    min_equity: float | None,
) -> tuple[pd.DataFrame, dict]:
    path = Path(config.PAPER_JOURNAL_CSV)
    if not path.exists():
        return pd.DataFrame(), {}
    signals = pd.read_csv(path)
    signals["ts"] = pd.to_datetime(signals["timestamp"])
    filtered, segment = filter_paper_journal(
        signals,
        live_only=live_only,
        min_equity=min_equity,
    )
    mask = (filtered["ts"].dt.date >= period_start) & (filtered["ts"].dt.date <= period_end)
    trade = filtered.loc[mask & (filtered["event"] == "signal")].copy()
    return trade, segment


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
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        metavar="N",
        help="Evaluation window in days (default: WISDOM_EVAL_DAYS / 30)",
    )
    parser.add_argument(
        "--live-only",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Use post-switch live journal only (default: on when PAPER_TRADING=false)",
    )
    parser.add_argument(
        "--book-type",
        choices=("live", "paper"),
        default=None,
        help="Override journal segment (default: live when --live-only, else paper)",
    )
    parser.add_argument(
        "--min-equity",
        type=float,
        default=None,
        metavar="USD",
        help="Drop journal rows below this equity (default: 50 for live book)",
    )
    args = parser.parse_args()

    window_days = args.days if args.days is not None else config.WISDOM_EVAL_DAYS
    live_only = resolve_live_only(args.live_only)
    book_type = args.book_type or default_book_type(live_only=live_only)
    min_equity = (
        args.min_equity if args.min_equity is not None else default_min_equity(book_type)
    )

    if args.refresh_eval:
        run_evaluation(force=True, live_only=live_only)

    live = live_metrics(
        window_days,
        book_type=book_type,
        min_equity=min_equity,
        live_only=live_only,
    )
    if not live:
        print("No wisdom journal data for this book segment.")
        return

    period_start = date.fromisoformat(str(live["from_date"]))
    period_end = date.fromisoformat(str(live["to_date"]))
    simulated = simulate_modes(
        window_days,
        period_start=period_start,
        period_end=period_end,
        live_only=live_only,
        live_start_equity=live.get("start_equity"),
    )
    active = live.get("mode", config.WISDOM_MODE)
    active_sim = simulated.get(active, {})

    trade_signals, paper_segment = _load_trade_signals(
        period_start,
        period_end,
        live_only=live_only,
        min_equity=min_equity,
    )

    report = {
        "window_days": window_days,
        "live_only": live_only,
        "book_type": book_type,
        "min_equity": min_equity,
        "paper_trading": config.PAPER_TRADING,
        "small_account_sim": use_small_account_sim(
            live_only=live_only,
            start_equity=live.get("start_equity"),
        ),
        "journal_segment": live.get("journal_segment"),
        "paper_journal_segment": paper_segment,
        "live_window": {"from": str(period_start), "to": str(period_end)},
        "data_through": str(load_close_matrix(interval="1d").index.max().date()),
        "live_equity_basis": "daily_last",
        "live": live,
        "alpaca": _alpaca_equity(deposit_basis=live.get("start_equity")),
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
            live_only=live_only,
            min_equity=min_equity,
        )
    if "return_pct" in live and "return_pct" in active_sim:
        report["live_minus_active_sim_pp"] = round(
            live["return_pct"] - active_sim["return_pct"], 2
        )

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
