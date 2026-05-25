"""Backtest that mirrors run_all.py (regime + crypto pairs + equity MA50).

Default: 365-day simulation on daily bars (fetch if missing).
Live bot still uses 5m data via fetch_data.py without --daily.

Run:  python backtester.py
       python backtester.py --days 180
       python fetch_data.py --daily --days 365
"""

import argparse
import warnings

import numpy as np
import pandas as pd

import config
from fetch_data import fetch_daily_history
from modules.data_loader import load_close_matrix
from modules.market_context import (
    get_market_regime,
    get_price_sentiment,
    get_volatility,
)
from modules.pipeline_strategies import (
    COOLDOWN_SECONDS,
    run_crypto_strategy,
    run_equity_strategy,
    run_spy_strategy,
)
from modules.risk_management import RiskManager

warnings.filterwarnings("ignore", category=RuntimeWarning)

MIN_HISTORY = max(50, config.SPY_MA_WINDOW)
TX_COST = 0.001
BENCHMARK = "VTI"
# One daily bar ≈ one pipeline day; ~1h cooldown ≈ 1 session on daily data
DAILY_COOLDOWN_BARS = 1


class BacktestExecutor:
    """Simulates AlpacaExecutor sizing: min(10% cash, $10k) per order."""

    def __init__(self, portfolio, prices):
        self.portfolio = portfolio
        self.prices = prices
        self.orders = []

    def execute_order(self, symbol, side, notional=None, reduce_only=False):
        price = self.prices.get(symbol)
        if price is None or not np.isfinite(price) or price <= 0:
            return None
        order = self.portfolio.trade(
            symbol, side.lower(), price, tx_cost=TX_COST, notional=notional
        )
        if order:
            self.orders.append(order)
        return order


class BacktestPortfolio:
    def __init__(self, initial_capital=10000.0):
        self.cash = initial_capital
        self.initial_capital = initial_capital
        self.positions = {}

    def equity(self, prices):
        total = self.cash
        for symbol, qty in self.positions.items():
            p = prices.get(symbol)
            if p is not None and np.isfinite(p):
                total += qty * p
        return total

    def trade(self, symbol, side, price, tx_cost=TX_COST, notional=None):
        if notional is None:
            notional = round(
                min(
                    self.cash * config.RISK_PER_TRADE,
                    config.MAX_NOTIONAL_PER_ORDER,
                    self.cash * 0.95,
                ),
                2,
            )
            notional = max(config.MIN_NOTIONAL, notional)
        if side == "buy":
            if notional < 1 or self.cash < notional:
                return None
            cost = notional * (1 + tx_cost)
            if cost > self.cash:
                return None
            qty = notional / price
            self.cash -= cost
            self.positions[symbol] = self.positions.get(symbol, 0) + qty
            return {"symbol": symbol, "side": "buy", "qty": qty, "notional": notional}
        if side == "sell":
            qty = self.positions.get(symbol, 0)
            if qty <= 0:
                return None
            sell_notional = min(notional, qty * price)
            sell_qty = sell_notional / price
            proceeds = sell_notional * (1 - tx_cost)
            self.cash += proceeds
            self.positions[symbol] = qty - sell_qty
            if self.positions[symbol] < 1e-9:
                del self.positions[symbol]
            return {"symbol": symbol, "side": "sell", "qty": sell_qty, "notional": sell_notional}
        return None


def _benchmark_return(data, start_idx):
    if BENCHMARK not in data.columns:
        return None
    col = data[BENCHMARK].iloc[start_idx:].dropna()
    if len(col) < 2 or col.iloc[0] <= 0:
        return None
    return (col.iloc[-1] / col.iloc[0] - 1) * 100


def _ensure_daily_data(days, refresh=False, use_max=False):
    if use_max:
        if not refresh:
            data = load_close_matrix(interval="1d")
            if len(data) >= MIN_HISTORY + 10:
                return data
        print("--- Downloading max daily history (may take a few minutes) ---")
        fetch_daily_history(use_max=True)
        return load_close_matrix(interval="1d")
    min_rows = max(MIN_HISTORY + 10, int(days * 0.85))
    if not refresh:
        data = load_close_matrix(interval="1d", days=days)
        if len(data) >= min_rows:
            return data
    print(f"--- Downloading {days} days of daily history ---")
    fetch_daily_history(days)
    return load_close_matrix(interval="1d", days=days)


