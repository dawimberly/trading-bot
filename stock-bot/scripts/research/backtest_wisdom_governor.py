"""Backtest steel-man wisdom governor vs existing wisdom modes.

Governor idea (strongest form):
  When headline mood and price math diverge (|gap| >= threshold) *and* the
  environment is already volatile or macro-stressed, pause new risk-on entries.
  When gap is large but markets are calm, use arbitrage (trust price).
  When stress exists without gap, let game_plan handle defense (separate backtest).

Run:
  python scripts/research/backtest_wisdom_governor.py
  python scripts/research/backtest_wisdom_governor.py --from 2022 --to 2022
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from backtester import MIN_HISTORY, BacktestExecutor, BacktestPortfolio, _benchmark_return, _ensure_daily_data
from backtester_wisdom import _slice_data
from modules.market_context import get_market_regime, get_price_sentiment, get_volatility
from modules.macro_signals import bond_stress
from modules.pipeline_strategies import run_crypto_strategy, run_equity_strategy, run_spy_strategy
from modules.risk_management import RiskManager
from modules.wayback_sentiment import load_monthly_web_sentiment, web_sentiment_for_date
from modules.wisdom_sentiment import PAUSE_REGIME, entries_paused, regime_sentiment

BEAR = "RHYME_E: Steady_Bearish_Decline"
PANIC = "RHYME_B: Panic_Volatility"
DAILY_COOLDOWN_BARS = 1


def _attach_tlt(daily: pd.DataFrame) -> pd.DataFrame:
    from modules.macro_signals import _load_daily_column

    tlt = _load_daily_column("TLT")
    if tlt.empty:
        return daily
    out = daily.copy()
    out["TLT"] = tlt.reindex(out.index).ffill()
    return out


def _governor_pause(
    *,
    gap: float | None,
    gap_threshold: float,
    vol: str,
    price_regime: str,
    window: pd.DataFrame,
    variant: str,
) -> bool:
    if gap is None or np.isnan(gap) or abs(gap) < gap_threshold:
        return False
    if variant == "wisdom_pause":
        return True
    stressed = price_regime in (BEAR, PANIC) or vol == "High" or bond_stress(window)
    if variant == "governor_vol":
        return vol == "High"
    if variant == "governor":
        return stressed
    if variant == "governor_strict":
        hits = sum(
            [
                vol == "High",
                price_regime in (BEAR, PANIC),
                bond_stress(window),
            ]
        )
        return hits >= 2
    return False


def run_mode_backtest(
    data: pd.DataFrame,
    monthly_web: pd.Series,
    variant: str,
    gap_threshold: float = 0.25,
) -> dict:
    data = _attach_tlt(data)
    portfolio = BacktestPortfolio()
    pair_cooldown = {}
    risk_manager = RiskManager(max_drawdown_pct=config.MAX_DRAWDOWN_PCT)
    equity_curve = []
    paused_days = 0
    halted = False
    total_crypto = total_equity = total_spy = total_orders = 0
    start_i = MIN_HISTORY

    for i in range(start_i, len(data)):
        window = data.iloc[: i + 1]
        prices = window.iloc[-1]
        ts = data.index[i]
        eq = portfolio.equity(prices)
        equity_curve.append(eq)
        if halted or not risk_manager.check_drawdown(eq):
            halted = True
            continue

        vol = get_volatility(window)
        price_only = get_price_sentiment(window)
        price_regime = get_market_regime(price_only, vol)

        if variant in ("baseline", "web_regime", "arbitrage", "wisdom_pause", "governor"):
            sent, web, gap = regime_sentiment(
                window, ts, monthly_web, mode=variant, gap_threshold=gap_threshold
            )
            regime = get_market_regime(sent, vol)
            if entries_paused(variant, web, gap, gap_threshold, data=window, vol=vol):
                regime = PAUSE_REGIME
                paused_days += 1
        else:
            sent, web, gap = regime_sentiment(
                window, ts, monthly_web, mode="arbitrage", gap_threshold=gap_threshold
            )
            regime = get_market_regime(sent, vol)
            if _governor_pause(
                gap=gap,
                gap_threshold=gap_threshold,
                vol=vol,
                price_regime=price_regime,
                window=window,
                variant=variant,
            ):
                regime = PAUSE_REGIME
                paused_days += 1

        executor = BacktestExecutor(portfolio, prices)
        total_crypto += run_crypto_strategy(
            window, executor, regime, i, pair_cooldown,
            cooldown_bars=DAILY_COOLDOWN_BARS, volatility=vol,
        )
        total_spy += run_spy_strategy(
            window, executor, regime, i, pair_cooldown, cooldown_bars=DAILY_COOLDOWN_BARS,
        )
        total_equity += run_equity_strategy(
            window, executor, regime, i, pair_cooldown, cooldown_bars=DAILY_COOLDOWN_BARS,
        )
        total_orders += len(executor.orders)

    curve = pd.Series(equity_curve, index=data.index[start_i:])
    returns = curve.pct_change().dropna()
    total_ret = (curve.iloc[-1] / portfolio.initial_capital - 1) * 100
    sharpe = (returns.mean() / returns.std()) * np.sqrt(252) if returns.std() else 0.0
    max_dd = ((curve / curve.cummax()) - 1).min() * 100
    bench = _benchmark_return(data, start_i)
    return {
        "mode": variant,
        "total_return_pct": round(total_ret, 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "orders": total_orders,
        "paused_days": paused_days,
        "benchmark_pct": round(bench, 2) if bench is not None else None,
        "final_equity": round(curve.iloc[-1], 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Steel-man wisdom governor backtest")
    parser.add_argument("--from", dest="year_from", type=int, default=2017)
    parser.add_argument("--to", dest="year_to", type=int, default=2023)
    parser.add_argument("--gap", type=float, default=0.25)
    args = parser.parse_args()

    monthly_web = load_monthly_web_sentiment()
    if monthly_web.empty:
        print("Missing wayback_sentiment.csv")
        return

    variants = (
        "baseline",
        "web_regime",
        "arbitrage",
        "wisdom_pause",
        "governor_vol",
        "governor",
        "governor_strict",
    )

    data = _ensure_daily_data(0, refresh=False, use_max=True)
    data = _slice_data(data, args.year_from, args.year_to)
    print("=== STEEL-MAN WISDOM GOVERNOR BACKTEST ===")
    print(f"Window: {args.year_from} -> {args.year_to} | gap {args.gap} | bars {len(data)}")
    print(
        "Governor: pause on |gap| + (High vol OR bear/panic OR bond stress); "
        "else arbitrage sentiment\n"
    )

    rows = []
    for variant in variants:
        row = run_mode_backtest(data, monthly_web, variant, gap_threshold=args.gap)
        rows.append(row)
        print(
            f"{variant:<16} return {row['total_return_pct']:+7.2f}%  "
            f"Sharpe {row['sharpe']:5.2f}  maxDD {row['max_drawdown_pct']:6.2f}%  "
            f"pause {row['paused_days']:4d}d  orders {row['orders']}"
        )

    bench = rows[0].get("benchmark_pct")
    if bench is not None:
        print(f"\nVTI buy & hold: {bench:+.2f}%")

    out = ROOT / f"wisdom_governor_{args.year_from}_{args.year_to}.csv"
    pd.DataFrame(rows).to_csv(out, index=False)
    print(f"\nSaved -> {out.name}")


if __name__ == "__main__":
    main()
