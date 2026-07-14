"""Dynamic NYSE/NASDAQ universe screener.

Ranks liquid US equities by momentum and MA50 trend after quality gates
(price, dollar volume, ATR band; optional market-cap gate). Writes top 75 to
data/screener_universe.json, then prefetches those symbols into SQLite so the
live bot's dynamic universe can actually see them.

Market-cap modes (SCREENER_MARKET_CAP_MODE):
  off   — skip mcap gate (default; avoids per-ticker yf.Ticker.info rate limits)
  cache — use data/market_cap_cache.json only
  fetch — cache + live .info for misses (slow)

Run from stock-bot root:
  python scripts/analysis/universe_screener.py
  python scripts/analysis/universe_screener.py --force
  python scripts/analysis/universe_screener.py --compare
  python scripts/analysis/universe_screener.py --force --compare
  python -c "from modules.dynamic_universe import prefetch_screener_price_data; print(prefetch_screener_price_data())"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

OUTPUT_PATH = ROOT / "data" / "screener_universe.json"
ALLOWED_EXCHANGES = frozenset({"NYSE", "NASDAQ"})
EXCLUDE_TICKERS = frozenset(
    {
        "SPY",
        "QQQ",
        "IWM",
        "VTI",
        "GLD",
        "SLV",
        "CPER",
        "URA",
        "PPLT",
        "DBB",
        "GDX",
        "BTC-USD",
        "ETH-USD",
        "SOL-USD",
    }
)
MIN_PRICE = 10.0
MIN_AVG_DOLLAR_VOLUME = 5_000_000.0
MIN_MARKET_CAP = 5_000_000_000.0
MIN_REVENUE = 100_000_000.0
MIN_ATR_PCT = 0.01
MAX_ATR_PCT = 0.08
MIN_HISTORY_DAYS = 65  # enough bars for 60-day momentum lookback
LOOKBACK = 20
LOOKBACK_60 = 60
MA_WINDOW = 50
TOP_N = 75
PRINT_TOP = 20
SECTOR_CAP = 15
BATCH_SIZE = 20
BATCH_SLEEP_SEC = 3
RATE_LIMIT_RETRY_SLEEP_SEC = 45
YFINANCE_PERIOD = "180d"  # need ~60+ trading days beyond quality gates
FRESH_DAYS = 7
WEIGHT_MOMENTUM = 0.50
WEIGHT_TREND = 0.50


def _output_path() -> Path:
    raw = getattr(config, "SCREENER_UNIVERSE_PATH", None) or "data/screener_universe.json"
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def _is_fresh(path: Path, *, max_age_days: int = FRESH_DAYS) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        generated = str(payload.get("generated_at") or "").strip()
        if not generated:
            return False
        ts = datetime.fromisoformat(generated.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - ts.astimezone(timezone.utc)
        return age.total_seconds() < max_age_days * 86400
    except Exception:
        return False


def _excluded(symbol: str) -> bool:
    sym = (symbol or "").strip().upper()
    if not sym:
        return True
    if "-USD" in sym:
        return True
    return sym in EXCLUDE_TICKERS


def _is_clean_ticker(t: str) -> bool:
    if '.' in t: return False          # preferred shares, units
    if len(t) > 5: return False        # warrants, special classes
    if t.endswith(('W','R','U','Q')): return False  # warrants, rights, units
    return True


def _is_rate_limit_error(exc: BaseException) -> bool:
    try:
        from yfinance.exceptions import YFRateLimitError

        if isinstance(exc, YFRateLimitError):
            return True
    except Exception:
        pass
    name = type(exc).__name__
    if name == "YFRateLimitError":
        return True
    msg = str(exc).lower()
    return "rate limit" in msg or "too many requests" in msg


def _append_skipped_tickers(symbols: list[str]) -> None:
    """Append rate-limit-skipped tickers to data/screener_skipped.txt."""
    path = ROOT / "data" / "screener_skipped.txt"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with path.open("a", encoding="utf-8") as fh:
            for sym in symbols:
                fh.write(f"{stamp}\t{sym}\n")
    except Exception as exc:
        print(f"Warning: could not write screener_skipped.txt: {exc}")


def fetch_alpaca_equity_symbols() -> list[str]:
    """Active tradable US equities on NYSE / NASDAQ only."""
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest

    api_key, secret_key = config.get_alpaca_credentials()
    client = TradingClient(api_key, secret_key, paper=bool(config.PAPER_TRADING))
    request = GetAssetsRequest(
        status=AssetStatus.ACTIVE,
        asset_class=AssetClass.US_EQUITY,
    )
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
        if _excluded(symbol):
            continue
        symbols.append(symbol)
    tickers = sorted(set(symbols))
    tickers = [t for t in tickers if _is_clean_ticker(t)]
    return tickers


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
    if not needed.issubset(set(df.columns)):
        return None
    out = df[list(needed)].copy()
    out.index = pd.to_datetime(out.index)
    out = out.sort_index().dropna(how="all")
    for col in needed:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["Close", "Volume", "High", "Low"])
    return out if len(out) >= MIN_HISTORY_DAYS else None


def _fetch_yfinance_batch(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Download OHLCV for a batch. Re-raises yfinance rate-limit errors."""
    if not symbols:
        return {}
    out: dict[str, pd.DataFrame] = {}
    if len(symbols) == 1:
        sym = symbols[0]
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                raw = yf.download(
                    sym, period=YFINANCE_PERIOD, progress=False, auto_adjust=True
                )
            frame = _normalize_ohlcv(raw)
            if frame is not None:
                out[sym] = frame
        except Exception as exc:
            if _is_rate_limit_error(exc):
                raise
        return out

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            raw = yf.download(
                symbols,
                period=YFINANCE_PERIOD,
                group_by="ticker",
                progress=False,
                auto_adjust=True,
                threads=True,
            )
    except Exception as exc:
        if _is_rate_limit_error(exc):
            raise
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


