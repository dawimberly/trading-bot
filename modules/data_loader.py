"""Load close-price matrices from the SQLite market database."""

import os
import re
import sqlite3
import time

import pandas as pd

import config

_SQL_TABLE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_matrix_cache: dict[tuple, tuple[float, float, pd.DataFrame]] = {}


def safe_sql_table(name: str, *, allowed: set[str] | None = None) -> str:
    """Validate table name before SQL interpolation."""
    if not _SQL_TABLE_RE.match(name or ""):
        raise ValueError(f"Invalid SQL table name: {name!r}")
    if allowed is not None and name not in allowed:
        raise ValueError(f"Table not in allowlist: {name!r}")
    return name


def _cache_ttl_sec() -> int:
    return int(os.getenv("DB_MATRIX_CACHE_SEC", "120"))


def clear_close_matrix_cache() -> None:
    """Drop cached matrices (e.g. after DB refresh)."""
    _matrix_cache.clear()


def load_close_matrix(db_path=None, interval="5m", days=None, *, force_refresh=False):
    """
    Read ticker tables into a wide DataFrame of close prices.

    interval:
      - "5m" (default): live tables (excludes *_5m and *_daily suffixes)
      - "1d": backtest tables (*_daily suffix, column names without suffix)
    days: if set, keep only the last N rows after load
    force_refresh: bypass TTL cache
    """
    path = db_path or config.DB_PATH
    cache_key = (str(path), interval, days)
    mtime = os.path.getmtime(path) if os.path.isfile(path) else 0.0
    ttl = _cache_ttl_sec()
    now = time.time()
    if not force_refresh and ttl > 0:
        cached = _matrix_cache.get(cache_key)
        if cached is not None:
            cached_mtime, cached_at, frame = cached
            if cached_mtime == mtime and (now - cached_at) < ttl:
                return frame.copy()

    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [t[0] for t in cursor.fetchall()]

    if interval == "1d":
        allowed = {f"{ticker}_daily" for ticker in config.backtest_fetch_tickers()}
        tables = [t for t in tables if t.endswith("_daily") and t in allowed]
    else:
        tables = [t for t in tables if "_5m" not in t and "_daily" not in t]

    data = pd.DataFrame()
    for table in tables:
        safe_table = safe_sql_table(table)
        df = pd.read_sql(f'SELECT * FROM "{safe_table}"', conn)
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

    if ttl > 0:
        _matrix_cache[cache_key] = (mtime, now, data.copy())
    return data
