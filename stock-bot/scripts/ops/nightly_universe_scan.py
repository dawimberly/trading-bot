"""
Nightly Universe Scanner

Runs after market close (scheduled ~8pm CT) to screen the FULL tradable
universe -- not just the fixed sleeve list -- and produce a ranked watchlist
for next-day research / eventual monitor use.

Does NOT touch live/paper execution. Only writes watchlist JSON under
data/watchlists/. Pointing the bot at current_watchlist.json is a separate,
deliberate step -- review a few nights first.

Pipeline:
  1. Alpaca list_assets (NYSE + NASDAQ US equities)
  2. Bulk daily bars (batched StockBarsRequest)
  3. Liquidity / price filters
  4. Score: liquidity + ATR% + range cleanliness (Sneaky Pivot fit)
  5. Top-N dated JSON + current pointer + night-to-night diff

Usage (from stock-bot/):
  python scripts/ops/nightly_universe_scan.py
  python scripts/ops/nightly_universe_scan.py --top-n 150
  python scripts/ops/nightly_universe_scan.py --max-symbols 400   # smoke
  python scripts/ops/nightly_universe_scan.py --no-current        # dated only
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import find_dotenv, load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")
load_dotenv(find_dotenv(), override=False)

import config  # noqa: E402

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


@dataclass
class ScanConfig:
    exchanges: tuple[str, ...] = ("NYSE", "NASDAQ")
    lookback_days: int = 30
    min_price: float = 5.0
    max_price: float = 500.0
    min_avg_dollar_volume: float = 5_000_000.0
    top_n: int = 150
    watchlist_dir: Path = field(
        default_factory=lambda: ROOT / "data" / "watchlists"
    )
    batch_size: int = 200
    batch_sleep_sec: float = 0.35
    weight_liquidity: float = 0.4
    weight_volatility: float = 0.3
    weight_range_cleanliness: float = 0.3
    write_current: bool = True
    max_symbols: int = 0  # 0 = no cap (smoke tests use >0)


DEFAULT_CONFIG = ScanConfig()


# --------------------------------------------------------------------------
# Step 1: universe pull (Alpaca TradingClient.get_all_assets)
# --------------------------------------------------------------------------


def _exchange_str(asset: Any) -> str:
    exchange = getattr(asset, "exchange", None)
    if exchange is None:
        return ""
    return str(exchange.value if hasattr(exchange, "value") else exchange).upper()


def _is_clean_common(symbol: str) -> bool:
    """Common-stock-ish tickers: AAPL, BRK.B; skip warrants/units/OTC junk."""
    t = (symbol or "").strip().upper()
    if not t:
        return False
    if "-" in t or "/" in t:
        return False
    if t.count(".") > 1:
        return False
    base = t.split(".")[0]
    if not base.isalpha() or not (1 <= len(base) <= 5):
        return False
    if len(base) >= 4 and base.endswith(("W", "R", "U", "Q")):
        return False
    return True


def get_tradable_universe(scan_config: ScanConfig) -> list[str]:
    """Active tradable US equities on configured exchanges (Alpaca asset list)."""
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest

    try:
        api_key, secret_key = config.get_paper_alpaca_credentials()
    except Exception:
        api_key, secret_key = config.get_alpaca_credentials(paper=True)

    client = TradingClient(api_key, secret_key, paper=True)
    request = GetAssetsRequest(
        status=AssetStatus.ACTIVE,
        asset_class=AssetClass.US_EQUITY,
    )
    assets = client.get_all_assets(request)

    allowed = {e.upper() for e in scan_config.exchanges}
    symbols: list[str] = []
    for asset in assets:
        if not getattr(asset, "tradable", False):
            continue
        exch = _exchange_str(asset)
        if exch not in allowed:
            continue
        symbol = str(getattr(asset, "symbol", "") or "").strip().upper()
        if not _is_clean_common(symbol):
            continue
        symbols.append(symbol)

    tickers = sorted(set(symbols))
    if scan_config.max_symbols and scan_config.max_symbols > 0:
        # Deterministic stride sample (not alphabetical head) for smoke tests.
        n = scan_config.max_symbols
        if len(tickers) > n:
            step = max(1, len(tickers) // n)
            tickers = tickers[::step][:n]
    return tickers


# --------------------------------------------------------------------------
# Step 2: bulk daily bars (batched StockBarsRequest)
# --------------------------------------------------------------------------


def _normalize_daily_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    out = df.copy()
    if isinstance(out.index, pd.MultiIndex):
        out = out.copy()
        # Drop symbol level if still present
        names = list(out.index.names or [])
        if "symbol" in names:
            out = out.droplevel("symbol")
    out = out.rename(columns={c: str(c).lower() for c in out.columns})
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in out.columns]
    out = out[keep].copy()
    for c in keep:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out.index = pd.to_datetime(out.index)
    if getattr(out.index, "tz", None) is not None:
        out.index = out.index.tz_convert(None)
    out = out.dropna(subset=["close"]).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    if "volume" not in out.columns:
        out["volume"] = 0.0
    return out


def get_recent_daily_bars(
    symbols: list[str], scan_config: ScanConfig
) -> dict[str, pd.DataFrame]:
    """Bulk-pull recent daily OHLCV; batch ~200 symbols per Alpaca call."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    try:
        api_key, secret_key = config.get_paper_alpaca_credentials()
    except Exception:
        api_key, secret_key = config.get_alpaca_credentials(paper=True)

    client = StockHistoricalDataClient(api_key, secret_key)
    # Free/paper SIP often rejects the most recent sessions — lag the end date.
    sip_lag_days = int(getattr(config, "ALPACA_SIP_LAG_DAYS", 5) or 5)
    end = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=sip_lag_days)
    # Calendar buffer for weekends/holidays so we still get ~lookback sessions.
    start = end - dt.timedelta(days=scan_config.lookback_days * 2 + 10)

    out: dict[str, pd.DataFrame] = {}
    batch_size = max(1, int(scan_config.batch_size))
    total = len(symbols)

    for i in range(0, total, batch_size):
        batch = symbols[i : i + batch_size]
        n = i // batch_size + 1
        n_batches = (total + batch_size - 1) // batch_size
        print(f"  bars batch {n}/{n_batches} ({len(batch)} symbols)...")
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
            )
            bars = client.get_stock_bars(req)
            raw = getattr(bars, "df", None)
        except Exception as exc:
            print(f"  batch error: {exc}")
            time.sleep(max(scan_config.batch_sleep_sec, 1.0))
            continue

        if raw is None or raw.empty:
            time.sleep(scan_config.batch_sleep_sec)
            continue

        if isinstance(raw.index, pd.MultiIndex):
            level_names = [str(n).lower() if n else "" for n in (raw.index.names or [])]
            sym_level = 0
            if "symbol" in level_names:
                sym_level = level_names.index("symbol")
            for sym in raw.index.get_level_values(sym_level).unique():
                try:
                    frame = _normalize_daily_frame(raw.xs(sym, level=sym_level))
                    if not frame.empty:
                        out[str(sym).upper()] = frame
                except Exception:
                    continue
        elif len(batch) == 1:
            frame = _normalize_daily_frame(raw)
            if not frame.empty:
                out[batch[0]] = frame

        time.sleep(scan_config.batch_sleep_sec)

    return out


