"""Download market OHLCV from yfinance into SQLite.

Run live 5m refresh:  python fetch_data.py
Run 365d daily hist:  python fetch_data.py --daily --days 365
"""

import argparse
import sqlite3

import pandas as pd
import yfinance as yf

import config


def _normalize_df(df):
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    close_col = next((c for c in df.columns if str(c).lower() == "close"), None)
    if close_col is None:
        return pd.DataFrame()
    out = df[[close_col]].copy()
    out.columns = ["Close"]
    out.index.name = "Date"
    return out.reset_index()


def fetch_and_store():
    """Live pipeline: 5-minute bars (~5 days, yfinance intraday limit)."""
    conn = sqlite3.connect(config.DB_PATH)
    print(f"Fetching 5-minute data for {len(config.UNIVERSE)} tickers...")
    for ticker in config.UNIVERSE:
        try:
            df = yf.download(ticker, period="5d", interval="5m", progress=False)
            df = _normalize_df(df)
            if df.empty:
                print("No data for " + ticker)
                continue
            df.to_sql(ticker, conn, if_exists="replace", index=False)
            print("Stored: " + ticker)
        except Exception as e:
            print(f"Failed: {ticker} - {e}")
    conn.close()
    print("Done. Database updated.")


def fetch_daily_history(days=None):
    """Backtest pipeline: daily bars for up to ~2 years (365d default)."""
    days = days or config.BACKTEST_DAYS
    conn = sqlite3.connect(config.DB_PATH)
    print(f"Fetching {days}-day daily data for {len(config.UNIVERSE)} tickers...")
    for ticker in config.UNIVERSE:
        table = f"{ticker}_daily"
        try:
            df = yf.download(
                ticker,
                period=f"{days}d",
                interval="1d",
                progress=False,
                auto_adjust=True,
            )
            df = _normalize_df(df)
            if df.empty:
                print("No data for " + ticker)
                continue
            df.to_sql(table, conn, if_exists="replace", index=False)
            print(f"Stored: {table} ({len(df)} rows)")
        except Exception as e:
            print(f"Failed: {ticker} - {e}")
    conn.close()
    print("Done. Daily history updated.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download market data into SQLite")
    parser.add_argument(
        "--daily",
        action="store_true",
        help="Fetch daily bars for backtesting (default: 5m bars for live bot)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=config.BACKTEST_DAYS,
        help=f"Days of daily history (default: {config.BACKTEST_DAYS})",
    )
    args = parser.parse_args()
    if args.daily:
        fetch_daily_history(args.days)
    else:
        fetch_and_store()
