"""Dynamic NYSE/NASDAQ universe screener (paper-first, standalone).

Uses market_data.db for OHLCV (fast, no API limits); yfinance only for gaps.
Scores by momentum / volatility / trend; writes top 15–20 to data/screener_universe.json.
Skips re-run when cache is fresher than 7 days (use --force to refresh).

Run from stock-bot/:
  python scripts/analysis/universe_screener.py
"""

from __future__ import annotations

import json
import logging
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
from modules.dynamic_universe import (
    MIN_PRICE,
    MIN_SHARE_VOLUME,
    STRICT_MIN_MOMENTUM_RANK,
    apply_sector_balance,
    apply_sticky_universe,
    effective_max_universe_size,
    effective_min_dollar_volume,
    effective_min_share_volume,
    effective_momentum_lookback,
    sector_for_symbol,
    strict_mode_active,
)

OUTPUT_PATH = ROOT / config.SCREENER_UNIVERSE_PATH
LOOKBACK = 20
MA_WINDOW = 50
MIN_AVG_SHARE_VOLUME = MIN_SHARE_VOLUME
TOP_N = int(os.getenv("SCREENER_TOP_N", str(effective_max_universe_size())))
PRINT_TOP = int(os.getenv("SCREENER_PRINT_TOP", "15"))
CACHE_MAX_AGE_DAYS = int(os.getenv("SCREENER_CACHE_DAYS", "7"))
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


