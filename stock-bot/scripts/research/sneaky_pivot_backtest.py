"""Sneaky Pivot — 15-min research backtest (freeze-safe; NOT wired into run_all).

Levels (once per day from prior daily session):
  range_high/low = prior day H/L
  swing_high/low = next prior day extreme beyond the range

Entry (RTH 15-min bars):
  1) Opening bar (candle 1) must touch range_low (long) or range_high (short)
  2) Within confirm_window bars, find a reversal close (green long / red short)
  3) Enter on later bar touching that confirm candle's high/low (or next open)

Run (from stock-bot/):
  python scripts/research/sneaky_pivot_backtest.py
  python scripts/research/sneaky_pivot_backtest.py --days 90 --refresh
  python scripts/research/sneaky_pivot_backtest.py --confirm-window 1 --tag strict_c2
  python scripts/research/sneaky_pivot_backtest.py --fill next_open --tag next_open
  python scripts/research/sneaky_pivot_backtest.py --run-matrix   # baseline + A/Bs
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import warnings
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from dotenv import find_dotenv, load_dotenv

warnings.filterwarnings("ignore", category=FutureWarning)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

ET = ZoneInfo("America/New_York")
CACHE_DIR = ROOT / "data" / "intraday_cache_15m"
OUT_DIR = ROOT / "scripts" / "research"
DEFAULT_UNIVERSE = ("SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "BRK.B")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
CHUNK_DAYS = 30
CACHE_MAX_AGE = timedelta(days=2)
INITIAL_EQUITY = 100_000.0
RISK_PCT = 0.01
MAX_NOTIONAL_PCT = 0.10


@dataclass
class Levels:
    range_high: float
    range_low: float
    swing_high: float
    swing_low: float


@dataclass
class Signal:
    side: str
    entry_price: float
    stop: float
    target: float
    entry_idx: int
    confirm_idx: int


@dataclass
class Trade:
    date: str
    ticker: str
    side: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    stop: float
    target: float
    shares: float
    pnl: float
    pnl_pct: float
    r_multiple: float
    exit_reason: str
    confirm_idx: int
    entry_idx: int


@dataclass
class RunStats:
    tag: str
    days: int
    confirm_window: int
    fill: str
    stretch: bool
    symbols: list[str]
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_r: float = 0.0
    median_r: float = 0.0
    max_dd_pct: float = 0.0
    final_equity: float = INITIAL_EQUITY
    signals: int = 0
    skipped_no_levels: int = 0
    skipped_no_touch: int = 0
    notes: list[str] = field(default_factory=list)


def _load_env() -> None:
    env_override = os.getenv("PYTHONTRADING_ENV_FILE", "").strip()
    if env_override and os.path.isfile(env_override):
        load_dotenv(env_override, override=True)
    else:
        load_dotenv(find_dotenv())


def _paper_keys() -> tuple[str, str]:
    return config.get_paper_alpaca_credentials()


def _normalize_bars(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy().reset_index()
    rename = {}
    for col in out.columns:
        low = str(col).lower()
        if low in ("timestamp", "time", "datetime", "date"):
            rename[col] = "timestamp"
        elif low == "open":
            rename[col] = "Open"
        elif low == "high":
            rename[col] = "High"
        elif low == "low":
            rename[col] = "Low"
        elif low == "close":
            rename[col] = "Close"
        elif low == "volume":
            rename[col] = "Volume"
    out = out.rename(columns=rename)
    if "timestamp" not in out.columns:
        return pd.DataFrame()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True).dt.tz_convert(ET)
    keep = [c for c in ("timestamp", "Open", "High", "Low", "Close", "Volume") if c in out.columns]
    out = out[keep].copy()
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["Close"]).sort_values("timestamp")
    out = out.drop_duplicates("timestamp", keep="last").reset_index(drop=True)
    if "Volume" not in out.columns:
        out["Volume"] = 0.0
    return out


def _is_rth(ts: pd.Timestamp) -> bool:
    if ts.weekday() >= 5:
        return False
    t = ts.timetz().replace(tzinfo=None) if getattr(ts, "tzinfo", None) else ts.time()
    return RTH_OPEN <= t < RTH_CLOSE


def _filter_rth(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    return df.loc[df["timestamp"].map(_is_rth)].reset_index(drop=True)


def _cache_path(ticker: str) -> Path:
    safe = ticker.upper().replace("/", "-")
    return CACHE_DIR / f"{safe}.pkl"


def _cache_fresh(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    return age < CACHE_MAX_AGE


def _load_cache(ticker: str) -> pd.DataFrame | None:
    path = _cache_path(ticker)
    if not path.is_file():
        return None
    try:
        with open(path, "rb") as f:
            df = pickle.load(f)
        if isinstance(df, pd.DataFrame) and not df.empty:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(ET)
            return df
    except Exception:
        return None
    return None


def _save_cache(ticker: str, df: pd.DataFrame) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_cache_path(ticker), "wb") as f:
        pickle.dump(df, f, protocol=pickle.HIGHEST_PROTOCOL)


def fetch_alpaca_15m(ticker: str, *, days: int) -> pd.DataFrame:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    api_key, secret_key = _paper_keys()
    client = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)
    # Free/paper SIP plans often block the most recent ~5 trading days of minute bars.
    end = datetime.now(timezone.utc) - timedelta(days=5)
    start = end - timedelta(days=days + 10)

    frames: list[pd.DataFrame] = []
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), end)
        try:
            request = StockBarsRequest(
                symbol_or_symbols=ticker,
                timeframe=TimeFrame(15, TimeFrameUnit.Minute),
                start=chunk_start,
                end=chunk_end,
            )
            bars = client.get_stock_bars(request)
            df = getattr(bars, "df", None)
            if df is not None and not df.empty:
                frames.append(_normalize_bars(df))
        except Exception as exc:
            print(
                f"  {ticker}: chunk error {chunk_start.date()}–{chunk_end.date()} — {exc}"
            )
        chunk_start = chunk_end

    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates("timestamp", keep="last").sort_values("timestamp")
    return _filter_rth(out.reset_index(drop=True))


def load_ticker_bars(ticker: str, *, days: int, refresh: bool) -> pd.DataFrame | None:
    path = _cache_path(ticker)
    if not refresh and _cache_fresh(path):
        cached = _load_cache(ticker)
        if cached is not None and not cached.empty:
            return cached
    if not refresh:
        cached = _load_cache(ticker)
        if cached is not None and not cached.empty:
            return cached

    print(f"  fetching 15m {ticker} ({days}d)...")
    df = fetch_alpaca_15m(ticker, days=days)
    if df.empty:
        return None
    _save_cache(ticker, df)
    return df


def bars_to_daily(bars: pd.DataFrame) -> pd.DataFrame:
    """Aggregate RTH 15-min bars to daily OHLC (date index, tz-naive dates)."""
    if bars.empty:
        return pd.DataFrame()
    tmp = bars.copy()
    tmp["session"] = tmp["timestamp"].dt.tz_convert(ET).dt.date
    g = tmp.groupby("session", sort=True)
    daily = pd.DataFrame(
        {
            "Open": g["Open"].first(),
            "High": g["High"].max(),
            "Low": g["Low"].min(),
            "Close": g["Close"].last(),
            "Volume": g["Volume"].sum(),
        }
    )
    daily.index = pd.to_datetime(daily.index)
    return daily


def compute_daily_levels(daily_df: pd.DataFrame, current_date: date) -> Levels | None:
    prior = daily_df[daily_df.index.date < current_date]
    if len(prior) < 2:
        return None
    range_high = float(prior.iloc[-1]["High"])
    range_low = float(prior.iloc[-1]["Low"])
    if range_high <= range_low:
        return None

    swing_high = None
    for h in prior.iloc[-2::-1]["High"]:
        if float(h) > range_high:
            swing_high = float(h)
            break

    swing_low = None
    for lo in prior.iloc[-2::-1]["Low"]:
        if float(lo) < range_low:
            swing_low = float(lo)
            break

    if swing_high is None or swing_low is None:
        return None
    return Levels(
        range_high=range_high,
        range_low=range_low,
        swing_high=swing_high,
        swing_low=swing_low,
    )


def sneaky_pivot_signal(
    day_bars: pd.DataFrame,
    levels: Levels,
    *,
    confirm_window: int = 3,
    fill: str = "touch",
    stretch: bool = False,
) -> Signal | None:
    """Return first valid signal for the session, or None."""
    if len(day_bars) < 2:
        return None

    c1 = day_bars.iloc[0]
    touched_low = float(c1["Low"]) <= levels.range_low
    touched_high = float(c1["High"]) >= levels.range_high
    if not touched_low and not touched_high:
        return None
    # If both, prefer the more extreme pierce by distance.
    if touched_low and touched_high:
        d_low = levels.range_low - float(c1["Low"])
        d_high = float(c1["High"]) - levels.range_high
        side = "long" if d_low >= d_high else "short"
    else:
        side = "long" if touched_low else "short"

    stop = levels.swing_low if side == "long" else levels.swing_high
    if stretch:
        target = levels.swing_high if side == "long" else levels.swing_low
    else:
        target = levels.range_high if side == "long" else levels.range_low

    # Invalid geometry (would be wrong-side stop/target).
    if side == "long" and not (stop < levels.range_low <= levels.range_high):
        return None
    if side == "short" and not (stop > levels.range_high >= levels.range_low):
        return None
    if side == "long" and target <= levels.range_low:
        return None
    if side == "short" and target >= levels.range_high:
        return None

    limit = min(confirm_window + 1, len(day_bars))
    for i in range(1, limit):
        candle = day_bars.iloc[i]
        o, c = float(candle["Open"]), float(candle["Close"])
        is_green = c > o
        is_red = c < o
        confirmed = (side == "long" and is_green) or (side == "short" and is_red)
        if not confirmed:
            continue

        trigger = float(candle["High"]) if side == "long" else float(candle["Low"])

        for j in range(i + 1, len(day_bars)):
            bar = day_bars.iloc[j]
            if fill == "next_open":
                # Conservative: only enter at open of bar AFTER a break bar.
                if j + 1 >= len(day_bars):
                    break
                prev = day_bars.iloc[j]
                broke = (
                    (side == "long" and float(prev["High"]) > trigger)
                    or (side == "short" and float(prev["Low"]) < trigger)
                )
                if not broke:
                    continue
                nxt = day_bars.iloc[j + 1]
                entry = float(nxt["Open"])
                return Signal(
                    side=side,
                    entry_price=entry,
                    stop=stop,
                    target=target,
                    entry_idx=j + 1,
                    confirm_idx=i,
                )

            # Optimistic: fill at trigger when bar touches through it.
            if side == "long" and float(bar["High"]) > trigger:
                return Signal(
                    side=side,
                    entry_price=trigger,
                    stop=stop,
                    target=target,
                    entry_idx=j,
                    confirm_idx=i,
                )
            if side == "short" and float(bar["Low"]) < trigger:
                return Signal(
                    side=side,
                    entry_price=trigger,
                    stop=stop,
                    target=target,
                    entry_idx=j,
                    confirm_idx=i,
                )
        break  # confirmed but no breakout this session

    return None


def _simulate_trade(
    day_bars: pd.DataFrame,
    sig: Signal,
    *,
    equity: float,
) -> Trade | None:
    entry = float(sig.entry_price)
    stop = float(sig.stop)
    target = float(sig.target)
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 1e-8 or entry <= 0:
        return None
    risk_dollars = equity * RISK_PCT
    shares = risk_dollars / risk_per_share
    max_shares = (equity * MAX_NOTIONAL_PCT) / entry
    shares = min(shares, max_shares)
    if shares * entry < 1.0:
        return None

    exit_price = float(day_bars.iloc[-1]["Close"])
    exit_idx = len(day_bars) - 1
    exit_reason = "eod"

    for k in range(sig.entry_idx, len(day_bars)):
        bar = day_bars.iloc[k]
        hi, lo = float(bar["High"]), float(bar["Low"])
        if sig.side == "long":
            # Conservative pathing: stop before target if both in same bar.
            hit_stop = lo <= stop
            hit_tgt = hi >= target
            if hit_stop and hit_tgt:
                exit_price, exit_idx, exit_reason = stop, k, "stop"
                break
            if hit_stop:
                exit_price, exit_idx, exit_reason = stop, k, "stop"
                break
            if hit_tgt:
                exit_price, exit_idx, exit_reason = target, k, "target"
                break
        else:
            hit_stop = hi >= stop
            hit_tgt = lo <= target
            if hit_stop and hit_tgt:
                exit_price, exit_idx, exit_reason = stop, k, "stop"
                break
            if hit_stop:
                exit_price, exit_idx, exit_reason = stop, k, "stop"
                break
            if hit_tgt:
                exit_price, exit_idx, exit_reason = target, k, "target"
                break

    if sig.side == "long":
        pnl = (exit_price - entry) * shares
    else:
        pnl = (entry - exit_price) * shares
    pnl_pct = pnl / equity
    r_mult = pnl / risk_dollars if risk_dollars else 0.0

    entry_ts = day_bars.iloc[sig.entry_idx]["timestamp"]
    exit_ts = day_bars.iloc[exit_idx]["timestamp"]
    return Trade(
        date=str(entry_ts.date()),
        ticker="",  # filled by caller
        side=sig.side,
        entry_time=str(entry_ts),
        exit_time=str(exit_ts),
        entry_price=entry,
        exit_price=exit_price,
        stop=stop,
        target=target,
        shares=float(shares),
        pnl=float(pnl),
        pnl_pct=float(pnl_pct),
        r_multiple=float(r_mult),
        exit_reason=exit_reason,
        confirm_idx=int(sig.confirm_idx),
        entry_idx=int(sig.entry_idx),
    )


def backtest_symbol(
    ticker: str,
    bars: pd.DataFrame,
    *,
    confirm_window: int,
    fill: str,
    stretch: bool,
) -> tuple[list[Trade], dict[str, int]]:
    daily = bars_to_daily(bars)
    counters = {
        "signals": 0,
        "skipped_no_levels": 0,
        "skipped_no_touch": 0,
        "skipped_no_confirm": 0,
    }
    trades: list[Trade] = []
    sessions = sorted(bars["timestamp"].dt.date.unique())
    equity_probe = INITIAL_EQUITY  # per-symbol probe; portfolio equity applied in runner

    for sess in sessions:
        levels = compute_daily_levels(daily, sess)
        if levels is None:
            counters["skipped_no_levels"] += 1
            continue
        day = bars[bars["timestamp"].dt.date == sess].reset_index(drop=True)
        if day.empty:
            continue
        c1 = day.iloc[0]
        touched = (float(c1["Low"]) <= levels.range_low) or (
            float(c1["High"]) >= levels.range_high
        )
        if not touched:
            counters["skipped_no_touch"] += 1
            continue
        sig = sneaky_pivot_signal(
            day,
            levels,
            confirm_window=confirm_window,
            fill=fill,
            stretch=stretch,
        )
        if sig is None:
            counters["skipped_no_confirm"] += 1
            continue
        counters["signals"] += 1
        trade = _simulate_trade(day, sig, equity=equity_probe)
        if trade is None:
            continue
        trade.ticker = ticker
        trades.append(trade)
        equity_probe = max(1_000.0, equity_probe + trade.pnl)

    return trades, counters


def _max_drawdown_pct(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    dd = (equity - peak) / peak.replace(0, np.nan)
    return float(dd.min() * 100.0) if len(dd) else 0.0


def run_backtest(
    *,
    symbols: list[str],
    days: int,
    refresh: bool,
    confirm_window: int,
    fill: str,
    stretch: bool,
    tag: str,
) -> tuple[RunStats, list[Trade], pd.Series]:
    all_trades: list[Trade] = []
    meta = {
        "signals": 0,
        "skipped_no_levels": 0,
        "skipped_no_touch": 0,
        "skipped_no_confirm": 0,
    }
    notes: list[str] = []

    for sym in symbols:
        bars = load_ticker_bars(sym, days=days, refresh=refresh)
        if bars is None or bars.empty:
            notes.append(f"no_15m_data:{sym}")
            print(f"  SKIP {sym}: no 15m data")
            continue
        print(f"  {sym}: {len(bars)} bars, {bars['timestamp'].dt.date.nunique()} sessions")
        trades, counters = backtest_symbol(
            sym,
            bars,
            confirm_window=confirm_window,
            fill=fill,
            stretch=stretch,
        )
        all_trades.extend(trades)
        for k, v in counters.items():
            meta[k] = meta.get(k, 0) + v

    # Portfolio equity: one trade at a time chronologically (research simplification).
    all_trades.sort(key=lambda t: t.entry_time)
    equity = INITIAL_EQUITY
    curve_pts: list[tuple[pd.Timestamp, float]] = []
    sized: list[Trade] = []
    for tr in all_trades:
        # Re-size vs running equity for portfolio stats.
        risk_per_share = abs(tr.entry_price - tr.stop)
        if risk_per_share <= 1e-8:
            continue
        shares = min(
            (equity * RISK_PCT) / risk_per_share,
            (equity * MAX_NOTIONAL_PCT) / tr.entry_price,
        )
        if shares * tr.entry_price < 1.0:
            continue
        if tr.side == "long":
            pnl = (tr.exit_price - tr.entry_price) * shares
        else:
            pnl = (tr.entry_price - tr.exit_price) * shares
        risk_dollars = equity * RISK_PCT
        tr2 = Trade(**{**asdict(tr), "shares": float(shares), "pnl": float(pnl),
                       "pnl_pct": float(pnl / equity),
                       "r_multiple": float(pnl / risk_dollars if risk_dollars else 0.0)})
        equity += pnl
        sized.append(tr2)
        curve_pts.append((pd.Timestamp(tr2.exit_time), equity))

    if curve_pts:
        curve = pd.Series(
            {ts: eq for ts, eq in curve_pts},
            dtype=float,
        ).sort_index()
    else:
        curve = pd.Series([INITIAL_EQUITY], index=[pd.Timestamp.now(tz=ET)])

    rs = [t.r_multiple for t in sized]
    wins = sum(1 for t in sized if t.pnl > 0)
    losses = sum(1 for t in sized if t.pnl <= 0)
    stats = RunStats(
        tag=tag,
        days=days,
        confirm_window=confirm_window,
        fill=fill,
        stretch=stretch,
        symbols=list(symbols),
        trades=len(sized),
        wins=wins,
        losses=losses,
        win_rate=(wins / len(sized)) if sized else 0.0,
        total_pnl=float(equity - INITIAL_EQUITY),
        avg_r=float(np.mean(rs)) if rs else 0.0,
        median_r=float(np.median(rs)) if rs else 0.0,
        max_dd_pct=_max_drawdown_pct(curve),
        final_equity=float(equity),
        signals=int(meta.get("signals", 0)),
        skipped_no_levels=int(meta.get("skipped_no_levels", 0)),
        skipped_no_touch=int(meta.get("skipped_no_touch", 0)),
        notes=notes,
    )
    return stats, sized, curve


def _write_outputs(
    stats: RunStats,
    trades: list[Trade],
    curve: pd.Series,
) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = stats.tag
    csv_path = OUT_DIR / f"sneaky_pivot_trades_{stamp}.csv"
    md_path = OUT_DIR / f"sneaky_pivot_report_{stamp}.md"
    pd.DataFrame([asdict(t) for t in trades]).to_csv(csv_path, index=False)

    lines = [
        f"# Sneaky Pivot research backtest — `{stats.tag}`",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "**Freeze-safe research only** — not wired into paper/live `run_all`.",
        "",
        "## Config",
        "",
        f"- Days requested: {stats.days}",
        f"- Confirm window: {stats.confirm_window}",
        f"- Fill: `{stats.fill}`",
        f"- Stretch target: {stats.stretch}",
        f"- Universe: {', '.join(stats.symbols)}",
        f"- Risk: {RISK_PCT:.0%} equity to stop; max notional {MAX_NOTIONAL_PCT:.0%}",
        "",
        "## Results",
        "",
        f"| Metric | Value |",
        f"|--------|------:|",
        f"| Trades | {stats.trades} |",
        f"| Signals | {stats.signals} |",
        f"| Win rate | {stats.win_rate:.1%} |",
        f"| Total PnL | ${stats.total_pnl:,.2f} |",
        f"| Final equity | ${stats.final_equity:,.2f} |",
        f"| Avg R | {stats.avg_r:.2f} |",
        f"| Median R | {stats.median_r:.2f} |",
        f"| Max DD | {stats.max_dd_pct:.2f}% |",
        f"| Skip (no levels) | {stats.skipped_no_levels} |",
        f"| Skip (no OR touch) | {stats.skipped_no_touch} |",
        "",
    ]
    if stats.notes:
        lines.append("## Notes")
        lines.append("")
        for n in stats.notes:
            lines.append(f"- {n}")
        lines.append("")
    if trades:
        lines.append("## Last 15 trades")
        lines.append("")
        lines.append(
            "| Date | Sym | Side | Entry | Exit | R | Reason |"
        )
        lines.append("|------|-----|------|------:|-----:|--:|--------|")
        for t in trades[-15:]:
            lines.append(
                f"| {t.date} | {t.ticker} | {t.side} | {t.entry_price:.2f} | "
                f"{t.exit_price:.2f} | {t.r_multiple:.2f} | {t.exit_reason} |"
            )
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")

    json_path = OUT_DIR / f"sneaky_pivot_stats_{stamp}.json"
    payload = asdict(stats)
    payload["equity_last"] = float(curve.iloc[-1]) if len(curve) else INITIAL_EQUITY
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return csv_path, md_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sneaky Pivot 15m research backtest")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--refresh", action="store_true", help="Refetch 15m bars from Alpaca")
    ap.add_argument("--confirm-window", type=int, default=3)
    ap.add_argument(
        "--fill",
        choices=("touch", "next_open"),
        default="touch",
        help="touch=fill at trigger; next_open=enter next bar open after break",
    )
    ap.add_argument(
        "--stretch",
        action="store_true",
        help="Target swing extreme instead of opposite range",
    )
    ap.add_argument(
        "--symbols",
        default=",".join(DEFAULT_UNIVERSE),
        help="Comma-separated symbols (Alpaca form, e.g. BRK.B)",
    )
    ap.add_argument("--tag", default="baseline", help="Output file tag")
    ap.add_argument(
        "--run-matrix",
        action="store_true",
        help="Run baseline + strict_c2 + next_open (+ stretch) variants",
    )
    args = ap.parse_args(argv)

    _load_env()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    print("=" * 64)
    print("Sneaky Pivot research backtest (15m) — freeze-safe, not live/paper")
    print("=" * 64)

    runs: list[tuple[str, int, str, bool]] = []
    if args.run_matrix:
        runs = [
            ("baseline_w3_touch", 3, "touch", False),
            ("strict_c2_touch", 1, "touch", False),
            ("baseline_w3_next_open", 3, "next_open", False),
            ("baseline_w3_touch_stretch", 3, "touch", True),
        ]
    else:
        runs = [(args.tag, args.confirm_window, args.fill, args.stretch)]

    summary_rows = []
    for tag, window, fill, stretch in runs:
        print(f"\n--- run tag={tag} confirm={window} fill={fill} stretch={stretch} ---")
        stats, trades, curve = run_backtest(
            symbols=symbols,
            days=args.days,
            refresh=args.refresh and tag == runs[0][0],
            confirm_window=window,
            fill=fill,
            stretch=stretch,
            tag=tag,
        )
        # Subsequent matrix legs reuse cache (refresh only first).
        csv_path, md_path = _write_outputs(stats, trades, curve)
        print(
            f"  trades={stats.trades} win={stats.win_rate:.1%} "
            f"pnl=${stats.total_pnl:,.0f} avgR={stats.avg_r:.2f} "
            f"maxDD={stats.max_dd_pct:.1f}%"
        )
        print(f"  wrote {md_path.name} | {csv_path.name}")
        summary_rows.append(stats)

    # Matrix summary
    if len(summary_rows) > 1:
        sum_path = OUT_DIR / "sneaky_pivot_matrix_last.md"
        lines = [
            "# Sneaky Pivot matrix summary",
            "",
            f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            f"Days={args.days} | Universe={', '.join(symbols)}",
            "",
            "| Tag | Confirm | Fill | Stretch | Trades | Win% | PnL | Avg R | MaxDD% |",
            "|-----|--------:|------|---------|-------:|-----:|----:|------:|-------:|",
        ]
        for s in summary_rows:
            lines.append(
                f"| `{s.tag}` | {s.confirm_window} | {s.fill} | {s.stretch} | "
                f"{s.trades} | {s.win_rate:.1%} | ${s.total_pnl:,.0f} | "
                f"{s.avg_r:.2f} | {s.max_dd_pct:.1f} |"
            )
        lines.append("")
        sum_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"\nMatrix summary: {sum_path}")

    print("\nDone. Research only — freeze unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
