"""Dynamic NYSE/NASDAQ universe screener (paper-first, standalone).

Pulls active US equities from Alpaca, filters by liquidity, scores by
momentum / volatility / trend, writes top 75 to data/screener_universe.json.

Run from stock-bot/:
  python scripts/analysis/universe_screener.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
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

OUTPUT_PATH = ROOT / config.SCREENER_UNIVERSE_PATH
LOOKBACK = 20
MA_WINDOW = 50
MIN_PRICE = 5.0
MIN_AVG_SHARE_VOLUME = 500_000
TOP_N = 75
PRINT_TOP = 20
BATCH_SIZE = 40
YFINANCE_PERIOD = "120d"
YFINANCE_BATCH_SLEEP_SEC = 1.5

EXCLUDED = frozenset(
    {"SPY", "QQQ", "IWM", "VTI", "GLD", "SLV", "CPER", "URA", "PPLT", "DBB", "GDX"}
)
ALLOWED_EXCHANGES = frozenset({"NYSE", "NASDAQ", "ARCA"})

# Common-stock tickers only (skip preferreds, warrants, units, class shares)
def _is_common_equity(symbol: str) -> bool:
    if not symbol or len(symbol) > 5:
        return False
    return symbol.isalpha()

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
    paper = os.getenv("PAPER_TRADING", "true").lower() in ("1", "true", "yes")
    paper_key = os.getenv("PAPER_APCA_API_KEY_ID", "").strip()
    paper_secret = os.getenv("PAPER_APCA_API_SECRET_KEY", "").strip()
    if paper and paper_key and paper_secret:
        return paper_key, paper_secret, True
    try:
        key, secret = config.get_alpaca_credentials()
        return key, secret, paper
    except ValueError:
        pass
    if paper_key and paper_secret:
        return paper_key, paper_secret, True
    raise ValueError("Alpaca credentials missing. Set APCA_* or PAPER_APCA_* in .env")


def fetch_alpaca_assets() -> dict[str, dict]:
    """Active tradable US equities (asset_class=us_equity, status=active)."""
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest

    api_key, secret_key, paper = _alpaca_credentials()
    client = TradingClient(api_key, secret_key, paper=paper)
    request = GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
    assets = client.get_all_assets(request)

    out: dict[str, dict] = {}
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
        if not symbol or symbol in EXCLUDED or not _is_common_equity(symbol):
            continue
        out[symbol] = {"exchange": exch}
    return out


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
        elif low == "date":
            rename[col] = "Date"
    df = df.rename(columns=rename)
    if "Date" in df.columns:
        df = df.set_index("Date")
    needed = {"Close", "Volume"}
    if not needed.issubset(df.columns):
        return None
    out = df.copy()
    out.index = pd.to_datetime(out.index, errors="coerce")
    out = out.sort_index().dropna(subset=["Close", "Volume"])
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    if "Open" not in out.columns:
        out["Open"] = out["Close"]
    if "High" not in out.columns:
        out["High"] = out["Close"]
    if "Low" not in out.columns:
        out["Low"] = out["Close"]
    return out if len(out) >= LOOKBACK + 1 else None


def _load_from_db(symbol: str) -> pd.DataFrame | None:
    """Load daily OHLCV from market_data.db when available."""
    db_path = ROOT / config.DB_PATH
    if not db_path.is_file():
        return None
    conn = sqlite3.connect(db_path)
    try:
        for table in (f"{symbol}_daily", symbol):
            try:
                info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            except sqlite3.Error:
                continue
            if not info:
                continue
            cols = {row[1] for row in info}
            close_col = next((c for c in cols if c.lower() == "close"), None)
            vol_col = next((c for c in cols if c.lower() == "volume"), None)
            if not close_col:
                continue
            date_col = "Date" if "Date" in cols else next(
                (c for c in cols if c.lower() in ("date", "datetime", "timestamp")), None
            )
            if not date_col:
                continue
            select = [f'"{date_col}" AS Date', f'"{close_col}" AS Close']
            for src, alias in (("Open", "Open"), ("High", "High"), ("Low", "Low")):
                col = next((c for c in cols if c.lower() == alias.lower()), None)
                if col:
                    select.append(f'"{col}" AS {alias}')
            if vol_col:
                select.append(f'"{vol_col}" AS Volume')
            df = pd.read_sql(f'SELECT {", ".join(select)} FROM "{table}"', conn)
            if df.empty:
                continue
            frame = _normalize_ohlcv(df)
            if frame is not None and len(frame) >= LOOKBACK + 1:
                if table == symbol and len(frame) > LOOKBACK * 4:
                    daily = frame.resample("D").agg(
                        {
                            "Open": "first",
                            "High": "max",
                            "Low": "min",
                            "Close": "last",
                            "Volume": "sum",
                        }
                    ).dropna(subset=["Close", "Volume"])
                    frame = _normalize_ohlcv(daily)
                if frame is not None:
                    return frame
    finally:
        conn.close()
    return None


def _fetch_alpaca_bars_batch(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Daily OHLCV from Alpaca data API (preferred over yfinance at scale)."""
    if not symbols:
        return {}
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        from datetime import datetime, timedelta, timezone

        api_key, secret_key, paper = _alpaca_credentials()
        client = StockHistoricalDataClient(api_key, secret_key)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=130)
        request = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame(1, TimeFrameUnit.Day),
            start=start,
            end=end,
        )
        bars = client.get_stock_bars(request)
        df = bars.df
        if df is None or df.empty:
            return {}
        out: dict[str, pd.DataFrame] = {}
        if isinstance(df.index, pd.MultiIndex):
            for sym in symbols:
                if sym not in df.index.get_level_values(0):
                    continue
                chunk = df.xs(sym, level=0).copy()
                chunk = chunk.rename(
                    columns={
                        "open": "Open",
                        "high": "High",
                        "low": "Low",
                        "close": "Close",
                        "volume": "Volume",
                    }
                )
                frame = _normalize_ohlcv(chunk)
                if frame is not None:
                    out[sym] = frame
        else:
            sym = symbols[0]
            chunk = df.rename(
                columns={
                    "open": "Open",
                    "high": "High",
                    "low": "Low",
                    "close": "Close",
                    "volume": "Volume",
                }
            )
            frame = _normalize_ohlcv(chunk)
            if frame is not None:
                out[sym] = frame
        return out
    except Exception:
        return {}


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
                frame = _normalize_ohlcv(raw[sym].copy())
                if frame is not None:
                    out[sym] = frame
            except Exception:
                continue
    else:
        frame = _normalize_ohlcv(raw)
        if frame is not None:
            out[symbols[0]] = frame
    return out