def _suppress_yfinance_noise() -> None:
    """Quiet yfinance delisted / rate-limit chatter on stderr."""
    warnings.filterwarnings("ignore", category=FutureWarning)
    for name in ("yfinance", "yfinance.base", "yfinance.scrapers", "yfinance.scrapers.history"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


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


def _load_from_db(symbol: str, conn: sqlite3.Connection | None = None) -> pd.DataFrame | None:
    """Load daily OHLCV from market_data.db when available."""
    own_conn = conn is None
    if own_conn:
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
            if "Volume" not in df.columns:
                df["Volume"] = float(MIN_AVG_SHARE_VOLUME)
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
        if own_conn and conn is not None:
            conn.close()
    return None


def _preload_db_frames(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Bulk-load bars from market_data.db (one connection per batch)."""
    db_path = ROOT / config.DB_PATH
    if not db_path.is_file() or not symbols:
        return {}
    out: dict[str, pd.DataFrame] = {}
    conn = sqlite3.connect(db_path)
    try:
        for sym in symbols:
            frame = _load_from_db(sym, conn)
            if frame is not None:
                out[sym] = frame
    finally:
        conn.close()
    return out


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
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
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
    min_price = max(MIN_PRICE, 8.0) if strict_mode_active() else MIN_PRICE
    if price < min_price:
        return None
    lookback = effective_momentum_lookback()
    avg_shares = float(volume.tail(lookback).mean())
    min_shares = effective_min_share_volume()
    if avg_shares < min_shares:
        return None
    avg_dollar = avg_shares * price
    min_dollar = effective_min_dollar_volume()
    if avg_dollar < min_dollar:
        return None
    if len(close) < lookback + 1:
        return None
    momentum = float(close.iloc[-1] / close.iloc[-lookback - 1] - 1.0)
    mom_30_lb = 30
    if strict_mode_active():
        from modules.dynamic_universe import STRICT_MOMENTUM_LOOKBACK

        mom_30_lb = STRICT_MOMENTUM_LOOKBACK
    momentum_30d = (
        float(close.iloc[-1] / close.iloc[-mom_30_lb - 1] - 1.0)
        if len(close) >= mom_30_lb + 1
        else momentum
    )
    ma50 = float(close.rolling(min(MA_WINDOW, len(close))).mean().iloc[-1])
    if ma50 <= 0:
        return None
    trend = float(price / ma50 - 1.0)
    atr_pct = _atr_pct(frame, LOOKBACK)
    if strict_mode_active() and price < 10 and avg_dollar < 80_000_000:
        return None
    pattern_score_val = 0.0
    if config.effective_pattern_awareness_enabled():
        from modules.chart_patterns import detect_patterns, pattern_score

        pattern_score_val = pattern_score(
            detect_patterns(
                close,
                symbol=symbol,
                volume=volume,
                avg_volume=avg_shares,
            )
        )
    return {
        "ticker": symbol,
        "exchange": exchange,
        "price": price,
        "avg_volume": int(avg_shares),
        "avg_dollar_volume": int(avg_dollar),
        "momentum": momentum,
        "momentum_30d": momentum_30d,
        "atr_pct": atr_pct,
        "trend": trend,
        "pattern_score": pattern_score_val,
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
    db_syms = set(_discover_db_symbols())
    seed = set(config.equity_universe()) | db_syms
    seed = {s for s in seed if s not in EXCLUDED and _is_common_equity(s)}
    if full_scan:
        symbols = sorted(set(asset_map.keys()) | seed)
    else:
        # Prefer DB + static universe; cap Alpaca extras to limit yfinance calls
        extra_cap = max(0, 800 - len(seed))
        extra = [s for s in asset_map if s not in seed][:extra_cap]
        symbols = sorted(seed | set(extra))
    return symbols


def score_candidates(
    rows: list[dict],
    *,
    rotation_summary: dict | None = None,
) -> list[dict]:
    if not rows:
        return []
    momentum = np.array([r["momentum"] for r in rows], dtype=float)
    momentum_30d = np.array([r.get("momentum_30d", r["momentum"]) for r in rows], dtype=float)
    atr_pct = np.array([r["atr_pct"] for r in rows], dtype=float)
    trend = np.array([r["trend"] for r in rows], dtype=float)

    mom_rank = _percentile_rank(momentum)
    mom_30_rank = _percentile_rank(momentum_30d)
    vol_rank = 1.0 - _percentile_rank(atr_pct)
    trend_rank = _percentile_rank(trend)

    rotation_state = None
    if rotation_summary is not None and config.effective_sector_rotation_enabled():
        from modules.sector_rotation import evaluate_rotation_state

        impact = float(rotation_summary.get("news_impact_score") or 0.0)
        rotation_state = evaluate_rotation_state(
            rotation_summary,
            confidence=0.55 + 0.35 * impact if impact > 0 else 0.60,
        )

    scored = []
    for i, row in enumerate(rows):
        composite = (
            WEIGHT_MOMENTUM * mom_rank[i]
            + WEIGHT_VOLATILITY * vol_rank[i]
            + WEIGHT_TREND * trend_rank[i]
        )
        if rotation_state is not None:
            from modules.sector_rotation import score_multiplier

            composite *= score_multiplier(row["ticker"], rotation_state)
        if config.effective_pattern_awareness_enabled():
            from modules.chart_patterns import pattern_composite_multiplier

            composite *= pattern_composite_multiplier(row.get("pattern_score", 0.0))
        scored.append(
            {
                "ticker": row["ticker"],
                "score": round(float(composite), 6),
                "momentum": round(float(row["momentum"]), 6),
                "momentum_30d": round(float(row.get("momentum_30d", row["momentum"])), 6),
                "momentum_rank": round(float(mom_rank[i]), 6),
                "momentum_30d_rank": round(float(mom_30_rank[i]), 6),
                "atr_pct": round(float(row["atr_pct"]), 6),
                "volatility_rank": round(float(vol_rank[i]), 6),
                "trend": round(float(row["trend"]), 6),
                "trend_rank": round(float(trend_rank[i]), 6),
                "price": round(float(row["price"]), 4),
                "avg_volume": int(row["avg_volume"]),
                "avg_dollar_volume": int(row.get("avg_dollar_volume") or 0),
                "exchange": row.get("exchange", ""),
                "sector": sector_for_symbol(row["ticker"]),
            }
        )
    scored.sort(key=lambda r: r["score"], reverse=True)
    if strict_mode_active():
        scored = [
            r
            for r in scored
            if float(r.get("momentum_30d_rank") or 0) >= STRICT_MIN_MOMENTUM_RANK
        ]
        scored = apply_sector_balance(scored, max_size=TOP_N)
    return scored


def _cache_age_days() -> float | None:
    if not OUTPUT_PATH.is_file():
        return None
    try:
        payload = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        gen = payload.get("generated_at")
        if not gen:
            return None
        ts = datetime.fromisoformat(str(gen).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
    except Exception:
        return None


def _load_cached_payload() -> dict | None:
    if not OUTPUT_PATH.is_file():
        return None
    try:
        return json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def print_top_table(rows: list[dict], *, n: int = PRINT_TOP) -> None:
    print(f"\nTop {n} by composite score (40% mom / 30% low-vol / 30% trend):")
    print(
        f"{'Rank':<5} {'Ticker':<8} {'Score':>7} {'Mom%':>8} {'ATR%':>7} "
        f"{'Trend%':>8} {'AvgVolK':>8}"
    )
    for i, row in enumerate(rows[:n], 1):
        print(
            f"{i:<5} {row['ticker']:<8} {row['score']:>7.4f} "
            f"{row['momentum'] * 100:>7.2f} {row['atr_pct'] * 100:>6.2f} "
            f"{row['trend'] * 100:>7.2f} {row['avg_volume'] / 1000:>7.0f}k"
        )


def run_screener(
    *,
    asset_map: dict[str, dict] | None = None,
    full_scan: bool = False,
    previous_tickers: list[str] | None = None,
) -> dict:
    _load_env()
    asset_map = asset_map or fetch_alpaca_assets()
    symbols = _build_symbol_list(asset_map, full_scan=full_scan)
    mode = "full Alpaca" if full_scan else "paper-first (UNIVERSE + DB + capped Alpaca)"
    db_path = ROOT / config.DB_PATH
    print(
        f"Screener mode: {mode} | symbols to scan: {len(symbols)} | "
        f"db={'yes' if db_path.is_file() else 'missing'} | top_n={TOP_N}"
    )

    candidates: list[dict] = []
    db_hits = 0
    yf_hits = 0
    for start in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[start : start + BATCH_SIZE]
        print(
            f"Batch {start // BATCH_SIZE + 1}/"
            f"{(len(symbols) + BATCH_SIZE - 1) // BATCH_SIZE} ({len(batch)} symbols)..."
        )
        frames = _preload_db_frames(batch)
        db_hits += len(frames)
        yf_needed = [s for s in batch if s not in frames]
        if yf_needed:
            alpaca_frames = _fetch_alpaca_bars_batch(yf_needed)
            frames.update(alpaca_frames)
            yf_needed = [s for s in yf_needed if s not in frames]
        if yf_needed:
            frames.update(_fetch_yfinance_batch(yf_needed))
            yf_hits += len([s for s in yf_needed if s in frames])
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
        f"{LOOKBACK}d): {len(candidates)} | db bars: {db_hits} | yfinance fills: {yf_hits}"
    )
    rotation_summary = None
    if config.effective_sector_rotation_enabled():
        from modules.sector_rotation import build_screener_rotation_context

        rotation_summary = build_screener_rotation_context()
        from modules.sector_rotation import evaluate_rotation_state

        rot = evaluate_rotation_state(
            rotation_summary,
            confidence=0.55 + 0.35 * float(rotation_summary.get("news_impact_score") or 0.0),
        )
        print(f"Sector rotation: {rot.narrative[:120]}")
    score_table = score_candidates(candidates, rotation_summary=rotation_summary)
    top = apply_sticky_universe(score_table, previous_tickers, top_n=TOP_N)
    turnover = 0
    if previous_tickers:
        prev_set = {str(t).upper() for t in previous_tickers}
        cur_set = {r["ticker"] for r in top}
        turnover = len(cur_set ^ prev_set)
    payload = {
        "tickers": [row["ticker"] for row in top],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "score_table": top,
        "ipo_count": sum(1 for row in top if row.get("is_ipo")),
        "turnover_vs_prior": turnover,
        "filters": {
            "min_price": MIN_PRICE,
            "min_avg_share_volume": effective_min_share_volume(),
            "min_avg_dollar_volume": effective_min_dollar_volume(),
            "lookback_days": effective_momentum_lookback(),
            "strict_mode": strict_mode_active(),
            "sticky_keep": int(os.getenv("PAPER_UNIVERSE_STICKY_KEEP", "6")),
            "cache_max_age_days": CACHE_MAX_AGE_DAYS,
            "data_source": "market_data.db primary, Alpaca bars, yfinance fallback",
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
    print_top_table(top)
    return payload


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Dynamic NYSE/NASDAQ universe screener")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Scan all Alpaca common equities (~10k; slow, yfinance rate limits)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=f"Ignore cache and re-run even if {OUTPUT_PATH.name} is <{CACHE_MAX_AGE_DAYS}d old",
    )
    args = parser.parse_args()
    _suppress_yfinance_noise()
    _load_env()

    age = _cache_age_days()
    if not args.force and age is not None and age < CACHE_MAX_AGE_DAYS:
        cached = _load_cached_payload()
        if cached and cached.get("score_table"):
            print(
                f"Using cached universe ({OUTPUT_PATH.name}, age {age:.1f}d < {CACHE_MAX_AGE_DAYS}d). "
                f"Run with --force to refresh."
            )
            print_top_table(cached["score_table"])
            return 0

    try:
        run_screener(full_scan=args.full)
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
