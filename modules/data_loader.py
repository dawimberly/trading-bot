"""Load close-price matrices from the SQLite market database."""

import sqlite3

import pandas as pd

import config


def load_close_matrix(db_path=None, interval="5m", days=None):
    """
    Read ticker tables into a wide DataFrame of close prices.

    interval:
      - "5m" (default): live tables (excludes *_5m and *_daily suffixes)
      - "1d": backtest tables (*_daily suffix, column names without suffix)
    days: if set, keep only the last N rows after load
    """
    path = db_path or config.DB_PATH
    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [t[0] for t in cursor.fetchall()]

    if interval == "1d":
        # Only fund-universe tickers (exclude macro-only tables like SH_daily that can
        # cap the shared index before newer sleeve bars are visible).
        allowed = {f"{ticker}_daily" for ticker in config.UNIVERSE}
        tables = [t for t in tables if t.endswith("_daily") and t in allowed]
    else:
        tables = [t for t in tables if "_5m" not in t and "_daily" not in t]

    data = pd.DataFrame()
    for table in tables:
        df = pd.read_sql(f"SELECT * FROM '{table}'", conn)
        target_col = next((c for c in df.columns if "close" in c.lower()), None)
        if not target_col:
            continue
        col = table.removesuffix("_daily") if interval == "1d" else table
        series = df.set_index("Date")[target_col]
        data[col] = pd.to_numeric(series, errors="coerce")

    conn.close()
    data.index = pd.to_datetime(data.index, errors="coerce")
    if data.index.duplicated().any():
        data = data[~data.index.duplicated(keep="last")]
    data = data.sort_index().ffill().dropna(how="all")
    if days is not None and len(data) > days:
        data = data.iloc[-days:]
    return data
