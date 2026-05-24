"""Z-score pair backtest using advisor ranker and SQLite price data.

Run: python backtester.py
"""

import sqlite3
import warnings

import numpy as np
import pandas as pd

import config
from modules.advisor_ranker import get_ranked_targets
from modules.data_loader import load_close_matrix
from modules.mock_executor import MockExecutor

warnings.filterwarnings("ignore", category=RuntimeWarning)


def run_performance_test():
    print("--- STARTING Z-SCORE BACKTEST ---")
    try:
        data = load_close_matrix()
    except Exception as e:
        print("Database error: " + str(e))
        return
    print(f"Loaded {len(data.columns)} tickers over {len(data)} rows.")
    initial_capital = 10000.0
    capital = initial_capital
    equity_curve = [initial_capital]
    tx_cost = 0.001
    step = 5
    executor = MockExecutor()
    for i in range(30, len(data), step):
        window = data.iloc[i - 30 : i]
        current_prices = data.iloc[i]
        prev_prices = data.iloc[i - 1]
        targets = get_ranked_targets(data.columns.tolist(), window)
        if i % 200 == 0:
            print(f"Row {i} of {len(data)}...")
        if targets:
            top_assets = list(
                set([t[0] for t in targets[:5]] + [t[1] for t in targets[:5]])
            )
            valid_targets = [t for t in top_assets if t in data.columns]
            if valid_targets:
                alloc = (capital * 0.95) / len(valid_targets)
                daily_pnl = 0
                for ticker in valid_targets:
                    price_now = current_prices[ticker]
                    price_prev = prev_prices[ticker]
                    rolling_mean = window[ticker].mean()
                    if price_prev > 0 and pd.notnull(price_now) and pd.notnull(price_prev):
                        if price_now > rolling_mean:
                            pct_change = (price_now - price_prev) / price_prev
                            daily_pnl += (alloc * pct_change) - (alloc * tx_cost)
                            executor.execute_order(ticker, "buy", qty=1)
                capital += daily_pnl
        equity_curve.append(capital)
    curve = pd.Series(equity_curve)
    returns = curve.pct_change().dropna()
    total_ret = (curve.iloc[-1] / initial_capital - 1) * 100
    sharpe = (returns.mean() / returns.std()) * np.sqrt(252) if returns.std() != 0 else 0
    max_dd = ((curve / curve.cummax()) - 1).min() * 100
    print("--- Z-SCORE STRATEGY REPORT ---")
    print(f"Total Return:   {round(total_ret, 2)}%")
    print(f"Sharpe Ratio:   {round(sharpe, 2)}")
    print(f"Max Drawdown:   {round(max_dd, 2)}%")
    print(f"Mock orders:    {len(executor.orders)}")
    print("-------------------------------")


if __name__ == "__main__":
    run_performance_test()