def _fetch_bars_batch(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Alpaca daily bars first, then yfinance for misses."""
    frames = _fetch_alpaca_bars_batch(symbols)
    missing = [s for s in symbols if s not in frames]
    if missing:
        frames.update(_fetch_yfinance_batch(missing))
    return frames


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


def _metrics(frame: pd.DataFrame, *, symbol: str, exchange: str) -> dict | None:
    close = frame["Close"]
    volume = frame["Volume"]
    price = float(close.iloc[-1])
    if price <= MIN_PRICE:
        return None
    avg_shares = float(volume.tail(LOOKBACK).mean())
    if avg_shares < MIN_AVG_SHARE_VOLUME:
        return None
    if len(close) < LOOKBACK + 1:
        return None
    momentum = float(close.iloc[-1] / close.iloc[-LOOKBACK - 1] - 1.0)
    ma50 = float(close.rolling(min(MA_WINDOW, len(close))).mean().iloc[-1])
    if ma50 <= 0:
        return None
    trend = float(price / ma50 - 1.0)
    atr_pct = _atr_pct(frame, LOOKBACK)
    return {
        "ticker": symbol,
        "exchange": exchange,
        "price": price,
        "avg_volume": int(avg_shares),
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


def _discover_db_symbols() -> list[str]:
    """Tickers with daily (or intraday) tables in market_data.db."""
    db_path = ROOT / config.DB_PATH
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    finally:
        conn.close()
    out: set[str] = set()
    for (name,) in rows:
        if name.endswith("_daily"):
            out.add(name[: -len("_daily")].upper())
        elif _is_common_equity(name.upper()) and name.upper() not in EXCLUDED:
            out.add(name.upper())
    return sorted(out)


def _build_symbol_list(asset_map: dict[str, dict], *, full_scan: bool) -> list[str]:
    seed = set(config.equity_universe()) | set(_discover_db_symbols())
    seed = {s for s in seed if s not in EXCLUDED and _is_common_equity(s)}
    if full_scan:
        symbols = sorted(set(asset_map.keys()) | seed)
    else:
        extra = [s for s in asset_map if s not in seed][: max(0, 2500 - len(seed))]
        symbols = sorted(seed | set(extra))
    return symbols


def score_candidates(rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    momentum = np.array([r["momentum"] for r in rows], dtype=float)
    atr_pct = np.array([r["atr_pct"] for r in rows], dtype=float)
    trend = np.array([r["trend"] for r in rows], dtype=float)

    mom_rank = _percentile_rank(momentum)
    vol_rank = 1.0 - _percentile_rank(atr_pct)
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
                "momentum_rank": round(float(mom_rank[i]), 6),
                "atr_pct": round(float(row["atr_pct"]), 6),
                "volatility_rank": round(float(vol_rank[i]), 6),
                "trend": round(float(row["trend"]), 6),
                "trend_rank": round(float(trend_rank[i]), 6),
                "price": round(float(row["price"]), 4),
                "avg_volume": int(row["avg_volume"]),
                "exchange": row.get("exchange", ""),
            }
        )
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored


def run_screener(*, asset_map: dict[str, dict] | None = None, full_scan: bool = False) -> dict:
    _load_env()
    asset_map = asset_map or fetch_alpaca_assets()
    symbols = _build_symbol_list(asset_map, full_scan=full_scan)
    mode = "full Alpaca" if full_scan else "paper-first (UNIVERSE + DB + capped Alpaca)"
    print(f"Screener mode: {mode} | symbols to scan: {len(symbols)}")

    candidates: list[dict] = []
    db_hits = 0
    for start in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[start : start + BATCH_SIZE]
        print(
            f"Batch {start // BATCH_SIZE + 1}/"
            f"{(len(symbols) + BATCH_SIZE - 1) // BATCH_SIZE} ({len(batch)} symbols)..."
        )
        yf_needed: list[str] = []
        frames: dict[str, pd.DataFrame] = {}
        for sym in batch:
            frame = _load_from_db(sym)
            if frame is not None:
                frames[sym] = frame
                db_hits += 1
            else:
                yf_needed.append(sym)
        if yf_needed:
            frames.update(_fetch_yfinance_batch(yf_needed))
            time.sleep(YFINANCE_BATCH_SLEEP_SEC)
        for sym in batch:
            frame = frames.get(sym)
            if frame is None:
                continue
            metrics = _metrics(
                frame,
                symbol=sym,
                exchange=asset_map.get(sym, {}).get("exchange", ""),
            )
            if metrics is not None:
                candidates.append(metrics)

    print(
        f"Passed filters (price>${MIN_PRICE}, {MIN_AVG_SHARE_VOLUME/1e3:.0f}k avg share vol, "
        f"{LOOKBACK}d): {len(candidates)} | db bars used: {db_hits}"
    )
    score_table = score_candidates(candidates)
    top = score_table[:TOP_N]
    payload = {
        "tickers": [row["ticker"] for row in top],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "score_table": top,
        "filters": {
            "min_price": MIN_PRICE,
            "min_avg_share_volume": MIN_AVG_SHARE_VOLUME,
            "lookback_days": LOOKBACK,
            "weights": {
                "momentum": WEIGHT_MOMENTUM,
                "volatility": WEIGHT_VOLATILITY,
                "trend": WEIGHT_TREND,
            },
            "excluded_etfs": sorted(EXCLUDED),
            "top_n": TOP_N,
        },
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {len(top)} tickers to {OUTPUT_PATH}")
    print(f"\nTop {PRINT_TOP} by composite score (40% mom / 30% low-vol / 30% trend):")
    print(
        f"{'Rank':<5} {'Ticker':<8} {'Score':>7} {'Mom%':>8} {'ATR%':>7} "
        f"{'Trend%':>8} {'AvgVolK':>8}"
    )
    for i, row in enumerate(top[:PRINT_TOP], 1):
        print(
            f"{i:<5} {row['ticker']:<8} {row['score']:>7.4f} "
            f"{row['momentum'] * 100:>7.2f} {row['atr_pct'] * 100:>6.2f} "
            f"{row['trend'] * 100:>7.2f} {row['avg_volume'] / 1000:>7.0f}k"
        )
    return payload


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Dynamic NYSE/NASDAQ universe screener")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Scan all Alpaca common equities (~10k; slow, yfinance rate limits)",
    )
    args = parser.parse_args()
    warnings.filterwarnings("ignore", category=FutureWarning)
    try:
        run_screener(full_scan=args.full)
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