def run_performance_test(days=None, refresh=False, use_max=False):
    if use_max:
        print("--- STARTING FUND BACKTEST (max available daily history) ---")
    else:
        days = days or config.BACKTEST_DAYS
        print(f"--- STARTING FUND BACKTEST ({days} days) ---")
    try:
        data = _ensure_daily_data(days or 0, refresh=refresh, use_max=use_max)
    except Exception as e:
        print("Database error: " + str(e))
        return
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} rows; got {len(data)}.")
        print("Run: python fetch_data.py --daily --max")
        return

    start_date = data.index[MIN_HISTORY]
    end_date = data.index[-1]
    cooldown_bars = DAILY_COOLDOWN_BARS
    sharpe_scale = np.sqrt(252)
    bar_label = "daily bars"
    sim_days = (end_date - start_date).days
    progress_step = max(50, len(data) // 20)

    print(f"Loaded {len(data.columns)} tickers over {len(data)} {bar_label}.")
    print(f"Simulation: {start_date.date()} to {end_date.date()}")
    print(f"Cooldown: {cooldown_bars} bar(s) (~{COOLDOWN_SECONDS // 60} min live logic)")

    portfolio = BacktestPortfolio()
    pair_cooldown = {}
    risk_manager = RiskManager(max_drawdown_pct=config.MAX_DRAWDOWN_PCT)
    equity_curve = []
    regime_counts = {}
    total_crypto = 0
    total_equity = 0
    total_spy = 0
    total_orders = 0
    halted = False

    for i in range(MIN_HISTORY, len(data)):
        window = data.iloc[: i + 1]
        prices = window.iloc[-1]
        eq = portfolio.equity(prices)
        equity_curve.append(eq)

        if halted or not risk_manager.check_drawdown(eq):
            if not halted:
                halted = True
                print(f"!!! RISK HALT at {data.index[i].date()} (equity ${round(eq, 2)}) !!!")
            continue

        sentiment = get_price_sentiment(window)
        vol = get_volatility(window)
        regime = get_market_regime(sentiment, vol)
        regime_counts[regime] = regime_counts.get(regime, 0) + 1

        executor = BacktestExecutor(portfolio, prices)
        total_crypto += run_crypto_strategy(
            window,
            executor,
            regime,
            i,
            pair_cooldown,
            cooldown_bars=cooldown_bars,
            volatility=vol,
        )
        total_spy += run_spy_strategy(
            window,
            executor,
            regime,
            i,
            pair_cooldown,
            cooldown_bars=cooldown_bars,
        )
        total_equity += run_equity_strategy(
            window,
            executor,
            regime,
            i,
            pair_cooldown,
            cooldown_bars=cooldown_bars,
        )
        total_orders += len(executor.orders)

        if i % progress_step == 0:
            print(
                f"{data.index[i].date()} ({i}/{len(data)}) | "
                f"equity ${round(eq, 2)} | {regime}"
            )

    curve = pd.Series(equity_curve)
    returns = curve.pct_change().dropna()
    total_ret = (curve.iloc[-1] / portfolio.initial_capital - 1) * 100
    sharpe = (
        (returns.mean() / returns.std()) * sharpe_scale if returns.std() != 0 else 0
    )
    max_dd = ((curve / curve.cummax()) - 1).min() * 100
    bench = _benchmark_return(data, MIN_HISTORY)

    print("--- FUND BACKTEST REPORT (SPY + vol-gated crypto + NYSE) ---")
    print(
        f"Simulation:       {start_date.date()} to {end_date.date()} "
        f"(~{sim_days} days, {len(data)} {bar_label})"
    )
    print(
        f"Sleeves:          SPY {config.SPY_SLEEVE_CAP_PCT:.0%} | "
        f"crypto {config.CRYPTO_SLEEVE_CAP_PCT:.0%} | "
        f"NYSE {config.NYSE_SLEEVE_CAP_PCT:.0%} | "
        f"cash {config.FUND_CASH_BUFFER_PCT:.0%}"
    )
    print(f"Crypto vol-only:  {config.CRYPTO_VOL_ONLY}")
    print(f"Final Equity:     ${round(curve.iloc[-1], 2)}")
    print(f"Total Return:     {round(total_ret, 2)}%")
    if bench is not None:
        print(f"VTI Buy & Hold:   {round(bench, 2)}%")
    print(f"Sharpe Ratio:     {round(sharpe, 2)}")
    print(f"Max Drawdown:     {round(max_dd, 2)}%")
    print(f"SPY signals:      {total_spy}")
    print(f"Crypto signals:   {total_crypto}")
    print(f"NYSE signals:     {total_equity}")
    print(f"Total orders:     {total_orders}")
    print("Regime distribution:")
    for name, count in sorted(regime_counts.items(), key=lambda x: -x[1]):
        print(f"  {name}: {count}")
    print("---------------------------------------------------")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest run_all.py pipeline")
    parser.add_argument(
        "--days",
        type=int,
        default=config.BACKTEST_DAYS,
        help=f"Simulation length in calendar days (default: {config.BACKTEST_DAYS})",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-download daily history before running",
    )
    parser.add_argument(
        "--max",
        action="store_true",
        help="Use maximum available daily history (full universe, yfinance max)",
    )
    args = parser.parse_args()
    run_performance_test(days=args.days, refresh=args.refresh, use_max=args.max)
