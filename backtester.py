"""Backtest that mirrors run_all.py (regime + crypto pairs + equity MA50).

Uses price-momentum sentiment (same fallback as live Tavily errors).
Data is 5m bars from market_data.db; one row = one pipeline cycle.

Run: python backtester.py
"""

import warnings

import numpy as np
import pandas as pd

import config
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
)
from modules.risk_management import RiskManager

warnings.filterwarnings("ignore", category=RuntimeWarning)

MIN_HISTORY = 50
TX_COST = 0.001
# 3600s cooldown on 5m bars
COOLDOWN_BARS = COOLDOWN_SECONDS // 300
BENCHMARK = "VTI"


class BacktestExecutor:
    """Simulates AlpacaExecutor sizing: min(10% cash, $10k) per order."""

    def __init__(self, portfolio, prices):
        self.portfolio = portfolio
        self.prices = prices
        self.orders = []

    def execute_order(self, symbol, side):
        price = self.prices.get(symbol)
        if price is None or not np.isfinite(price) or price <= 0:
            return None
        order = self.portfolio.trade(symbol, side.lower(), price, tx_cost=TX_COST)
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

    def trade(self, symbol, side, price, tx_cost=TX_COST):
        notional = round(min(self.cash * 0.10, 10000.0), 2)
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


def run_performance_test():
    print("--- STARTING run_all.py PIPELINE BACKTEST ---")
    try:
        data = load_close_matrix()
    except Exception as e:
        print("Database error: " + str(e))
        return
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} rows; got {len(data)}.")
        return

    print(f"Loaded {len(data.columns)} tickers over {len(data)} rows (5m bars).")
    print(f"Cooldown: {COOLDOWN_BARS} bars (~{COOLDOWN_SECONDS // 60} min)")

    portfolio = BacktestPortfolio()
    pair_cooldown = {}
    risk_manager = RiskManager(max_drawdown_pct=config.MAX_DRAWDOWN_PCT)
    equity_curve = []
    regime_counts = {}
    total_crypto = 0
    total_equity = 0
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
                print(f"!!! RISK HALT at bar {i} (equity ${round(eq, 2)}) !!!")
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
            cooldown_bars=COOLDOWN_BARS,
        )
        total_equity += run_equity_strategy(
            window,
            executor,
            regime,
            i,
            pair_cooldown,
            cooldown_bars=COOLDOWN_BARS,
        )
        total_orders += len(executor.orders)

        if i % 500 == 0:
            print(f"Bar {i} of {len(data)} | equity ${round(eq, 2)} | {regime}")

    curve = pd.Series(equity_curve)
    returns = curve.pct_change().dropna()
    total_ret = (curve.iloc[-1] / portfolio.initial_capital - 1) * 100
    sharpe = (
        (returns.mean() / returns.std()) * np.sqrt(252 * 78)
        if returns.std() != 0
        else 0
    )
    max_dd = ((curve / curve.cummax()) - 1).min() * 100
    bench = _benchmark_return(data, MIN_HISTORY)

    print("--- PIPELINE BACKTEST REPORT (mirrors run_all.py) ---")
    print(f"Final Equity:     ${round(curve.iloc[-1], 2)}")
    print(f"Total Return:     {round(total_ret, 2)}%")
    if bench is not None:
        print(f"VTI Buy & Hold:   {round(bench, 2)}%")
    print(f"Sharpe Ratio:     {round(sharpe, 2)}")
    print(f"Max Drawdown:     {round(max_dd, 2)}%")
    print(f"Crypto signals:   {total_crypto}")
    print(f"Equity signals:   {total_equity}")
    print(f"Total orders:     {total_orders}")
    print("Regime distribution:")
    for name, count in sorted(regime_counts.items(), key=lambda x: -x[1]):
        print(f"  {name}: {count}")
    print("---------------------------------------------------")


if __name__ == "__main__":
    run_performance_test()
