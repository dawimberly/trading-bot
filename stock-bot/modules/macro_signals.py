"""Daily macro signals for game plan (bonds, yields, stress)."""

from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd
import yfinance as yf

import config
from modules.data_loader import safe_sql_table
from modules.market_context import get_market_regime, get_price_sentiment, get_volatility
from modules.pipeline_strategies import _spy_market_up_signal

BEAR_REGIME = "RHYME_E: Steady_Bearish_Decline"
PANIC_REGIME = "RHYME_B: Panic_Volatility"
_daily_cache: pd.DataFrame | None = None


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
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


def _load_daily_column(col: str) -> pd.Series:
    table = safe_sql_table(f"{col}_daily")
    try:
        conn = sqlite3.connect(config.DB_PATH)
        df = pd.read_sql(f"SELECT * FROM '{table}'", conn)
        conn.close()
    except Exception:
        return pd.Series(dtype=float)
    target = next((c for c in df.columns if "close" in c.lower()), None)
    if target is None:
        return pd.Series(dtype=float)
    # robust date col detect (support Date/date/timestamp etc)
    date_col = None
    for c in df.columns:
        lc = str(c).lower()
        if lc in ("date", "timestamp", "datetime", "time", "dt") or "date" in lc:
            date_col = c
            break
    if date_col is None:
        # fallback to first non-close col
        for c in df.columns:
            if "close" not in str(c).lower():
                date_col = c
                break
    if date_col is None:
        date_col = df.columns[0]
    s = pd.to_numeric(df.set_index(date_col)[target], errors="coerce")
    s.index = pd.to_datetime(s.index, errors="coerce")
    return s.sort_index()


def _fetch_daily_yf(yf_ticker: str) -> pd.DataFrame:
    for period in ("10y", "max"):
        try:
            df = yf.download(
                yf_ticker, period=period, interval="1d", progress=False, auto_adjust=True
            )
            df = _normalize_df(df)
            if not df.empty:
                return df
        except Exception:
            continue
    return pd.DataFrame()


MACRO_DAILY_YF = {
    "SPY": "SPY",
    "TLT": "TLT",
    "^TNX": "TNX",
    "GLD": "GLD",
    "SLV": "SLV",
    "CPER": "CPER",
}


def ensure_macro_daily(refresh: bool = False) -> None:
    """Ensure daily tables for macro + metal signals exist in market_data.db."""
    for yf_ticker, col in MACRO_DAILY_YF.items():
        if not refresh:
            s = _load_daily_column(col)
            if len(s) >= 250:
                continue
        try:
            df = _fetch_daily_yf(yf_ticker)
            if df.empty:
                print(f"Macro daily fetch warning ({col}): no data")
                continue
            conn = sqlite3.connect(config.DB_PATH)
            df.to_sql(f"{col}_daily", conn, if_exists="replace", index=False)
            conn.close()
        except Exception as exc:
            print(f"Macro daily fetch warning ({col}): {exc}")


def load_daily_matrix(days: int = 400, refresh: bool = False) -> pd.DataFrame:
    """Wide daily closes for SPY, metals, TLT (+ fund cols from DB)."""
    global _daily_cache
    if refresh:
        _daily_cache = None
    ensure_macro_daily(refresh=refresh)
    from modules.data_loader import load_close_matrix

    data = load_close_matrix(interval="1d", days=days, force_refresh=refresh)
    for col in ("TLT", "TNX"):
        series = _load_daily_column(col)
        if not series.empty:
            data[col] = series.reindex(data.index).ffill()
    if not data.empty:
        _daily_cache = data
    return data


def bond_stress(window: pd.DataFrame) -> bool:
    if "TLT" not in window.columns:
        return False
    tlt = window["TLT"].dropna()
    if len(tlt) < 50:
        return False
    return float(tlt.iloc[-1]) < float(tlt.rolling(50).mean().iloc[-1])


def yield_gate_blocks(window: pd.DataFrame) -> bool:
    """Block new SPY buys when 10Y yield rising above MA50 (TLT weak as fallback)."""
    if not config.YIELD_GATE_ENABLED:
        return False
    if "TNX" in window.columns:
        y = window["TNX"].dropna()
        if len(y) >= 50:
            ma50 = float(y.rolling(50).mean().iloc[-1])
            return float(y.iloc[-1]) > ma50 and float(y.iloc[-1]) > float(y.iloc[-6])
    return bond_stress(window)


def macro_stress(window: pd.DataFrame, regime: str) -> bool:
    bullish, _ = _spy_market_up_signal(window, config.SPY_BOT_SYMBOL, config.SPY_MA_WINDOW)
    if not bullish:
        return True
    if regime in (BEAR_REGIME, PANIC_REGIME):
        return True
    return bond_stress(window)


def evaluate(daily: pd.DataFrame | None, regime: str) -> dict:
    """Return game-plan signal snapshot from daily bars."""
    if daily is None or daily.empty or len(daily) < max(50, config.SPY_MA_WINDOW):
        return {
            "ok": False,
            "stress": False,
            "yield_gate": False,
            "bond_stress": False,
            "spy_below_ma200": False,
        }
    window = daily
    bullish, _ = _spy_market_up_signal(window, config.SPY_BOT_SYMBOL, config.SPY_MA_WINDOW)
    return {
        "ok": True,
        "stress": macro_stress(window, regime),
        "yield_gate": yield_gate_blocks(window),
        "bond_stress": bond_stress(window),
        "spy_below_ma200": not bullish,
    }


def regime_from_daily(daily: pd.DataFrame) -> tuple[str, str]:
    """Price regime + vol from daily matrix (fallback if 5m window thin)."""
    fund_cols = [c for c in daily.columns if c in config.UNIVERSE]
    window = daily[fund_cols] if fund_cols else daily
    sentiment = get_price_sentiment(window)
    vol = get_volatility(window)
    return get_market_regime(sentiment, vol, apply_hysteresis=False), vol
