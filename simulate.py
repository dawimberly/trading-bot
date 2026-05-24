"""Pair z-score mean-reversion simulation over SQLite history.

Run: python simulate.py
"""

import numpy as np
import pandas as pd
import sqlite3

import config
from modules.data_loader import load_close_matrix
from modules.universe_manager import get_full_market_universe


def run_backtest(data):
    tickers = data.columns
    results = []
    for i in range(30, len(data)):
        current_date = data.index[i]
        window = data.iloc[i - 30 : i + 1]
        for idx_a in range(len(tickers)):
            for idx_b in range(idx_a + 1, len(tickers)):
                t1, t2 = tickers[idx_a], tickers[idx_b]
                spread = window[t1] - window[t2]
                z = (spread.iloc[-1] - spread.mean()) / spread.std()
                if abs(z) > 2.0:
                    pnl = 0
                    for day_offset in range(1, 6):
                        if i + day_offset < len(data):
                            future_spread = (
                                data.iloc[i + day_offset][t1]
                                - data.iloc[i + day_offset][t2]
                            )
                            if abs(future_spread) < abs(spread.iloc[-1]):
                                pnl = 1
                                break
                    results.append(
                        {"Date": current_date, "Pair": f"{t1}/{t2}", "Z": z, "Success": pnl}
                    )
    return pd.DataFrame(results)


if __name__ == "__main__":
    tickers = get_full_market_universe()
    data = load_close_matrix()
    if data.empty:
        conn = sqlite3.connect(config.DB_PATH)
        combined = pd.DataFrame()
        for ticker in tickers:
            try:
                df = pd.read_sql(f"SELECT * FROM '{ticker}'", conn)
                price_col = next(
                    (c for c in df.columns if "close" in c.lower()), None
                )
                if price_col:
                    df["Date"] = pd.to_datetime(df["Date"])
                    df.set_index("Date", inplace=True)
                    combined[ticker] = df[price_col]
            except Exception:
                continue
        conn.close()
        data = combined.ffill().dropna()
    print("--- Running 365-Day Backtest Simulation ---")
    history = run_backtest(data)
    success_rate = history["Success"].mean() * 100 if len(history) else 0
    print(f"Total signals: {len(history)}")
    print(f"Strategy Mean Reversion Success Rate: {success_rate:.2f}%")
    print("\nSample of signals:")
    print(history.head(10))