# --------------------------------------------------------------------------
# Step 3: liquidity / sanity filter
# --------------------------------------------------------------------------


def passes_basic_filters(bars: pd.DataFrame, scan_config: ScanConfig) -> bool:
    if bars is None or bars.empty or len(bars) < 15:
        return False

    last_close = float(bars["close"].iloc[-1])
    if not (scan_config.min_price <= last_close <= scan_config.max_price):
        return False

    avg_dollar_vol = float((bars["close"] * bars["volume"]).tail(20).mean())
    if avg_dollar_vol < scan_config.min_avg_dollar_volume:
        return False

    return True


# --------------------------------------------------------------------------
# Step 4: scoring
# --------------------------------------------------------------------------


def compute_atr_pct(bars: pd.DataFrame, window: int = 14) -> float:
    high, low, close = bars["high"], bars["low"], bars["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(window).mean().iloc[-1]
    if pd.isna(atr) or close.iloc[-1] == 0:
        return 0.0
    return float(atr / close.iloc[-1] * 100)


def compute_range_cleanliness(bars: pd.DataFrame) -> float:
    """
    Fraction of days whose range overlaps the prior day's range
    (vs gapping entirely through). Higher = better for Sneaky Pivot / range plays.
    """
    if len(bars) < 10:
        return 0.0

    overlaps = 0
    total = 0
    for i in range(1, len(bars)):
        prior_high = bars["high"].iloc[i - 1]
        prior_low = bars["low"].iloc[i - 1]
        cur_high = bars["high"].iloc[i]
        cur_low = bars["low"].iloc[i]
        overlap = not (cur_low > prior_high or cur_high < prior_low)
        overlaps += int(overlap)
        total += 1

    return overlaps / total if total > 0 else 0.0


def zscore(series: pd.Series) -> pd.Series:
    std = series.std()
    if std == 0 or pd.isna(std):
        return pd.Series(0.0, index=series.index)
    return (series - series.mean()) / std


@dataclass
class ScoredSymbol:
    symbol: str
    last_close: float
    avg_dollar_volume: float
    atr_pct: float
    range_cleanliness: float
    composite_score: float


def score_universe(
    bars_by_symbol: dict[str, pd.DataFrame], scan_config: ScanConfig
) -> list[ScoredSymbol]:
    rows = []
    for sym, bars in bars_by_symbol.items():
        if not passes_basic_filters(bars, scan_config):
            continue
        avg_dollar_vol = float((bars["close"] * bars["volume"]).tail(20).mean())
        atr_pct = compute_atr_pct(bars)
        cleanliness = compute_range_cleanliness(bars)
        rows.append(
            {
                "symbol": sym,
                "last_close": float(bars["close"].iloc[-1]),
                "avg_dollar_volume": avg_dollar_vol,
                "atr_pct": atr_pct,
                "range_cleanliness": cleanliness,
            }
        )

    if not rows:
        return []

    df = pd.DataFrame(rows)
    df["z_liquidity"] = zscore(df["avg_dollar_volume"])
    df["z_volatility"] = zscore(df["atr_pct"])
    df["z_cleanliness"] = zscore(df["range_cleanliness"])
    df["composite_score"] = (
        scan_config.weight_liquidity * df["z_liquidity"]
        + scan_config.weight_volatility * df["z_volatility"]
        + scan_config.weight_range_cleanliness * df["z_cleanliness"]
    )
    df = df.sort_values("composite_score", ascending=False)

    return [
        ScoredSymbol(
            symbol=row["symbol"],
            last_close=row["last_close"],
            avg_dollar_volume=row["avg_dollar_volume"],
            atr_pct=row["atr_pct"],
            range_cleanliness=row["range_cleanliness"],
            composite_score=row["composite_score"],
        )
        for _, row in df.iterrows()
    ]


# --------------------------------------------------------------------------
# Step 5: write watchlist + night-to-night diff
# --------------------------------------------------------------------------


def _load_prior_symbols(watchlist_dir: Path, scan_date: dt.date) -> tuple[list[str], str]:
    """Prefer yesterday's dated file; else current_watchlist.json."""
    yesterday = scan_date - dt.timedelta(days=1)
    dated = watchlist_dir / f"watchlist_{yesterday.isoformat()}.json"
    current = watchlist_dir / "current_watchlist.json"
    for path, label in ((dated, dated.name), (current, current.name)):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            syms = [str(s).upper() for s in (payload.get("symbols") or [])]
            if syms:
                return syms, label
        except Exception:
            continue
    return [], ""


def write_diff_report(
    new_symbols: list[str],
    prior_symbols: list[str],
    prior_label: str,
    watchlist_dir: Path,
    scan_date: dt.date,
) -> Path | None:
    if not prior_symbols:
        return None

    new_set = set(new_symbols)
    old_set = set(prior_symbols)
    added = sorted(new_set - old_set)
    dropped = sorted(old_set - new_set)
    kept = sorted(new_set & old_set)

    lines = [
        f"# Watchlist diff — {scan_date.isoformat()}",
        "",
        f"Compared to: `{prior_label}`",
        "",
        f"| Metric | Count |",
        f"|--------|------:|",
        f"| Kept | {len(kept)} |",
        f"| Added | {len(added)} |",
        f"| Dropped | {len(dropped)} |",
        f"| New list size | {len(new_symbols)} |",
        f"| Prior list size | {len(prior_symbols)} |",
        "",
    ]
    if added:
        lines.append("## Added")
        lines.append("")
        lines.append(", ".join(added))
        lines.append("")
    if dropped:
        lines.append("## Dropped")
        lines.append("")
        lines.append(", ".join(dropped))
        lines.append("")

    path = watchlist_dir / f"watchlist_diff_{scan_date.isoformat()}.md"
    path.write_text("\n".join(lines), encoding="utf-8")

    # Machine-readable twin
    (watchlist_dir / f"watchlist_diff_{scan_date.isoformat()}.json").write_text(
        json.dumps(
            {
                "scan_date": scan_date.isoformat(),
                "compared_to": prior_label,
                "kept": kept,
                "added": added,
                "dropped": dropped,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def write_watchlist(
    scored: list[ScoredSymbol],
    scan_config: ScanConfig,
    scan_date: dt.date,
) -> Path:
    scan_config.watchlist_dir.mkdir(parents=True, exist_ok=True)
    top = scored[: scan_config.top_n]
    symbols = [s.symbol for s in top]

    prior_syms, prior_label = _load_prior_symbols(scan_config.watchlist_dir, scan_date)
    diff_path = write_diff_report(
        symbols, prior_syms, prior_label, scan_config.watchlist_dir, scan_date
    )
    if diff_path:
        print(f"  Diff report: {diff_path.name}")

    payload = {
        "scan_date": scan_date.isoformat(),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "freeze_note": (
            "Ranked scan list. Merged into paper screener when "
            "MERGE_NIGHTLY_WATCHLIST=true; MAX_ACTIVE_TICKERS still caps holdings."
        ),
        "config": {
            "exchanges": list(scan_config.exchanges),
            "lookback_days": scan_config.lookback_days,
            "top_n": scan_config.top_n,
            "min_price": scan_config.min_price,
            "max_price": scan_config.max_price,
            "min_avg_dollar_volume": scan_config.min_avg_dollar_volume,
            "weights": {
                "liquidity": scan_config.weight_liquidity,
                "volatility": scan_config.weight_volatility,
                "range_cleanliness": scan_config.weight_range_cleanliness,
            },
        },
        "symbols": symbols,
        "detail": [asdict(s) for s in top],
        "diff_vs_prior": {
            "prior_source": prior_label or None,
            "added": sorted(set(symbols) - set(prior_syms)) if prior_syms else [],
            "dropped": sorted(set(prior_syms) - set(symbols)) if prior_syms else [],
        },
    }

    dated_path = scan_config.watchlist_dir / f"watchlist_{scan_date.isoformat()}.json"
    dated_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    if scan_config.write_current:
        current_path = scan_config.watchlist_dir / "current_watchlist.json"
        current_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return dated_path


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


def run_nightly_scan(scan_config: ScanConfig = DEFAULT_CONFIG) -> Path:
    scan_date = dt.date.today()
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"[{stamp}] Pulling tradable universe...")
    universe = get_tradable_universe(scan_config)
    print(f"  {len(universe)} symbols in universe")

    print(f"[{stamp}] Pulling ~{scan_config.lookback_days}d daily bars (batched)...")
    bars_by_symbol = get_recent_daily_bars(universe, scan_config)
    print(f"  bars returned for {len(bars_by_symbol)} symbols")

    print(f"[{stamp}] Scoring universe...")
    scored = score_universe(bars_by_symbol, scan_config)
    print(f"  {len(scored)} symbols passed filters")

    path = write_watchlist(scored, scan_config, scan_date)
    print(f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Wrote watchlist: {path}")
    print(f"  Top 10: {[s.symbol for s in scored[:10]]}")
    print(
        "Watchlist written — paper merges it when MERGE_NIGHTLY_WATCHLIST=true "
        "(MAX_ACTIVE_TICKERS still caps how many names it holds)."
    )
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Nightly Alpaca universe -> ranked watchlist")
    ap.add_argument("--top-n", type=int, default=DEFAULT_CONFIG.top_n)
    ap.add_argument("--lookback-days", type=int, default=DEFAULT_CONFIG.lookback_days)
    ap.add_argument("--batch-size", type=int, default=DEFAULT_CONFIG.batch_size)
    ap.add_argument(
        "--max-symbols",
        type=int,
        default=0,
        help="Cap universe size after asset list (smoke tests)",
    )
    ap.add_argument(
        "--no-current",
        action="store_true",
        help="Write dated watchlist only (skip current_watchlist.json)",
    )
    ap.add_argument("--min-price", type=float, default=DEFAULT_CONFIG.min_price)
    ap.add_argument("--max-price", type=float, default=DEFAULT_CONFIG.max_price)
    ap.add_argument(
        "--min-adv",
        type=float,
        default=DEFAULT_CONFIG.min_avg_dollar_volume,
        help="Min average dollar volume",
    )
    args = ap.parse_args(argv)

    cfg = ScanConfig(
        lookback_days=args.lookback_days,
        top_n=args.top_n,
        batch_size=args.batch_size,
        max_symbols=args.max_symbols,
        write_current=not args.no_current,
        min_price=args.min_price,
        max_price=args.max_price,
        min_avg_dollar_volume=args.min_adv,
    )
    run_nightly_scan(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
