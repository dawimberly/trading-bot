"""Z-score pair backtest using advisor ranker and SQLite price data.

Run: python backtester.py
"""

import warnings

import numpy as np
import pandas as pd

from modules.advisor_ranker import get_ranked_targets
from modules.data_loader import load_close_matrix
from modules.mock_executor import MockExecutor

warnings.filterwarnings("ignore", category=RuntimeWarning)

LOOKBACK = 30
REBALANCE_STEP = 5
TX_COST = 0.001
Z_ENTRY = 1.5
TOP_PAIRS = 5


def _mean_reversion_longs(targets, z_entry=Z_ENTRY):
    """Pick long candidates from top pairs (positive z → buy B, negative z → buy A)."""
    scores = {}
    for asset_a, asset_b, z in targets[:TOP_PAIRS]:
        if abs(z) < z_entry:
            continue
        if z > 0:
            scores[asset_b] = scores.get(asset_b, 0) + abs(z)
        else:
            scores[asset_a] = scores.get(asset_a, 0) + abs(z)
    return sorted(scores, key=scores.get, reverse=True)


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
    rebalance_count = 0
    executor = MockExecutor()
    for i in range(LOOKBACK, len(data), REBALANCE_STEP):
        window = data.iloc[i - LOOKBACK : i]
        current_prices = data.iloc[i]
        prev_prices = data.iloc[i - 1]
        targets = get_ranked_targets(data.columns.tolist(), window)
        if i % 200 == 0:
            print(f"Row {i} of {len(data)}...")
        longs = _mean_reversion_longs(targets)
        if longs:
            rebalance_count += 1
            valid = [t for t in longs if t in data.columns]
            if valid:
                alloc = (capital * 0.95) / len(valid)
                daily_pnl = 0
                for ticker in valid:
                    price_now = current_prices[ticker]
                    price_prev = prev_prices[ticker]
                    if price_prev > 0 and pd.notnull(price_now) and pd.notnull(price_prev):
                        pct_change = (price_now - price_prev) / price_prev
                        daily_pnl += (alloc * pct_change) - (alloc * TX_COST)
                        qty = round(alloc / price_now, 4) if price_now > 0 else 0
                        executor.execute_order(ticker, "buy", qty=qty, notional=alloc)
                capital += daily_pnl
        equity_curve.append(capital)
    curve = pd.Series(equity_curve)
    returns = curve.pct_change().dropna()
    total_ret = (curve.iloc[-1] / initial_capital - 1) * 100
    periods_per_year = 252 / REBALANCE_STEP
    sharpe = (
        (returns.mean() / returns.std()) * np.sqrt(periods_per_year)
        if returns.std() != 0
        else 0
    )
    max_dd = ((curve / curve.cummax()) - 1).min() * 100
    print("--- Z-SCORE STRATEGY REPORT ---")
    print(f"Final Equity:   ${round(curve.iloc[-1], 2)}")
    print(f"Total Return:   {round(total_ret, 2)}%")
    print(f"Sharpe Ratio:   {round(sharpe, 2)}")
    print(f"Max Drawdown:   {round(max_dd, 2)}%")
    print(f"Rebalances:     {rebalance_count}")
    print(f"Mock orders:    {len(executor.orders)}")
    print("-------------------------------")


if __name__ == "__main__":
    run_performance_test()
