"""Download market OHLCV from yfinance into SQLite.

Run live 5m refresh:  python fetch_data.py
Run 365d daily hist:  python fetch_data.py --daily --days 365
"""

import argparse
import logging
import sqlite3

import pandas as pd
import yfinance as yf

import config
from modules.safe_io import safe_print

logger = logging.getLogger(__name__)


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


def fetch_and_store(tickers=None):
    """Live pipeline: 5-minute bars (~5 days, yfinance intraday limit)."""
    tickers = list(config.UNIVERSE if tickers is None else tickers)
    conn = sqlite3.connect(config.DB_PATH)
    safe_print(f"Fetching 5-minute data for {len(tickers)} tickers...")
    for ticker in tickers:
        try:
            df = yf.download(ticker, period="5d", interval="5m", progress=False)
            df = _normalize_df(df)
            if df.empty:
                safe_print("No data for " + ticker)
                continue
            df.to_sql(ticker, conn, if_exists="replace", index=False)
            safe_print("Stored: " + ticker)
        except Exception as e:
            logger.warning("fetch 5m failed for %s: %s", ticker, e)
            safe_print(f"Failed: {ticker} - {e}")
    conn.close()
    safe_print("Done. Database updated.")


def fetch_daily_history(days=None, use_max=False):
    """Backtest pipeline: daily bars. use_max=True requests full yfinance history per ticker."""
    conn = sqlite3.connect(config.DB_PATH)
    tickers = config.backtest_fetch_tickers()
    if use_max:
        print(f"Fetching max daily history for {len(tickers)} tickers...")
    else:
        days = days or config.BACKTEST_DAYS
        print(f"Fetching {days}-day daily data for {len(tickers)} tickers...")
    for ticker in tickers:
        table = f"{ticker}_daily"
        try:
            kwargs = dict(interval="1d", progress=False, auto_adjust=True)
            if use_max:
                kwargs["period"] = "max"
            else:
                kwargs["period"] = f"{days}d"
            df = yf.download(ticker, **kwargs)
            df = _normalize_df(df)
            if df.empty:
                safe_print("No data for " + ticker)
                continue
            df.to_sql(table, conn, if_exists="replace", index=False)
            safe_print(f"Stored: {table} ({len(df)} rows)")
        except Exception as e:
            logger.warning("fetch daily failed for %s: %s", ticker, e)
            safe_print(f"Failed: {ticker} - {e}")
    conn.close()
    safe_print("Done. Daily history updated.")


def fetch_daily_history_for_tickers(
    tickers,
    *,
    days=None,
    use_max=False,
):
    """Fetch daily bars for a subset (e.g. dynamic screener names not in static UNIVERSE)."""
    tickers = [t for t in dict.fromkeys(tickers) if t]
    if not tickers:
        return
    conn = sqlite3.connect(config.DB_PATH)
    if use_max:
        print(f"Fetching max daily history for {len(tickers)} screener tickers...")
    else:
        days = days or config.BACKTEST_DAYS
        print(f"Fetching {days}-day daily data for {len(tickers)} screener tickers...")
    for ticker in tickers:
        table = f"{ticker}_daily"
        try:
            kwargs = dict(interval="1d", progress=False, auto_adjust=True)
            if use_max:
                kwargs["period"] = "max"
            else:
                kwargs["period"] = f"{days}d"
            df = yf.download(ticker, **kwargs)
            df = _normalize_df(df)
            if df.empty:
                safe_print("No data for " + ticker)
                continue
            df.to_sql(table, conn, if_exists="replace", index=False)
            safe_print(f"Stored: {table} ({len(df)} rows)")
        except Exception as e:
            logger.warning("fetch daily (subset) failed for %s: %s", ticker, e)
            safe_print(f"Failed: {ticker} - {e}")
    conn.close()
    from modules.data_loader import clear_close_matrix_cache

    clear_close_matrix_cache()


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
    parser.add_argument(
        "--max",
        action="store_true",
        help="Fetch maximum available daily history (yfinance period=max)",
    )
    args = parser.parse_args()
    if args.daily:
        fetch_daily_history(args.days, use_max=args.max)
    else:
        fetch_and_store()
