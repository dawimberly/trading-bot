"""Screen US equities from Alpaca and rank by momentum / volatility / trend.

Pulls active tradable NYSE/NASDAQ/ARCA symbols from Alpaca, filters by price
and volume, scores survivors, and writes the top 75 to data/screener_universe.json.

Run from repo root:
  python scripts/analysis/universe_screener.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
from dotenv import find_dotenv, load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config

OUTPUT_PATH = ROOT / "data" / "screener_universe.json"
ALLOWED_EXCHANGES = frozenset({"NYSE", "NASDAQ", "ARCA"})
MIN_PRICE = 5.0
MIN_AVG_VOLUME = 500_000
LOOKBACK = 20
MA_WINDOW = 50
TOP_N = 75
PRINT_TOP = 20
BATCH_SIZE = 80
YFINANCE_PERIOD = "120d"
WEIGHT_MOMENTUM = 0.40
WEIGHT_VOLATILITY = 0.30
WEIGHT_TREND = 0.30


def _load_env() -> None:
    env_override = os.getenv("PYTHONTRADING_ENV_FILE", "").strip()
    if env_override and os.path.isfile(env_override):
        load_dotenv(env_override, override=True)
    else:
        load_dotenv(find_dotenv())


def _alpaca_credentials() -> tuple[str, str, bool]:
    """Return (api_key, secret_key, paper). Prefers APCA_*; falls back to PAPER_APCA_*."""
    apca_key = os.getenv("APCA_API_KEY_ID", "").strip() or os.getenv("ALPACA_API_KEY", "").strip()
    apca_secret = (
        os.getenv("APCA_API_SECRET_KEY", "").strip()
        or os.getenv("ALPACA_SECRET_KEY", "").strip()
    )
    if apca_key and apca_secret:
        paper = os.getenv("PAPER_TRADING", "true").lower() in ("1", "true", "yes")
        return apca_key, apca_secret, paper

    paper_key = os.getenv("PAPER_APCA_API_KEY_ID", "").strip()
    paper_secret = os.getenv("PAPER_APCA_API_SECRET_KEY", "").strip()
    if paper_key and paper_secret:
        return paper_key, paper_secret, True

    raise ValueError(
        "Alpaca credentials missing. Set APCA_* or PAPER_APCA_* in .env"
    )


def fetch_alpaca_symbols() -> list[str]:
    """Active, tradable US equities on NYSE, NASDAQ, or ARCA."""
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest

    api_key, secret_key, paper = _alpaca_credentials()
    client = TradingClient(api_key, secret_key, paper=paper)
    request = GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
    assets = client.get_all_assets(request)

    symbols: list[str] = []
    for asset in assets:
        if not getattr(asset, "tradable", False):
            continue
        exchange = getattr(asset, "exchange", None)
        if exchange is None:
            continue
        exch = exchange.value if hasattr(exchange, "value") else str(exchange)
        if exch not in ALLOWED_EXCHANGES:
            continue
        symbol = str(getattr(asset, "symbol", "") or "").strip().upper()
        if symbol:
            symbols.append(symbol)
    return sorted(set(symbols))


def _normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    rename = {}
    for col in df.columns:
        low = str(col).lower()
        if low == "open":
            rename[col] = "Open"
        elif low == "high":
            rename[col] = "High"
        elif low == "low":
            rename[col] = "Low"
        elif low == "close":
            rename[col] = "Close"
        elif low == "volume":
            rename[col] = "Volume"
    df = df.rename(columns=rename)
    needed = {"Open", "High", "Low", "Close", "Volume"}
    if not needed.issubset(df.columns):
        return None
    out = df[list(needed)].copy()
    out.index = pd.to_datetime(out.index)
    out = out.sort_index().dropna(how="all")
    for col in needed:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["Close", "Volume"])
    return out if len(out) >= MA_WINDOW else None


def _load_db_daily(symbol: str, conn: sqlite3.Connection) -> pd.DataFrame | None:
    table = f"{symbol}_daily"
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if not row:
        return None
    frame = pd.read_sql_query(f'SELECT Date, Close FROM "{table}" ORDER BY Date', conn)
    if frame.empty or len(frame) < MA_WINDOW:
        return None
    frame["Date"] = pd.to_datetime(frame["Date"])
    frame = frame.set_index("Date").sort_index()
    close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
    if close.empty:
        return None
    # DB stores close only — volume/ATR need yfinance; return None to force yfinance path.
    return None


def _fetch_yfinance_batch(symbols: list[str]) -> dict[str, pd.DataFrame]:
    if not symbols:
        return {}
    out: dict[str, pd.DataFrame] = {}
    if len(symbols) == 1:
        sym = symbols[0]
        try:
            raw = yf.download(sym, period=YFINANCE_PERIOD, progress=False, auto_adjust=True)
            frame = _normalize_ohlcv(raw)
            if frame is not None:
                out[sym] = frame
        except Exception:
            pass
        return out

    try:
        raw = yf.download(
            symbols,
            period=YFINANCE_PERIOD,
            group_by="ticker",
            progress=False,
            auto_adjust=True,
            threads=True,
        )
    except Exception:
        return out

    if raw is None or raw.empty:
        return out

    if isinstance(raw.columns, pd.MultiIndex):
        for sym in symbols:
            if sym not in raw.columns.get_level_values(0):
                continue
            try:
                chunk = raw[sym].copy()
                frame = _normalize_ohlcv(chunk)
                if frame is not None:
                    out[sym] = frame
            except Exception:
                continue
    else:
        sym = symbols[0]
        frame = _normalize_ohlcv(raw)
        if frame is not None:
            out[sym] = frame
    return out


def _atr_pct(frame: pd.DataFrame, window: int = LOOKBACK) -> float:
    high = frame["High"]
    low = frame["Low"]
    close = frame["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(window).mean().iloc[-1]
    price = float(close.iloc[-1])
    if not np.isfinite(atr) or price <= 0:
        return 0.0
    return float(atr / price)


def _metrics(frame: pd.DataFrame) -> dict[str, float] | None:
    if len(frame) < MA_WINDOW:
        return None
    close = frame["Close"]
    volume = frame["Volume"]
    price = float(close.iloc[-1])
    if price <= MIN_PRICE:
        return None
    avg_vol = float(volume.tail(LOOKBACK).mean())
    if avg_vol < MIN_AVG_VOLUME:
        return None
    if len(close) < LOOKBACK + 1:
        return None
    momentum = float(close.iloc[-1] / close.iloc[-LOOKBACK - 1] - 1.0)
    atr_pct = _atr_pct(frame, LOOKBACK)
    ma50 = float(close.rolling(MA_WINDOW).mean().iloc[-1])
    if ma50 <= 0:
        return None
    trend = float(price / ma50 - 1.0)
    return {
        "price": price,
        "avg_volume": avg_vol,
        "momentum": momentum,
        "atr_pct": atr_pct,
        "trend": trend,
    }


def _percentile_rank(values: np.ndarray) -> np.ndarray:
    n = len(values)
    if n <= 1:
        return np.ones(n)
    order = values.argsort().argsort()
    return order / (n - 1)


def score_candidates(rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    momentum = np.array([r["momentum"] for r in rows], dtype=float)
    atr_pct = np.array([r["atr_pct"] for r in rows], dtype=float)
    trend = np.array([r["trend"] for r in rows], dtype=float)

    mom_rank = _percentile_rank(momentum)
    vol_rank = _percentile_rank(atr_pct)
    trend_rank = _percentile_rank(trend)

    scored = []
    for i, row in enumerate(rows):
        composite = (
            WEIGHT_MOMENTUM * mom_rank[i]
            + WEIGHT_VOLATILITY * vol_rank[i]
            + WEIGHT_TREND * trend_rank[i]
        )
        scored.append(
            {
                "ticker": row["ticker"],
                "score": round(float(composite), 6),
                "momentum": round(float(row["momentum"]), 6),
                "atr_pct": round(float(row["atr_pct"]), 6),
                "trend": round(float(row["trend"]), 6),
                "price": round(float(row["price"]), 4),
                "avg_volume": int(row["avg_volume"]),
            }
        )
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored


def run_screener() -> dict:
    _load_env()
    symbols = fetch_alpaca_symbols()
    print(f"Alpaca universe: {len(symbols)} active tradable symbols")

    candidates: list[dict] = []
    db_path = ROOT / config.DB_PATH
    conn = sqlite3.connect(db_path) if db_path.is_file() else None
    try:
        for start in range(0, len(symbols), BATCH_SIZE):
            batch = symbols[start : start + BATCH_SIZE]
            print(
                f"Fetching batch {start // BATCH_SIZE + 1}/"
                f"{(len(symbols) + BATCH_SIZE - 1) // BATCH_SIZE} "
                f"({len(batch)} symbols)..."
            )
            frames = _fetch_yfinance_batch(batch)
            for sym in batch:
                frame = frames.get(sym)
                if frame is None and conn is not None:
                    _load_db_daily(sym, conn)
                if frame is None:
                    continue
                metrics = _metrics(frame)
                if metrics is None:
                    continue
                candidates.append({"ticker": sym, **metrics})
    finally:
        if conn is not None:
            conn.close()

    print(f"Passed filters (price > ${MIN_PRICE}, 20d avg vol > {MIN_AVG_VOLUME:,}): {len(candidates)}")
    score_table = score_candidates(candidates)
    top = score_table[:TOP_N]
    payload = {
        "tickers": [row["ticker"] for row in top],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "score_table": top,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote top {len(top)} tickers to {OUTPUT_PATH}")
    print(f"\nTop {PRINT_TOP} by composite score:")
    print(f"{'Rank':<5} {'Ticker':<8} {'Score':>8} {'Mom%':>8} {'ATR%':>8} {'Trend%':>8}")
    for i, row in enumerate(top[:PRINT_TOP], 1):
        print(
            f"{i:<5} {row['ticker']:<8} {row['score']:>8.4f} "
            f"{row['momentum'] * 100:>7.2f}% {row['atr_pct'] * 100:>7.2f}% "
            f"{row['trend'] * 100:>7.2f}%"
        )
    return payload


def main() -> int:
    warnings.filterwarnings("ignore", category=FutureWarning)
    try:
        run_screener()
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
