"""Download 5-minute OHLCV from yfinance and store in SQLite.

Run: python fetch_data.py
"""

import sqlite3

import pandas as pd
import yfinance as yf

import config


def fetch_and_store():
    conn = sqlite3.connect(config.DB_PATH)
    print(f"Fetching 5-minute data for {len(config.UNIVERSE)} tickers...")
    for ticker in config.UNIVERSE:
        try:
            df = yf.download(ticker, period="5d", interval="5m", progress=False)
            if df.empty:
                print("No data for " + ticker)
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df[["Close"]].copy()
            df.index.name = "Date"
            df.reset_index(inplace=True)
            df.to_sql(ticker, conn, if_exists="replace", index=False)
            print("Stored: " + ticker)
        except Exception as e:
            print(f"Failed: {ticker} - {e}")
    conn.close()
    print("Done. Database updated.")


if __name__ == "__main__":
    fetch_and_store()
