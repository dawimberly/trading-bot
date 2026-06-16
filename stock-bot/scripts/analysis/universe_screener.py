"""Screen US equities from Alpaca and rank by momentum / liquidity / trend.

Pulls active tradable NYSE/NASDAQ/ARCA symbols from Alpaca, filters by price,
dollar volume, and volatility; detects recent IPOs; writes top names to
data/screener_universe.json.

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
from modules.dynamic_universe import (
    ALLOWED_EXCHANGES,
    HIGH_VOL_ATR_PCT,
    HIGH_VOL_POSITION_SCALE,
    IPO_MAX_TRADING_DAYS,
    IPO_MIN_TRADING_DAYS,
    IPO_POSITION_SCALE,
    MAX_ATR_PCT,
    MAX_IPO_SLOTS,
    MAX_UNIVERSE_SIZE,
    MIN_AVG_DOLLAR_VOLUME,
    MIN_PRICE,
    MIN_SHARE_VOLUME,
    MOMENTUM_LOOKBACK,
    STRICT_MAX_PER_SECTOR,
    STRICT_MAX_UNIVERSE_SIZE,
    STRICT_MIN_MOMENTUM_RANK,
    STRICT_MIN_UNIVERSE_SIZE,
    apply_sector_balance,
    effective_max_ipo_slots,
    effective_max_universe_size,
    effective_min_dollar_volume,
    effective_min_share_volume,
    effective_momentum_lookback,
    sector_for_symbol,
    strict_mode_active,
)

OUTPUT_PATH = ROOT / "data" / "screener_universe.json"
LOOKBACK = 20
MA_WINDOW = 50
PRINT_TOP = 20
BATCH_SIZE = 80
YFINANCE_PERIOD = "120d"
WEIGHT_LIQUIDITY = 0.30
WEIGHT_MOMENTUM = 0.35
WEIGHT_TREND = 0.25
WEIGHT_VOLATILITY = 0.10


def _load_env() -> None:
    env_override = os.getenv("PYTHONTRADING_ENV_FILE", "").strip()
    if env_override and os.path.isfile(env_override):
        load_dotenv(env_override, override=True)
    else:
        load_dotenv(find_dotenv())


def _alpaca_credentials() -> tuple[str, str, bool]:
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


def fetch_alpaca_assets() -> dict[str, dict]:
    """Active tradable US equities on NYSE, NASDAQ, or ARCA with borrow metadata."""
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
        if not symbol:
            continue
        etb = getattr(asset, "easy_to_borrow", None)
        out[symbol] = {
            "exchange": exch,
            "easy_to_borrow": None if etb is None else bool(etb),
        }
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
    return out if len(out) >= IPO_MIN_TRADING_DAYS else None


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


def _metrics(
    frame: pd.DataFrame, asset_meta: dict, *, symbol: str = ""
) -> dict[str, float | int | bool | str] | None:
    trading_days = len(frame)
    if trading_days < IPO_MIN_TRADING_DAYS:
        return None
    close = frame["Close"]
    volume = frame["Volume"]
    price = float(close.iloc[-1])
    if price <= MIN_PRICE:
        return None
    mom_lookback = effective_momentum_lookback()
    vol_lookback = max(LOOKBACK, mom_lookback)
    avg_shares = float(volume.tail(vol_lookback).mean())
    min_share_vol = effective_min_share_volume()
    if avg_shares < min_share_vol:
        return None
    avg_dollar_vol = avg_shares * price
    min_dollar_vol = effective_min_dollar_volume()
    if avg_dollar_vol < min_dollar_vol:
        return None
    atr_pct = _atr_pct(frame, LOOKBACK)
    is_ipo = trading_days < IPO_MAX_TRADING_DAYS
    if atr_pct > MAX_ATR_PCT and not is_ipo:
        return None
    if strict_mode_active():
        etb = asset_meta.get("easy_to_borrow")
        if etb is False:
            return None
    if len(close) < mom_lookback + 1:
        return None
    momentum = float(close.iloc[-1] / close.iloc[-mom_lookback - 1] - 1.0)
    ma50 = float(close.rolling(min(MA_WINDOW, len(close))).mean().iloc[-1])
    if ma50 <= 0:
        return None
    trend = float(price / ma50 - 1.0)
    position_scale = 1.0
    if is_ipo:
        position_scale = IPO_POSITION_SCALE
    elif atr_pct >= HIGH_VOL_ATR_PCT:
        position_scale = HIGH_VOL_POSITION_SCALE
    return {
        "price": price,
        "avg_volume": int(avg_shares),
        "avg_dollar_volume": round(avg_dollar_vol, 0),
        "momentum": momentum,
        "momentum_30d": momentum if mom_lookback >= 30 else None,
        "atr_pct": atr_pct,
        "trend": trend,
        "trading_days": trading_days,
        "is_ipo": is_ipo,
        "exchange": asset_meta.get("exchange", ""),
        "easy_to_borrow": asset_meta.get("easy_to_borrow"),
        "sector": sector_for_symbol(symbol),
        "position_scale": position_scale,
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
    dollar_vol = np.array([r["avg_dollar_volume"] for r in rows], dtype=float)
    momentum = np.array([r["momentum"] for r in rows], dtype=float)
    atr_pct = np.array([r["atr_pct"] for r in rows], dtype=float)
    trend = np.array([r["trend"] for r in rows], dtype=float)

    liq_rank = _percentile_rank(dollar_vol)
    mom_rank = _percentile_rank(momentum)
    vol_rank = 1.0 - _percentile_rank(atr_pct)
    trend_rank = _percentile_rank(trend)

    if strict_mode_active():
        keep = mom_rank >= STRICT_MIN_MOMENTUM_RANK
        if not keep.any():
            keep = np.ones(len(rows), dtype=bool)
        rows = [r for r, ok in zip(rows, keep) if ok]
        if not rows:
            return []
        dollar_vol = np.array([r["avg_dollar_volume"] for r in rows], dtype=float)
        momentum = np.array([r["momentum"] for r in rows], dtype=float)
        atr_pct = np.array([r["atr_pct"] for r in rows], dtype=float)
        trend = np.array([r["trend"] for r in rows], dtype=float)
        liq_rank = _percentile_rank(dollar_vol)
        mom_rank = _percentile_rank(momentum)
        vol_rank = 1.0 - _percentile_rank(atr_pct)
        trend_rank = _percentile_rank(trend)

    scored = []
    for i, row in enumerate(rows):
        composite = (
            WEIGHT_LIQUIDITY * liq_rank[i]
            + WEIGHT_MOMENTUM * mom_rank[i]
            + WEIGHT_VOLATILITY * vol_rank[i]
            + WEIGHT_TREND * trend_rank[i]
        )
        scored.append(
            {
                "ticker": row["ticker"],
                "score": round(float(composite), 6),
                "momentum": round(float(row["momentum"]), 6),
                "momentum_30d": round(float(row["momentum"]), 6)
                if effective_momentum_lookback() >= 30
                else None,
                "momentum_rank": round(float(mom_rank[i]), 6),
                "momentum_30d_rank": round(float(mom_rank[i]), 6)
                if effective_momentum_lookback() >= 30
                else None,
                "atr_pct": round(float(row["atr_pct"]), 6),
                "trend": round(float(row["trend"]), 6),
                "price": round(float(row["price"]), 4),
                "avg_volume": int(row["avg_volume"]),
                "avg_dollar_volume": int(row["avg_dollar_volume"]),
                "trading_days": int(row["trading_days"]),
                "is_ipo": bool(row["is_ipo"]),
                "exchange": row.get("exchange", ""),
                "sector": row.get("sector", "Other"),
                "easy_to_borrow": row.get("easy_to_borrow"),
                "position_scale": round(float(row["position_scale"]), 2),
            }
        )
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored


def _select_universe(scored: list[dict]) -> list[dict]:
    max_size = effective_max_universe_size()
    max_ipo = effective_max_ipo_slots()
    if strict_mode_active():
        established = [r for r in scored if not r["is_ipo"]]
        ipos = [r for r in scored if r["is_ipo"]]
        ipos.sort(key=lambda r: (r["avg_dollar_volume"], r["score"]), reverse=True)
        max_established = max(0, max_size - min(len(ipos), max_ipo))
        pool = established[:max_established] + ipos[:max_ipo]
        selected = apply_sector_balance(
            pool,
            max_size=max_size,
            max_per_sector=STRICT_MAX_PER_SECTOR,
        )
        if len(selected) < STRICT_MIN_UNIVERSE_SIZE:
            seen = {r["ticker"] for r in selected}
            for row in scored:
                if row["ticker"] in seen:
                    continue
                selected.append(row)
                seen.add(row["ticker"])
                if len(selected) >= STRICT_MIN_UNIVERSE_SIZE:
                    break
        return selected[:max_size]

    established = [r for r in scored if not r["is_ipo"]]
    ipos = [r for r in scored if r["is_ipo"]]
    ipos.sort(key=lambda r: (r["avg_dollar_volume"], r["score"]), reverse=True)
    max_established = max(0, max_size - min(len(ipos), max_ipo))
    selected = established[:max_established] + ipos[:max_ipo]
    return selected[:max_size]


def run_screener(*, asset_map: dict[str, dict] | None = None) -> dict:
    _load_env()
    asset_map = asset_map or fetch_alpaca_assets()
    symbols = sorted(asset_map.keys())
    print(f"Alpaca universe: {len(symbols)} active tradable symbols (NYSE+NASDAQ+ARCA)")

    candidates: list[dict] = []
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
            if frame is None:
                continue
            metrics = _metrics(frame, {**asset_map.get(sym, {}), "symbol": sym}, symbol=sym)
            if metrics is None:
                continue
            candidates.append({"ticker": sym, **metrics})

    print(
        f"Passed filters (price>${MIN_PRICE}, ${effective_min_dollar_volume()/1e6:.0f}M avg daily $vol"
        f"{', strict ETB' if strict_mode_active() else ''}): "
        f"{len(candidates)}"
    )
    score_table = score_candidates(candidates)
    top = _select_universe(score_table)
    ipo_count = sum(1 for row in top if row.get("is_ipo"))
    payload = {
        "tickers": [row["ticker"] for row in top],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "score_table": top,
        "ipo_count": ipo_count,
        "filters": {
            "min_price": MIN_PRICE,
            "min_avg_dollar_volume": effective_min_dollar_volume(),
            "min_share_volume": effective_min_share_volume(),
            "momentum_lookback": effective_momentum_lookback(),
            "max_atr_pct": MAX_ATR_PCT,
            "ipo_max_trading_days": IPO_MAX_TRADING_DAYS,
            "max_tickers": effective_max_universe_size(),
            "strict_mode": strict_mode_active(),
            "max_per_sector": STRICT_MAX_PER_SECTOR if strict_mode_active() else None,
            "min_momentum_rank": STRICT_MIN_MOMENTUM_RANK if strict_mode_active() else None,
        },
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {len(top)} tickers ({ipo_count} IPO) to {OUTPUT_PATH}")
    print(f"\nTop {PRINT_TOP} by composite score:")
    print(
        f"{'Rank':<5} {'Ticker':<8} {'Exch':<7} {'Score':>7} {'$VolM':>7} "
        f"{'IPO':>4} {'Scale':>6}"
    )
    for i, row in enumerate(top[:PRINT_TOP], 1):
        ipo = "Y" if row.get("is_ipo") else ""
        print(
            f"{i:<5} {row['ticker']:<8} {row.get('exchange',''):<7} "
            f"{row['score']:>7.4f} {row['avg_dollar_volume']/1e6:>6.1f}M "
            f"{ipo:>4} {row.get('position_scale', 1):>6.2f}"
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
