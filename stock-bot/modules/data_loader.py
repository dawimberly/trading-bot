"""Load close-price matrices from the SQLite market database."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import config

logger = logging.getLogger(__name__)

DEEP_CACHE_DIR = Path(os.getenv("BACKTEST_DISK_CACHE_DIR", "data/cache/backtest"))

# yfinance ticker aliases (macro / index symbols stored under short names).
_YF_SYMBOL_MAP = {
    "TNX": "^TNX",
    "VIX": "^VIX",
}

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


def _close_column(conn: sqlite3.Connection, table: str) -> str | None:
    safe_table = safe_sql_table(table)
    rows = conn.execute(f'PRAGMA table_info("{safe_table}")').fetchall()
    for _cid, name, *_rest in rows:
        if "close" in name.lower():
            return name
    return None


def _date_column(conn: sqlite3.Connection, table: str) -> str | None:
    """Detect the date/timestamp column name (robust to 'date', 'Date', 'timestamp' etc)."""
    safe_table = safe_sql_table(table)
    rows = conn.execute(f'PRAGMA table_info("{safe_table}")').fetchall()
    if not rows:
        return None
    # Prefer common names, case-insensitive match
    preferred = ("date", "timestamp", "datetime", "time", "dt")
    for _cid, name, *_rest in rows:
        n = str(name).lower()
        if n in preferred or "date" in n or "time" in n:
            return name
    # fallback: first column that does not look like close/volume
    for _cid, name, *_rest in rows:
        n = str(name).lower()
        if n not in ("close", "volume", "open", "high", "low", "adj close"):
            return name
    return rows[0][1]


def _load_table_close(conn: sqlite3.Connection, table: str) -> pd.Series | None:
    close_col = _close_column(conn, table)
    if not close_col:
        return None
    date_col = _date_column(conn, table) or "Date"
    safe_table = safe_sql_table(table)
    df = pd.read_sql(
        f'SELECT "{date_col}" AS Date, "{close_col}" AS Close FROM "{safe_table}"',
        conn,
    )
    if df.empty or "Date" not in df.columns:
        return None
    series = pd.to_numeric(df.set_index("Date")["Close"], errors="coerce")
    series.index = pd.to_datetime(series.index, utc=True, errors="coerce")
    if getattr(series.index, "tz", None) is not None:
        series.index = series.index.tz_convert(None)
    return series


def load_close_matrix(db_path=None, interval="5m", days=None, *, force_refresh=False):
    """
    Read ticker tables into a wide DataFrame of close prices.

    interval:
      - "5m" (default): live tables (excludes *_5m and *_daily suffixes)
      - "1d": backtest tables (*_daily suffix, column names without suffix)
    days: if set, keep only the last N rows after load
    force_refresh: bypass TTL cache
    """
    path = str(db_path or config.resolve_db_path())
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

    columns: dict[str, pd.Series] = {}
    for table in tables:
        series = _load_table_close(conn, table)
        if series is None:
            continue
        col = table.removesuffix("_daily") if interval == "1d" else table
        columns[col] = series

    conn.close()
    data = pd.DataFrame(columns) if columns else pd.DataFrame()
    if not data.empty:
        data.index = pd.to_datetime(data.index, utc=True, errors="coerce")
        if getattr(data.index, "tz", None) is not None:
            data.index = data.index.tz_convert(None)
        if data.index.duplicated().any():
            data = data[~data.index.duplicated(keep="last")]
        data = data.sort_index().ffill().dropna(how="all")
    if days is not None and len(data) > days:
        data = data.iloc[-days:]

    if ttl > 0:
        _matrix_cache[cache_key] = (mtime, now, data.copy())
    return data


def _deep_cache_stem(symbol: str) -> str:
    return str(symbol).replace("/", "-").replace("\\", "-")


def _normalize_daily_index(index: pd.Index) -> pd.DatetimeIndex:
    idx = pd.to_datetime(index, utc=True, errors="coerce")
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert(None)
    return pd.DatetimeIndex(idx).normalize()


def _yf_ticker(symbol: str) -> str:
    sym = str(symbol).strip()
    return _YF_SYMBOL_MAP.get(sym, sym)


def _deep_cache_paths(symbol: str) -> tuple[Path, Path]:
    stem = _deep_cache_stem(symbol)
    return (
        DEEP_CACHE_DIR / f"{stem}_deep.pkl",
        DEEP_CACHE_DIR / f"{stem}_deep.meta.json",
    )


def _load_deep_cache(symbol: str, *, max_years: int) -> pd.Series | None:
    data_path, meta_path = _deep_cache_paths(symbol)
    if not data_path.is_file() or not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if int(meta.get("max_years", 0)) != int(max_years):
            return None
        series = pd.read_pickle(data_path)
        if not isinstance(series, pd.Series) or series.empty:
            return None
        series.index = _normalize_daily_index(series.index)
        return series.sort_index()
    except Exception:
        return None


def _save_deep_cache(symbol: str, series: pd.Series, *, max_years: int) -> None:
    DEEP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data_path, meta_path = _deep_cache_paths(symbol)
    try:
        out = series.sort_index()
        out.to_pickle(data_path)
        meta_path.write_text(
            json.dumps(
                {
                    "symbol": symbol,
                    "max_years": int(max_years),
                    "rows": int(len(out)),
                    "start": out.index[0].isoformat() if len(out) else None,
                    "end": out.index[-1].isoformat() if len(out) else None,
                }
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("deep history cache write failed for %s: %s", symbol, exc)


def clear_deep_history_cache(symbol: str | None = None) -> None:
    """Remove cached deep-history pickles (all symbols or one)."""
    if not DEEP_CACHE_DIR.is_dir():
        return
    if symbol is not None:
        data_path, meta_path = _deep_cache_paths(symbol)
        data_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        return
    for path in DEEP_CACHE_DIR.glob("*_deep.pkl"):
        path.unlink(missing_ok=True)
    for path in DEEP_CACHE_DIR.glob("*_deep.meta.json"):
        path.unlink(missing_ok=True)


def _fetch_alpaca_daily_closes(symbol: str, *, start: datetime, end: datetime) -> pd.Series:
    """Best-effort Alpaca daily bars; empty series when unavailable."""
    if config.is_crypto(symbol):
        return pd.Series(dtype=float)
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        from modules.alpaca_client import get_trading_client

        get_trading_client(paper=True, allow_live=False)
        api_key, secret_key = config.get_alpaca_credentials(paper=True)
        data_client = StockHistoricalDataClient(api_key, secret_key)
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )
        bars = data_client.get_stock_bars(request)
        df = getattr(bars, "df", None)
        if df is None or df.empty:
            return pd.Series(dtype=float)
        if isinstance(df.index, pd.MultiIndex):
            try:
                df = df.xs(symbol, level="symbol")
            except KeyError:
                df = df.droplevel(0)
        close_col = next((c for c in df.columns if str(c).lower() == "close"), None)
        if close_col is None:
            return pd.Series(dtype=float)
        close = pd.to_numeric(df[close_col], errors="coerce").dropna()
        close.index = _normalize_daily_index(close.index)
        return close.sort_index()
    except Exception as exc:
        logger.debug("Alpaca deep history unavailable for %s: %s", symbol, exc)
        return pd.Series(dtype=float)


def _fetch_yfinance_daily_closes(symbol: str, *, max_years: int) -> pd.Series:
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not installed; run: pip install yfinance")
        return pd.Series(dtype=float)

    yf_sym = _yf_ticker(symbol)
    try:
        df = yf.download(
            yf_sym,
            period="max",
            interval="1d",
            progress=False,
            auto_adjust=True,
        )
    except Exception as exc:
        logger.warning("yfinance deep history failed for %s: %s", symbol, exc)
        return pd.Series(dtype=float)
    if df is None or df.empty:
        return pd.Series(dtype=float)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    close_col = next((c for c in df.columns if str(c).lower() == "close"), None)
    if close_col is None:
        return pd.Series(dtype=float)
    close = pd.to_numeric(df[close_col], errors="coerce").dropna()
    close.index = _normalize_daily_index(close.index)
    cutoff = pd.Timestamp.now().normalize() - pd.DateOffset(years=int(max_years))
    close = close.loc[close.index >= cutoff]
    return close.sort_index()


def _merge_daily_closes(alpaca: pd.Series, yfinance: pd.Series) -> pd.Series:
    """Merge Alpaca + yfinance daily closes; Alpaca wins on overlap."""
    a = alpaca.sort_index() if alpaca is not None and not alpaca.empty else pd.Series(dtype=float)
    y = yfinance.sort_index() if yfinance is not None and not yfinance.empty else pd.Series(dtype=float)
    if a.empty and y.empty:
        return pd.Series(dtype=float)
    if a.empty:
        out = y[~y.index.duplicated(keep="last")]
        return out.sort_index()
    if y.empty:
        out = a[~a.index.duplicated(keep="last")]
        return out.sort_index()
    merged = a.combine_first(y)
    merged = merged[~merged.index.duplicated(keep="last")]
    return merged.sort_index()


def fetch_deep_history(
    symbol: str,
    max_years: int = 20,
    *,
    refresh: bool = False,
) -> pd.Series:
    """Fetch deepest available daily closes (Alpaca first, yfinance fill); cache once per symbol."""
    symbol = str(symbol).strip()
    max_years = max(1, int(max_years))
    if not symbol:
        return pd.Series(dtype=float)
    if refresh:
        clear_deep_history_cache(symbol)
    cached = _load_deep_cache(symbol, max_years=max_years)
    if cached is not None and not cached.empty:
        return cached

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(max_years * 366) + 30)
    alpaca = _fetch_alpaca_daily_closes(symbol, start=start, end=end)
    yfinance = _fetch_yfinance_daily_closes(symbol, max_years=max_years)
    merged = _merge_daily_closes(alpaca, yfinance)
    if merged.empty:
        return merged
    _save_deep_cache(symbol, merged, max_years=max_years)
    return merged


def deep_history_symbol_set(trade_columns) -> list[str]:
    """Universe + benchmark/macro symbols needed for indicator warmup."""
    symbols = [str(c) for c in trade_columns if c]
    seen: set[str] = set()
    ordered: list[str] = []
    for sym in symbols:
        if sym not in seen:
            seen.add(sym)
            ordered.append(sym)
    for required in (config.SPY_BOT_SYMBOL, "VTI", "TLT", "TNX"):
        if required not in seen:
            seen.add(required)
            ordered.append(required)
    for col in getattr(config, "MACRO_DAILY_TICKERS", ()):
        if col not in seen:
            seen.add(col)
            ordered.append(str(col))
    try:
        from modules.sector_screener import sector_etf_symbols

        for etf in sector_etf_symbols():
            if etf not in seen:
                seen.add(etf)
                ordered.append(etf)
    except ImportError:
        pass
    if getattr(config, "DYNAMIC_SECTOR_SCREENER_ENABLED", False):
        try:
            from modules.sector_screener import sector_etf_symbols

            for etf in sector_etf_symbols():
                if etf not in seen:
                    seen.add(etf)
                    ordered.append(etf)
        except ImportError:
            pass
    return ordered


def load_deep_history_matrix(
    symbols: list[str],
    *,
    max_years: int = 20,
    refresh: bool = False,
) -> pd.DataFrame:
    """Wide daily close matrix from per-symbol deep history caches."""
    columns: dict[str, pd.Series] = {}
    for sym in dict.fromkeys(symbols):
        series = fetch_deep_history(sym, max_years=max_years, refresh=refresh)
        if series is not None and not series.empty:
            columns[sym] = series
    if not columns:
        return pd.DataFrame()
    data = pd.DataFrame(columns)
    data.index = _normalize_daily_index(data.index)
    data = data.sort_index().ffill().dropna(how="all")
    return data
