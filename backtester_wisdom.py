"""Fund backtest comparing price-only vs Wayback wisdom sentiment modes.

Uses the same three sleeves as backtester.py (SPY + vol-gated crypto + NYSE)
on daily bars, with monthly Wayback web sentiment forward-filled (no lookahead).

Run:
  python backtester_wisdom.py
  python backtester_wisdom.py --from 2017 --to 2023
"""

from __future__ import annotations

import argparse
import warnings

import numpy as np
import pandas as pd

import config
from backtester import (
    BENCHMARK,
    DAILY_COOLDOWN_BARS,
    MIN_HISTORY,
    BacktestExecutor,
    BacktestPortfolio,
    _benchmark_return,
    _ensure_daily_data,
)
from modules.market_context import get_market_regime, get_volatility
from modules.pipeline_strategies import (
    run_crypto_strategy,
    run_equity_strategy,
    run_spy_strategy,
)
from modules.risk_management import RiskManager
from modules.wayback_sentiment import load_monthly_web_sentiment
from modules.wisdom_sentiment import MODES, PAUSE_REGIME, entries_paused, regime_sentiment


def _slice_data(data: pd.DataFrame, year_from: int, year_to: int) -> pd.DataFrame:
    start = pd.Timestamp(f"{year_from}-01-01")
    end = pd.Timestamp(f"{year_to}-12-31")
    if data.index.tz is not None:
        start = start.tz_localize(data.index.tz)
        end = end.tz_localize(data.index.tz)
    return data.loc[(data.index >= start) & (data.index <= end)]


def run_fund_backtest(
    data: pd.DataFrame,
    monthly_web: pd.Series,
    mode: str,
    gap_threshold: float = 0.25,
) -> dict:
    if len(data) < MIN_HISTORY:
        raise ValueError(f"Need {MIN_HISTORY}+ rows; got {len(data)}")

    portfolio = BacktestPortfolio()
    pair_cooldown = {}
    risk_manager = RiskManager(max_drawdown_pct=config.MAX_DRAWDOWN_PCT)
    equity_curve = []
    regime_counts = {}
    total_crypto = total_equity = total_spy = total_orders = 0
    paused_days = 0
    halted = False
    start_i = MIN_HISTORY

    for i in range(start_i, len(data)):
        window = data.iloc[: i + 1]
        prices = window.iloc[-1]
        ts = data.index[i]
        eq = portfolio.equity(prices)
        equity_curve.append(eq)

        if halted or not risk_manager.check_drawdown(eq):
            if not halted:
                halted = True
            continue

        vol = get_volatility(window)
        sent, web, gap = regime_sentiment(
            window, ts, monthly_web, mode=mode, gap_threshold=gap_threshold
        )
        regime = get_market_regime(sent, vol)
        if entries_paused(mode, web, gap, gap_threshold, data=window, vol=vol):
            regime = PAUSE_REGIME
            paused_days += 1
        regime_counts[regime] = regime_counts.get(regime, 0) + 1

        executor = BacktestExecutor(portfolio, prices)
        total_crypto += run_crypto_strategy(
            window,
            executor,
            regime,
            i,
            pair_cooldown,
            cooldown_bars=DAILY_COOLDOWN_BARS,
            volatility=vol,
        )
        total_spy += run_spy_strategy(
            window,
            executor,
            regime,
            i,
            pair_cooldown,
            cooldown_bars=DAILY_COOLDOWN_BARS,
        )
        total_equity += run_equity_strategy(
            window,
            executor,
            regime,
            i,
            pair_cooldown,
            cooldown_bars=DAILY_COOLDOWN_BARS,
        )
        total_orders += len(executor.orders)

    curve = pd.Series(equity_curve, index=data.index[start_i:])
    returns = curve.pct_change().dropna()
    total_ret = (curve.iloc[-1] / portfolio.initial_capital - 1) * 100
    sharpe = (
        (returns.mean() / returns.std()) * np.sqrt(252) if returns.std() != 0 else 0.0
    )
    max_dd = ((curve / curve.cummax()) - 1).min() * 100
    bench = _benchmark_return(data, start_i)

    return {
        "mode": mode,
        "final_equity": round(curve.iloc[-1], 2),
        "total_return_pct": round(total_ret, 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "benchmark_pct": round(bench, 2) if bench is not None else None,
        "spy_signals": total_spy,
        "crypto_signals": total_crypto,
        "nyse_signals": total_equity,
        "orders": total_orders,
        "paused_days": paused_days,
        "halted": halted,
        "regime_counts": regime_counts,
        "start": data.index[start_i].date(),
        "end": data.index[-1].date(),
        "equity_index": [ts.isoformat() for ts in curve.index],
        "equity_values": [round(v, 2) for v in curve.values],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Fund backtest: wisdom vs baseline")
    parser.add_argument("--from", dest="year_from", type=int, default=2017)
    parser.add_argument("--to", dest="year_to", type=int, default=2023)
    parser.add_argument("--gap", type=float, default=0.25)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    monthly_web = load_monthly_web_sentiment()
    if monthly_web.empty:
        print("Missing wayback_sentiment.csv — run simulate_wayback_sentiment.py first.")
        return

    print("=== FUND WISDOM BACKTEST (SPY + crypto + NYSE) ===")
    print(f"Wayback window: {monthly_web.index.min().date()} -> {monthly_web.index.max().date()}")
    print(f"Simulation:     {args.year_from} -> {args.year_to}")
    print(f"Gap threshold:  {args.gap}")

    data = _ensure_daily_data(0, refresh=args.refresh, use_max=True)
    data = _slice_data(data, args.year_from, args.year_to)
    print(f"Daily bars:     {len(data)} ({data.index.min().date()} -> {data.index.max().date()})")

    results = []
    for mode in MODES:
        print(f"\n--- Running {mode} ---")
        row = run_fund_backtest(data, monthly_web, mode, gap_threshold=args.gap)
        results.append(row)
        print(
            f"  return {row['total_return_pct']:+.2f}%  Sharpe {row['sharpe']:.2f}  "
            f"max DD {row['max_drawdown_pct']:.2f}%  equity ${row['final_equity']:,.0f}"
        )
        if mode == "wisdom_pause":
            print(f"  wisdom-pause days: {row['paused_days']}")
        if mode == "governor":
            print(f"  governor-pause days: {row['paused_days']}")

    print("\n=== COMPARISON ===")
    header = f"{'Mode':<16} {'Return':>9} {'Sharpe':>7} {'MaxDD':>8} {'Orders':>7}"
    print(header)
    print("-" * len(header))
    for row in sorted(results, key=lambda r: -r["total_return_pct"]):
        print(
            f"{row['mode']:<16} {row['total_return_pct']:+8.2f}% "
            f"{row['sharpe']:7.2f} {row['max_drawdown_pct']:7.2f}% {row['orders']:7d}"
        )
    bench = results[0].get("benchmark_pct")
    if bench is not None:
        print(f"\nVTI buy & hold (same window): {bench:+.2f}%")

    out = pd.DataFrame(results)
    out_path = "fund_wisdom_backtest_results.csv"
    out.to_csv(out_path, index=False)
    print(f"\nSaved -> {out_path}")


if __name__ == "__main__":
    main()