def _fetch_yfinance_batch_with_retry(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Fetch a batch; on rate limit wait 45s and retry once, then skip."""
    try:
        return _fetch_yfinance_batch(symbols)
    except Exception as exc:
        if not _is_rate_limit_error(exc):
            raise
        print("Rate limited — waiting 45s...")
        time.sleep(RATE_LIMIT_RETRY_SLEEP_SEC)
        try:
            return _fetch_yfinance_batch(symbols)
        except Exception as retry_exc:
            if _is_rate_limit_error(retry_exc):
                print(f"Rate limit persists after retry; skipping batch ({len(symbols)} symbols)")
                _append_skipped_tickers(symbols)
                return {}
            raise


# Fundamentals gate (per-ticker yf.Ticker.info is the #1 rate-limit amplifier):
#   off   — skip mcap/revenue/sector gates (fast; not used with $5B / revenue filters)
#   cache — use data/market_cap_cache.json only; cache miss rejects (needs revenue/sector)
#   fetch — cache first, then yf.Ticker.info for misses (default; required for new filters)
MARKET_CAP_MODE = (os.getenv("SCREENER_MARKET_CAP_MODE", "fetch") or "fetch").strip().lower()
MARKET_CAP_CACHE_PATH = ROOT / "data" / "market_cap_cache.json"
MARKET_CAP_CACHE_DAYS = float(os.getenv("SCREENER_MARKET_CAP_CACHE_DAYS", "30"))


def _load_market_cap_cache() -> dict[str, dict]:
    if not MARKET_CAP_CACHE_PATH.is_file():
        return {}
    try:
        raw = json.loads(MARKET_CAP_CACHE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save_market_cap_cache(cache: dict[str, dict]) -> None:
    try:
        MARKET_CAP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        MARKET_CAP_CACHE_PATH.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def _cache_fundamentals_fresh(entry: dict) -> dict | None:
    """Return cached market_cap / total_revenue / sector if entry is fresh and complete."""
    try:
        if "total_revenue" not in entry or "sector" not in entry:
            return None  # old cache shape — force re-fetch for revenue/sector
        cap = float(entry.get("market_cap") or 0)
        ts = str(entry.get("ts") or "")
        if cap <= 0 or not ts:
            return None
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - when.astimezone(timezone.utc)).total_seconds() / 86400
        if age_days > MARKET_CAP_CACHE_DAYS:
            return None
        rev_raw = entry.get("total_revenue")
        rev = float(rev_raw) if rev_raw is not None else None
        sector = entry.get("sector")
        return {
            "market_cap": cap,
            "total_revenue": rev if rev is not None and np.isfinite(rev) else None,
            "sector": str(sector) if sector else None,
        }
    except Exception:
        return None


def _fetch_ticker_fundamentals_live(symbol: str) -> dict | None:
    """
    One yfinance .info lookup for marketCap, totalRevenue, and sector.
    Retries once on rate limit; returns None on failure / persistent rate limit.
    """
    def _once() -> dict | None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            info = yf.Ticker(symbol).info or {}
        cap = info.get("marketCap")
        if cap is None:
            return None
        cap_f = float(cap)
        if not np.isfinite(cap_f) or cap_f <= 0:
            return None
        rev_raw = info.get("totalRevenue")
        rev = float(rev_raw) if rev_raw is not None else None
        if rev is not None and (not np.isfinite(rev) or rev < 0):
            rev = None
        sector = info.get("sector")
        return {
            "market_cap": cap_f,
            "total_revenue": rev,
            "sector": str(sector) if sector else None,
        }

    try:
        return _once()
    except Exception as exc:
        if not _is_rate_limit_error(exc):
            return None
        print("Rate limited — waiting 45s...")
        time.sleep(RATE_LIMIT_RETRY_SLEEP_SEC)
        try:
            return _once()
        except Exception as retry_exc:
            if _is_rate_limit_error(retry_exc):
                print(f"Rate limit persists after retry; skipping fundamentals for {symbol}")
                _append_skipped_tickers([symbol])
                return None
            return None


def _resolve_fundamentals(
    symbol: str,
    cache: dict[str, dict],
    *,
    mode: str,
) -> tuple[dict | None, str]:
    """
    Returns (fundamentals_or_None, disposition).
    disposition: off_skip | cache_hit | cache_miss_pass | fetched | fetch_fail_reject
    """
    if mode in ("off", "skip", "false", "0", "no"):
        return None, "off_skip"
    cached = _cache_fundamentals_fresh(cache.get(symbol.upper()) or {})
    if cached is not None:
        return cached, "cache_hit"
    if mode == "cache":
        return None, "cache_miss_pass"
    # fetch mode — single .info call for marketCap + totalRevenue + sector
    fund = _fetch_ticker_fundamentals_live(symbol)
    if fund is None:
        return None, "fetch_fail_reject"
    cache[symbol.upper()] = {
        "market_cap": fund["market_cap"],
        "total_revenue": fund.get("total_revenue"),
        "sector": fund.get("sector"),
        "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    return fund, "fetched"


def _atr_pct(frame: pd.DataFrame, window: int = LOOKBACK) -> float | None:
    high = frame["High"]
    low = frame["Low"]
    close = frame["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = float(tr.rolling(window).mean().iloc[-1])
    price = float(close.iloc[-1])
    if not np.isfinite(atr) or price <= 0:
        return None
    return atr / price


def _evaluate_frame(
    frame: pd.DataFrame, counters: dict[str, int]
) -> dict[str, float] | None:
    """Apply quality gates; return momentum/trend scores or None if rejected."""
    if len(frame) < MIN_HISTORY_DAYS:
        counters["history"] += 1
        return None

    close = frame["Close"]
    volume = frame["Volume"]
    price = float(close.iloc[-1])
    if price < MIN_PRICE:
        counters["price"] += 1
        return None

    avg_volume = float(volume.tail(LOOKBACK).mean())
    if not np.isfinite(avg_volume) or avg_volume <= 0:
        counters["dollar_volume"] += 1
        return None
    avg_dollar_vol = avg_volume * price
    if avg_dollar_vol < MIN_AVG_DOLLAR_VOLUME:
        counters["dollar_volume"] += 1
        return None

    volatility = _atr_pct(frame, LOOKBACK)
    if volatility is None:
        counters["atr"] += 1
        return None
    if volatility < MIN_ATR_PCT or volatility > MAX_ATR_PCT:
        counters["atr"] += 1
        return None

    # Smoothed momentum: average of 20-day and 60-day returns
    if len(close) <= LOOKBACK_60:
        counters["history"] += 1
        return None
    price_20d = float(close.iloc[-(LOOKBACK + 1)])
    price_60d = float(close.iloc[-(LOOKBACK_60 + 1)])
    if price_20d <= 0 or price_60d <= 0:
        counters["history"] += 1
        return None
    mom_20 = price / price_20d - 1.0
    mom_60 = price / price_60d - 1.0
    momentum = (mom_20 + mom_60) / 2.0

    # Same logic as modules.pipeline_strategies._momentum_score()
    ma50 = float(close.rolling(window=min(MA_WINDOW, len(close))).mean().iloc[-1])
    if ma50 <= 0:
        counters["history"] += 1
        return None
    trend = price / ma50 - 1.0

    composite = WEIGHT_MOMENTUM * momentum + WEIGHT_TREND * trend
    return {
        "momentum": round(float(momentum), 6),
        "volatility": round(float(volatility), 6),
        "trend": round(float(trend), 6),
        "composite": round(float(composite), 6),
    }


def _apply_sector_diversity(
    scored: list[dict], *, top_n: int = TOP_N, sector_cap: int = SECTOR_CAP
) -> list[dict]:
    """Keep top_n by composite with at most sector_cap names per GICS sector."""
    selected: list[dict] = []
    sector_counts: dict[str, int] = {}
    for row in scored:
        if len(selected) >= top_n:
            break
        sector = (row.get("sector") or "Unknown").strip() or "Unknown"
        if sector_counts.get(sector, 0) >= sector_cap:
            continue
        selected.append(row)
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
    return selected


def _print_sector_distribution(top: list[dict]) -> None:
    counts: dict[str, int] = {}
    for row in top:
        sector = (row.get("sector") or "Unknown").strip() or "Unknown"
        counts[sector] = counts.get(sector, 0) + 1
    print("\n--- Sector distribution (top 75) ---")
    for sector, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {sector:<30} {n}")
    print(f"  {'TOTAL':<30} {len(top)}")


def screen_universe() -> dict:
    symbols = fetch_alpaca_equity_symbols()
    scanned = len(symbols)
    print(f"Alpaca NYSE/NASDAQ clean tickers (post pre-filter): {scanned}")
    print(f"Market-cap mode: {MARKET_CAP_MODE} (off|cache|fetch)")

    counters = {
        "no_price_data": 0,
        "history": 0,
        "price": 0,
        "dollar_volume": 0,
        "atr": 0,
        "market_cap": 0,
        "market_cap_skipped": 0,
        "revenue": 0,
        "passed": 0,
    }
    scored: list[dict] = []
    n_batches = (scanned + BATCH_SIZE - 1) // BATCH_SIZE or 1
    mcap_cache = _load_market_cap_cache()
    cache_dirty = False

    processed = 0
    for i in range(0, len(symbols), BATCH_SIZE):
        if i > 0:
            time.sleep(BATCH_SLEEP_SEC)
        batch_num = i // BATCH_SIZE + 1
        batch = symbols[i : i + BATCH_SIZE]
        frames = _fetch_yfinance_batch_with_retry(batch)
        missing = [sym for sym in batch if sym not in frames]
        counters["no_price_data"] += len(missing)

        for sym, frame in frames.items():
            try:
                metrics = _evaluate_frame(frame, counters)
            except Exception:
                counters["history"] += 1
                continue
            if metrics is None:
                continue

            fund, fund_disp = _resolve_fundamentals(sym, mcap_cache, mode=MARKET_CAP_MODE)
            if fund_disp == "fetched":
                cache_dirty = True
            if fund_disp in ("off_skip", "cache_miss_pass"):
                counters["market_cap_skipped"] += 1
                continue
            market_cap = None if fund is None else fund.get("market_cap")
            total_revenue = None if fund is None else fund.get("total_revenue")
            sector = None if fund is None else fund.get("sector")
            if market_cap is None or market_cap < MIN_MARKET_CAP:
                counters["market_cap"] += 1
                continue
            if total_revenue is None or total_revenue < MIN_REVENUE:
                counters["revenue"] += 1
                continue

            counters["passed"] += 1
            scored.append(
                {
                    "ticker": sym,
                    "market_cap": float(market_cap),
                    "sector": sector,
                    **metrics,
                }
            )

        processed += len(batch)
        if batch_num % 5 == 0 or batch_num == n_batches:
            print(
                f"[Batch {batch_num}/{n_batches}] Processed {processed} tickers, "
                f"{counters['passed']} passed filters so far"
            )

    if cache_dirty:
        _save_market_cap_cache(mcap_cache)

    scored.sort(key=lambda row: row["composite"], reverse=True)
    top = _apply_sector_diversity(scored, top_n=TOP_N, sector_cap=SECTOR_CAP)

    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    payload = {
        "tickers": [row["ticker"] for row in top],
        "generated_at": generated_at,
        "score_table": [
            {
                "ticker": row["ticker"],
                "momentum": row["momentum"],
                "volatility": row["volatility"],
                "trend": row["trend"],
                "composite": row["composite"],
            }
            for row in top
        ],
        "filters": {
            "market_cap_mode": MARKET_CAP_MODE,
            "min_price": MIN_PRICE,
            "min_adv_usd": MIN_AVG_DOLLAR_VOLUME,
            "min_market_cap": MIN_MARKET_CAP if MARKET_CAP_MODE == "fetch" else None,
            "min_revenue": MIN_REVENUE if MARKET_CAP_MODE == "fetch" else None,
            "sector_cap": SECTOR_CAP,
        },
    }

    out_path = _output_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print("\n--- Filter eliminations ---")
    print(f"Scanned:                          {scanned}")
    print(f"Eliminated (no/insufficient yfinance history): {counters['no_price_data'] + counters['history']}")
    print(f"  - no price data:                {counters['no_price_data']}")
    print(f"  - history / score fail:         {counters['history']}")
    print(f"Eliminated (price < ${MIN_PRICE:.0f}):           {counters['price']}")
    print(f"Eliminated (ADV$ < ${MIN_AVG_DOLLAR_VOLUME/1e6:.0f}M):      {counters['dollar_volume']}")
    print(
        f"Eliminated (ATR% not in "
        f"{MIN_ATR_PCT:.0%}-{MAX_ATR_PCT:.0%}): {counters['atr']}"
    )
    print(f"Eliminated (mkt cap < $5B / fail): {counters['market_cap']}")
    print(f"Eliminated (revenue < $100M / miss): {counters['revenue']}")
    print(f"Market-cap gate skipped (mode):   {counters['market_cap_skipped']}")
    print(f"Passed all filters:               {counters['passed']}")
    print(f"Top {TOP_N}:                           {len(top)}")
    print(f"Wrote {out_path}")
    _print_sector_distribution(top)
    print(f"\nTop {PRINT_TOP}:")
    print(f"{'Rank':>4}  {'Ticker':<8}  {'Mom':>8}  {'ATR%':>8}  {'Trend':>8}  {'Comp':>8}")
    for idx, row in enumerate(top[:PRINT_TOP], start=1):
        print(
            f"{idx:4d}  {row['ticker']:<8}  "
            f"{row['momentum']:8.4f}  {row['volatility']:8.4f}  "
            f"{row['trend']:8.4f}  {row['composite']:8.4f}"
        )

    # Prefetch so live close matrix can actually see screener names.
    try:
        from modules.dynamic_universe import prefetch_screener_price_data

        pref = prefetch_screener_price_data(payload.get("tickers") or [])
        print(
            f"Prefetch: 5m={pref.get('fetched_5m', 0)} "
            f"daily={pref.get('fetched_daily', 0)} "
            f"skipped_already_static={pref.get('skipped_static', 0)}"
        )
    except Exception as exc:
        print(f"Prefetch warning (universe file still written): {exc}")

    return payload


# Back-compat alias (maybe_refresh_screener_universe historically imported run_screener).
run_screener = screen_universe


def _load_screener_tickers(path: Path | None = None) -> list[str]:
    path = path or _output_path()
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [str(t).strip().upper() for t in (payload.get("tickers") or []) if str(t).strip()]


def compare_to_fixed_universe(screener_tickers: list[str] | None = None) -> None:
    """Compare fixed config.get_nyse_universe() vs dynamic screener tickers."""
    fixed = {str(t).strip().upper() for t in config.get_nyse_universe() if str(t).strip()}
    if screener_tickers is None:
        screener = set(_load_screener_tickers())
    else:
        screener = {str(t).strip().upper() for t in screener_tickers if str(t).strip()}

    both = sorted(fixed & screener)
    screener_only = sorted(screener - fixed)
    fixed_only = sorted(fixed - screener)
    union = fixed | screener

    pct_vs_fixed = (100.0 * len(both) / len(fixed)) if fixed else 0.0
    pct_vs_screener = (100.0 * len(both) / len(screener)) if screener else 0.0
    pct_vs_union = (100.0 * len(both) / len(union)) if union else 0.0

    def _fmt(label: str, tickers: list[str]) -> None:
        joined = ", ".join(tickers) if tickers else "(none)"
        print(f"\n{label} ({len(tickers)}):")
        print(f"  {joined}")

    print("\n=== Fixed vs screener comparison ===")
    print(f"Fixed (get_nyse_universe): {len(fixed)}")
    print(f"Screener (screener_universe.json): {len(screener)}")
    _fmt("In both", both)
    _fmt("Screener only", screener_only)
    _fmt("Fixed only", fixed_only)
    print(
        f"\nOverlap vs fixed:    {pct_vs_fixed:5.1f}%  "
        f"(|both| / |fixed| = {len(both)}/{len(fixed)})"
    )
    print(
        f"Overlap vs screener: {pct_vs_screener:5.1f}%  "
        f"(|both| / |screener| = {len(both)}/{len(screener)})"
    )
    print(
        f"Overlap vs union:    {pct_vs_union:5.1f}%  "
        f"(|both| / |union| = {len(both)}/{len(union)})"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Dynamic NYSE universe screener")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Refresh even if screener_universe.json is less than 7 days old",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="After write (or against existing JSON), compare screener vs fixed get_nyse_universe()",
    )
    args = parser.parse_args()

    out_path = _output_path()
    payload: dict | None = None

    if not args.force and _is_fresh(out_path):
        print("Universe fresh, skipping refresh")
    elif args.compare and not args.force and out_path.is_file():
        # --compare without --force: reuse existing JSON (no yfinance download)
        print(f"Using existing {out_path} for --compare (pass --force to refresh first)")
    else:
        payload = screen_universe()

    if args.compare:
        if payload is None and not out_path.is_file():
            print(f"Error: {out_path} not found; cannot compare")
            return 1
        tickers = None if payload is None else list(payload.get("tickers") or [])
        compare_to_fixed_universe(tickers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
