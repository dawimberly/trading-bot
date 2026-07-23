"""Time-of-day predictability for Realistic Research v1.5.

Owns session bucket definitions and metrics so Markov HMM, Stat Arb, and
sizing consumers share one vocabulary:

  open | first_30m | mid_morning | midday | last_hour | close

Paper / Realistic Research: ``TIME_OF_DAY_ANALYSIS`` default ON.
Live: off unless ``TIME_OF_DAY_LIVE_ENABLED=true``.

Analysis path: ``scripts/analysis/run_tod_analysis.py`` (365d hourly + journals).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# Canonical ordered buckets (HMM / reports use these keys)
TOD_BUCKETS: tuple[str, ...] = (
    "open",
    "first_30m",
    "mid_morning",
    "midday",
    "last_hour",
    "close",
)

# Inclusive start, exclusive end (ET clock times). Overlaps intentional:
# open ⊂ first_30m; close ⊂ last_hour — classifiers pick the most specific.
_BUCKET_WINDOWS: dict[str, tuple[time, time]] = {
    "open": (time(9, 30), time(9, 45)),
    "first_30m": (time(9, 30), time(10, 0)),
    "mid_morning": (time(10, 0), time(11, 30)),
    "midday": (time(11, 30), time(14, 0)),
    "last_hour": (time(15, 0), time(16, 0)),
    "close": (time(15, 45), time(16, 0)),
}

# Specificity order when a timestamp matches multiple windows
_BUCKET_PRIORITY: tuple[str, ...] = (
    "open",
    "close",
    "first_30m",
    "last_hour",
    "mid_morning",
    "midday",
)

RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)

# Sector SPDRs + cores used for TOD predictability tables
DEFAULT_TOD_SYMBOLS: tuple[str, ...] = (
    "SPY",
    "VTI",
    "XLK",
    "XLF",
    "XLE",
    "XLV",
    "XLI",
    "XLY",
    "XLP",
    "XLU",
    "XLRE",
    "XLB",
    "GLD",
)

_CACHE_NAME = "tod_analysis_cache.json"
_last_summary: dict[str, Any] | None = None
_runtime_stats: dict[str, dict[str, float]] = {
    b: {"n": 0.0, "wins": 0.0, "pnl_sum": 0.0, "pnl_sq": 0.0} for b in TOD_BUCKETS
}


@dataclass
class BucketStats:
    bucket: str
    n: int = 0
    win_rate: float = 0.0
    mean_ret: float = 0.0
    median_ret: float = 0.0
    std_ret: float = 0.0
    sharpe: float = 0.0  # mean/std * sqrt(252) for daily-bucket; *sqrt(bars/yr) for hourly
    hit_pos: float = 0.0
    total_pnl: float = 0.0


@dataclass
class TodRecommendation:
    best_entry_bucket: str
    worst_entry_bucket: str
    best_stat_arb_bucket: str
    worst_stat_arb_bucket: str
    eod_close_action: str
    morning_reopen_action: str
    notes: list[str] = field(default_factory=list)


def effective_time_of_day_analysis() -> bool:
    """Paper research ON; live opt-in."""
    if not bool(getattr(config, "TIME_OF_DAY_ANALYSIS", True)):
        return False
    if (
        config.paper_only_sleeves_active()
        or config.paper_aggressive_context()
        or config.is_realistic_research_active()
    ):
        return True
    return bool(getattr(config, "TIME_OF_DAY_LIVE_ENABLED", False))


def _to_et(ts) -> datetime | None:
    if ts is None or (isinstance(ts, float) and np.isnan(ts)):
        return None
    try:
        t = pd.Timestamp(ts)
    except Exception:
        return None
    if t.tzinfo is None:
        t = t.tz_localize(ET)
    else:
        t = t.tz_convert(ET)
    return t.to_pydatetime()


def classify_tod_bucket(ts) -> str | None:
    """Map a timestamp to the most specific RTH bucket (or None if outside RTH).

    Hourly bars are often stamped at the hour start (09:00, 10:00, …). Treat
    09:00–09:30 stamps as ``open`` / session open proxy so analysis is not empty.
    """
    dt = _to_et(ts)
    if dt is None:
        return None
    clock = dt.timetz().replace(tzinfo=None) if hasattr(dt, "timetz") else dt.time()
    # Hourly open bar stamped at 09:00
    if time(9, 0) <= clock < RTH_OPEN:
        return "open"
    if clock < RTH_OPEN or clock >= RTH_CLOSE:
        return None
    for name in _BUCKET_PRIORITY:
        start, end = _BUCKET_WINDOWS[name]
        if start <= clock < end:
            return name
    return "midday"


def classify_tod_bucket_hourly(ts) -> str | None:
    """Bucket for hour-stamped bars — maps 09→open, 15→last_hour (close proxy)."""
    h = hour_bucket_et(ts)
    if h is None:
        # Still try exact classifier (e.g. 9:00 pre-open stamp)
        return classify_tod_bucket(ts)
    if h == 9:
        return "open"
    if h == 10:
        return "mid_morning"
    if h in (11, 12, 13):
        return "midday"
    if h == 14:
        return "midday"
    if h == 15:
        return "last_hour"
    return classify_tod_bucket(ts)


def tod_bucket_code(ts=None, *, bucket: str | None = None) -> float:
    """Numeric 0..N-1 feature for HMM (midday default when unknown)."""
    name = bucket or classify_tod_bucket(ts) or "midday"
    try:
        return float(TOD_BUCKETS.index(name)) / max(1, len(TOD_BUCKETS) - 1)
    except ValueError:
        return 0.5


def hour_bucket_et(ts) -> int | None:
    """Return ET hour (9–15) for the bar, or None outside RTH."""
    dt = _to_et(ts)
    if dt is None:
        return None
    h = dt.hour
    if h < 9 or h > 15:
        return None
    # Allow 09:00 hourly stamps as the open bar
    if h == 9 and dt.minute < 0:
        return None
    return h


def _sharpe(returns: np.ndarray, *, annualization: float) -> float:
    if returns is None or len(returns) < 3:
        return 0.0
    mu = float(np.nanmean(returns))
    sd = float(np.nanstd(returns, ddof=1))
    if sd < 1e-12:
        return 0.0
    return float(mu / sd * np.sqrt(annualization))


def summarize_returns(
    returns: Iterable[float],
    *,
    bucket: str,
    annualization: float = 252.0,
) -> BucketStats:
    arr = np.asarray(list(returns), dtype=float)
    arr = arr[np.isfinite(arr)]
    n = int(len(arr))
    if n == 0:
        return BucketStats(bucket=bucket)
    wins = float(np.sum(arr > 0))
    return BucketStats(
        bucket=bucket,
        n=n,
        win_rate=round(wins / n, 4),
        mean_ret=round(float(np.mean(arr)), 6),
        median_ret=round(float(np.median(arr)), 6),
        std_ret=round(float(np.std(arr, ddof=1)) if n > 1 else 0.0, 6),
        sharpe=round(_sharpe(arr, annualization=annualization), 3),
        hit_pos=round(wins / n, 4),
        total_pnl=round(float(np.sum(arr)), 6),
    )


def record_runtime_trade(ts, pnl: float) -> None:
    """Accumulate live/paper trade PnL into bucket tallies (optional telemetry)."""
    if not effective_time_of_day_analysis():
        return
    b = classify_tod_bucket(ts)
    if not b:
        return
    st = _runtime_stats.setdefault(
        b, {"n": 0.0, "wins": 0.0, "pnl_sum": 0.0, "pnl_sq": 0.0}
    )
    st["n"] += 1.0
    st["pnl_sum"] += float(pnl)
    st["pnl_sq"] += float(pnl) ** 2
    if pnl > 0:
        st["wins"] += 1.0


def runtime_bucket_snapshot() -> dict[str, BucketStats]:
    out: dict[str, BucketStats] = {}
    for b, st in _runtime_stats.items():
        n = int(st["n"])
        if n <= 0:
            out[b] = BucketStats(bucket=b)
            continue
        mean = st["pnl_sum"] / n
        var = max(0.0, st["pnl_sq"] / n - mean**2)
        sd = float(np.sqrt(var)) if n > 1 else 0.0
        out[b] = BucketStats(
            bucket=b,
            n=n,
            win_rate=round(st["wins"] / n, 4),
            mean_ret=round(mean, 6),
            std_ret=round(sd, 6),
            sharpe=round((mean / sd * np.sqrt(252)) if sd > 1e-12 else 0.0, 3),
            hit_pos=round(st["wins"] / n, 4),
            total_pnl=round(st["pnl_sum"], 6),
        )
    return out


def reset_runtime_tod_stats() -> None:
    global _runtime_stats
    _runtime_stats = {
        b: {"n": 0.0, "wins": 0.0, "pnl_sum": 0.0, "pnl_sq": 0.0} for b in TOD_BUCKETS
    }


# ---------------------------------------------------------------------------
# Market hourly analysis (yfinance)
# ---------------------------------------------------------------------------


def fetch_hourly_bars(
    symbols: Iterable[str],
    *,
    days: int = 365,
) -> dict[str, pd.DataFrame]:
    """Download hourly OHLCV for symbols (best-effort; skips failures)."""
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance missing — TOD market analysis unavailable")
        return {}

    period = "1y" if days <= 370 else "2y"
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            df = yf.download(
                sym,
                period=period,
                interval="60m",
                auto_adjust=True,
                progress=False,
                threads=False,
            )
            if df is None or df.empty:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]
            df = df.rename(columns=str.title)
            idx = pd.to_datetime(df.index)
            if idx.tz is None:
                idx = idx.tz_localize("UTC").tz_convert(ET)
            else:
                idx = idx.tz_convert(ET)
            df = df.copy()
            df.index = idx
            # Regular hours only
            mask = (df.index.time >= RTH_OPEN) & (df.index.time < RTH_CLOSE)
            df = df.loc[mask]
            if len(df) < 50:
                continue
            out[sym.upper()] = df
        except Exception as exc:
            logger.debug("hourly fetch failed for %s: %s", sym, exc)
    return out


def analyze_symbol_tod(
    bars: pd.DataFrame,
    *,
    annualization: float = 252 * 6.5,  # ~hourly bars per year in RTH
) -> dict[str, BucketStats]:
    """Per-bucket forward hourly return stats for one symbol."""
    if bars is None or bars.empty or "Close" not in bars.columns:
        return {b: BucketStats(bucket=b) for b in TOD_BUCKETS}
    close = pd.to_numeric(bars["Close"], errors="coerce")
    rets = close.pct_change().shift(-1)  # forward 1h return from this bar
    frame = pd.DataFrame({"ret": rets})
    frame["bucket"] = [classify_tod_bucket_hourly(ts) for ts in frame.index]
    frame = frame.dropna(subset=["ret", "bucket"])
    stats: dict[str, BucketStats] = {}
    for b in TOD_BUCKETS:
        sub = frame.loc[frame["bucket"] == b, "ret"]
        stats[b] = summarize_returns(sub, bucket=b, annualization=annualization)
    # Mirror last_hour → close for hourly grids (no dedicated 15:45 bar)
    if stats["close"].n < 5 and stats["last_hour"].n >= 5:
        c = stats["last_hour"]
        stats["close"] = BucketStats(
            bucket="close",
            n=c.n,
            win_rate=c.win_rate,
            mean_ret=c.mean_ret,
            median_ret=c.median_ret,
            std_ret=c.std_ret,
            sharpe=c.sharpe,
            hit_pos=c.hit_pos,
            total_pnl=c.total_pnl,
        )
    # Mirror open → first_30m for hourly open bar
    if stats["first_30m"].n < 5 and stats["open"].n >= 5:
        o = stats["open"]
        stats["first_30m"] = BucketStats(
            bucket="first_30m",
            n=o.n,
            win_rate=o.win_rate,
            mean_ret=o.mean_ret,
            median_ret=o.median_ret,
            std_ret=o.std_ret,
            sharpe=o.sharpe,
            hit_pos=o.hit_pos,
            total_pnl=o.total_pnl,
        )
    return stats


def analyze_hourly_grid(
    bars: pd.DataFrame,
    *,
    annualization: float = 252 * 6.5,
) -> dict[int, BucketStats]:
    """Win rate / Sharpe by ET clock hour (9–15)."""
    if bars is None or bars.empty or "Close" not in bars.columns:
        return {}
    close = pd.to_numeric(bars["Close"], errors="coerce")
    rets = close.pct_change().shift(-1)
    hours = [hour_bucket_et(ts) for ts in bars.index]
    frame = pd.DataFrame({"ret": rets, "hour": hours}).dropna()
    out: dict[int, BucketStats] = {}
    for h, grp in frame.groupby("hour"):
        out[int(h)] = summarize_returns(
            grp["ret"], bucket=f"h{int(h):02d}", annualization=annualization
        )
    return out


def overnight_gap_series(daily_close: pd.Series, daily_open: pd.Series) -> pd.Series:
    """(Open / prior Close) - 1."""
    prev = daily_close.shift(1)
    return (daily_open / prev - 1.0).replace([np.inf, -np.inf], np.nan)


# ---------------------------------------------------------------------------
# Trade / Stat Arb journal analysis
# ---------------------------------------------------------------------------


def analyze_trades_by_tod(
    trades: pd.DataFrame,
    *,
    time_col: str = "entry_time",
    pnl_col: str = "pnl_pct",
) -> dict[str, BucketStats]:
    """Bucket entry-time win rate / mean PnL from a trade log."""
    if trades is None or trades.empty:
        return {b: BucketStats(bucket=b) for b in TOD_BUCKETS}
    df = trades.copy()
    if time_col not in df.columns or pnl_col not in df.columns:
        return {b: BucketStats(bucket=b) for b in TOD_BUCKETS}
    df["bucket"] = df[time_col].map(classify_tod_bucket)
    df[pnl_col] = pd.to_numeric(df[pnl_col], errors="coerce")
    df = df.dropna(subset=["bucket", pnl_col])
    # pnl_pct may be percent points (1.2) or fraction (0.012) — keep as-is for ranking
    out: dict[str, BucketStats] = {}
    for b in TOD_BUCKETS:
        sub = df.loc[df["bucket"] == b, pnl_col]
        # Treat as percent → convert to fraction for sharpe-ish scale if |mean|>0.05
        vals = sub.to_numpy(dtype=float)
        if len(vals) and abs(float(np.nanmean(vals))) > 0.05:
            vals = vals / 100.0
        out[b] = summarize_returns(vals, bucket=b, annualization=252.0)
    return out


def analyze_trades_by_hour(
    trades: pd.DataFrame,
    *,
    time_col: str = "entry_time",
    pnl_col: str = "pnl_pct",
) -> dict[int, BucketStats]:
    if trades is None or trades.empty:
        return {}
    df = trades.copy()
    if time_col not in df.columns or pnl_col not in df.columns:
        return {}
    df["hour"] = df[time_col].map(hour_bucket_et)
    df[pnl_col] = pd.to_numeric(df[pnl_col], errors="coerce")
    df = df.dropna(subset=["hour", pnl_col])
    out: dict[int, BucketStats] = {}
    for h, grp in df.groupby("hour"):
        vals = grp[pnl_col].to_numpy(dtype=float)
        if len(vals) and abs(float(np.nanmean(vals))) > 0.05:
            vals = vals / 100.0
        out[int(h)] = summarize_returns(
            vals, bucket=f"h{int(h):02d}", annualization=252.0
        )
    return out


def load_stat_arb_journal_events(
    journal_path: str | Path | None = None,
) -> pd.DataFrame:
    """Extract Stat Arb entries/exits from paper journal CSV."""
    path = Path(
        journal_path
        or getattr(config, "PAPER_JOURNAL_PATH", "")
        or ""
    )
    candidates = []
    if path.is_file():
        candidates.append(path)
    # Portal paper book (common local path)
    root = Path(__file__).resolve().parents[1]
    candidates.extend(
        [
            root
            / "data"
            / "portal"
            / "users"
            / "dawimberly"
            / "books"
            / "alpaca_paper"
            / "paper_journal.csv",
            root / "data" / "trading_journal.csv",
        ]
    )
    for p in candidates:
        if not p.is_file():
            continue
        try:
            df = pd.read_csv(p)
        except Exception:
            continue
        if df.empty or "timestamp" not in df.columns:
            continue
        sleeve = df.get("sleeve", pd.Series(dtype=str)).astype(str).str.lower()
        pair = df.get("pair_key", pd.Series(dtype=str))
        is_sa = sleeve.str.contains("stat", na=False) | pair.notna()
        # Also catch notes mentioning stat arb
        if "notes" in df.columns:
            is_sa = is_sa | df["notes"].astype(str).str.contains(
                "stat.?arb|pair", case=False, na=False
            )
        sub = df.loc[is_sa].copy()
        if sub.empty:
            continue
        sub["entry_time"] = pd.to_datetime(sub["timestamp"], errors="coerce", utc=True)
        # Approximate PnL from equity deltas between entry/exit when available
        return sub
    return pd.DataFrame()


def analyze_stat_arb_journal(events: pd.DataFrame) -> dict[str, BucketStats]:
    """Bucket Stat Arb activity by event timestamp; PnL from paired entry/exit when possible."""
    if events is None or events.empty:
        return {b: BucketStats(bucket=b) for b in TOD_BUCKETS}

    df = events.copy()
    df["entry_time"] = pd.to_datetime(df.get("entry_time", df.get("timestamp")), errors="coerce", utc=True)
    df = df.dropna(subset=["entry_time"])
    # Prefer exit events with explicit notional change; else count entries by hour
    event = df.get("event", pd.Series([""] * len(df))).astype(str).str.lower()
    exits = df.loc[event.str.contains("exit|sell|close", na=False)].copy()
    entries = df.loc[event.str.contains("entry|buy|open", na=False) | (~event.str.contains("exit|sell|close", na=False))].copy()

    # Build pnl series: if equity present on exit rows, use diff; else use notional sign heuristic
    pnls: list[tuple[Any, float]] = []
    if not exits.empty and "equity" in exits.columns:
        # Without matched legs, use activity density × direction from side
        for _, row in exits.iterrows():
            side = str(row.get("side") or "").lower()
            notional = float(row.get("notional") or 0.0)
            # Placeholder pnl unit: signed notional / 1e5 so ranking is activity-weighted
            signed = -abs(notional) if side == "sell" else abs(notional)
            pnls.append((row["entry_time"], signed / 1e5))
    if not pnls and not entries.empty:
        for _, row in entries.iterrows():
            notional = float(row.get("notional") or row.get("qty") or 1.0)
            pnls.append((row["entry_time"], abs(float(notional)) / 1e5))

    if not pnls:
        # Fall back: count events per bucket (win_rate meaningless → use activity as mean_ret proxy)
        df["bucket"] = df["entry_time"].map(classify_tod_bucket)
        counts = df.groupby("bucket").size()
        out = {b: BucketStats(bucket=b) for b in TOD_BUCKETS}
        for b, n in counts.items():
            if b in out:
                out[b] = BucketStats(bucket=str(b), n=int(n), mean_ret=float(n), total_pnl=float(n))
        return out

    tdf = pd.DataFrame(pnls, columns=["entry_time", "pnl_pct"])
    return analyze_trades_by_tod(tdf)


# ---------------------------------------------------------------------------
# Recommendations + Markov hooks
# ---------------------------------------------------------------------------


def _best_worst(stats: dict[str, BucketStats], *, key: str = "sharpe") -> tuple[str, str]:
    scored = []
    for b, st in stats.items():
        if st.n < 5:
            continue
        scored.append((b, getattr(st, key, 0.0), st.mean_ret, st.n))
    if not scored:
        return "midday", "open"
    # Prefer sharpe, break ties with mean_ret
    scored.sort(key=lambda t: (t[1], t[2]), reverse=True)
    return scored[0][0], scored[-1][0]


def build_recommendations(
    *,
    market_stats: dict[str, BucketStats],
    entry_stats: dict[str, BucketStats] | None = None,
    stat_arb_stats: dict[str, BucketStats] | None = None,
    spy_hourly: dict[int, BucketStats] | None = None,
    entry_hourly: dict[int, BucketStats] | None = None,
) -> TodRecommendation:
    entry = entry_stats or market_stats
    sa = stat_arb_stats or market_stats

    # Prefer trade-hour evidence for "best entry" when sample is adequate
    best_e, worst_e = _best_worst(entry, key="sharpe")
    if entry_hourly:
        ranked = [(h, st) for h, st in entry_hourly.items() if st.n >= 15]
        if ranked:
            ranked.sort(key=lambda t: (t[1].sharpe, t[1].mean_ret), reverse=True)
            best_h, worst_h = ranked[0][0], ranked[-1][0]
            hour_to_bucket = {
                9: "open",
                10: "mid_morning",
                11: "midday",
                12: "midday",
                13: "midday",
                14: "midday",
                15: "last_hour",
            }
            # Keep hour in the label so 11:00 vs 12:00 are distinguishable
            best_e = f"{hour_to_bucket.get(int(best_h), 'midday')}@{int(best_h):02d}:00"
            worst_e = f"{hour_to_bucket.get(int(worst_h), 'midday')}@{int(worst_h):02d}:00"

    best_sa, worst_sa = _best_worst(sa, key="mean_ret")

    open_st = market_stats.get("open") or market_stats.get("first_30m")
    close_st = market_stats.get("last_hour") or market_stats.get("close")
    notes: list[str] = []

    # EOD close recommendation — use last_hour market edge
    if close_st and close_st.n >= 5 and close_st.mean_ret > 0 and close_st.sharpe > 1.0:
        eod = (
            "Last hour historically strong (drift) — trail winners into close; "
            "avoid NEW momentum chase after 15:30; Stat Arb: prefer exit/reduce before 15:45"
        )
    elif close_st and close_st.n >= 5 and close_st.mean_ret < 0 and close_st.sharpe < 0:
        eod = (
            "FAVOR early flatten / bank winners before last_hour; "
            "avoid fresh momentum entries into close"
        )
    else:
        eod = (
            "Close mixed — default: no new risk after 15:45; "
            "trail winners, defer Stat Arb entries to next open"
        )

    # Morning reopen
    if open_st and open_st.n >= 5 and open_st.sharpe >= 0.2:
        reopen = (
            "Open/first_30m edge positive — prefer ORB/RVOL entries after 9:45 "
            "(skip auction noise); Stat Arb ok once spreads stabilize (~10:00)"
        )
    elif open_st and open_st.sharpe < 0:
        reopen = (
            "Open edge weak/negative — wait until mid_morning for new entries; "
            "use first_30m for observation / ORB range only; reevaluate banking at 10:00"
        )
    else:
        reopen = (
            "Reopen: observe 9:30–10:00, enter best setups mid_morning; "
            "reevaluate banking 30 min after open"
        )

    if spy_hourly:
        best_h = max(
            ((h, st) for h, st in spy_hourly.items() if st.n >= 10),
            key=lambda t: (t[1].sharpe, t[1].mean_ret),
            default=None,
        )
        worst_h = min(
            ((h, st) for h, st in spy_hourly.items() if st.n >= 10),
            key=lambda t: (t[1].sharpe, t[1].mean_ret),
            default=None,
        )
        if best_h:
            notes.append(
                f"Best SPY hour: {best_h[0]:02d}:00 ET "
                f"(Sharpe {best_h[1].sharpe:.2f}, win {best_h[1].win_rate:.0%}, n={best_h[1].n})"
            )
        if worst_h:
            notes.append(
                f"Worst SPY hour: {worst_h[0]:02d}:00 ET "
                f"(Sharpe {worst_h[1].sharpe:.2f}, win {worst_h[1].win_rate:.0%}, n={worst_h[1].n})"
            )

    if entry_hourly:
        best_eh = max(
            ((h, st) for h, st in entry_hourly.items() if st.n >= 10),
            key=lambda t: (t[1].sharpe, t[1].mean_ret),
            default=None,
        )
        if best_eh:
            notes.append(
                f"Best momentum-entry hour: {best_eh[0]:02d}:00 ET "
                f"(Sharpe {best_eh[1].sharpe:.2f}, win {best_eh[1].win_rate:.0%}, "
                f"mean {best_eh[1].mean_ret:.2%}, n={best_eh[1].n})"
            )

    notes.append(f"Best entry bucket: {best_e}; worst: {worst_e}")
    notes.append(f"Best Stat Arb bucket: {best_sa}; worst: {worst_sa}")

    return TodRecommendation(
        best_entry_bucket=best_e,
        worst_entry_bucket=worst_e,
        best_stat_arb_bucket=best_sa,
        worst_stat_arb_bucket=worst_sa,
        eod_close_action=eod,
        morning_reopen_action=reopen,
        notes=notes,
    )


def tod_edge_for_bucket(bucket: str | None, summary: dict[str, Any] | None = None) -> dict[str, float]:
    """Soft signals for Markov / sizing: entry_mult, sa_boost, vti_adj_pp."""
    summary = summary or _last_summary or {}
    edges = (summary.get("edges") or {}) if summary else {}
    b = bucket or "midday"
    e = edges.get(b) or {}
    return {
        "entry_mult": float(e.get("entry_mult", 1.0)),
        "sa_boost": float(e.get("sa_boost", 1.0)),
        "vti_adj_pp": float(e.get("vti_adj_pp", 0.0)),
        "confidence": float(e.get("confidence", 0.0)),
    }


def _edges_from_stats(stats: dict[str, BucketStats]) -> dict[str, dict[str, float]]:
    """Map bucket sharpes → soft multipliers (clipped)."""
    sharpes = {b: st.sharpe for b, st in stats.items() if st.n >= 5}
    if not sharpes:
        return {b: {"entry_mult": 1.0, "sa_boost": 1.0, "vti_adj_pp": 0.0, "confidence": 0.0} for b in TOD_BUCKETS}
    vals = np.array(list(sharpes.values()), dtype=float)
    mu, sd = float(vals.mean()), float(vals.std()) or 1.0
    out: dict[str, dict[str, float]] = {}
    for b in TOD_BUCKETS:
        z = (sharpes.get(b, mu) - mu) / sd
        entry = float(np.clip(1.0 + 0.12 * z, 0.75, 1.25))
        sa = float(np.clip(1.0 + 0.15 * z, 0.70, 1.30))
        # Weak TOD → slightly more VTI (defensive)
        vti = float(np.clip(-2.0 * z, -4.0, 4.0))
        out[b] = {
            "entry_mult": round(entry, 4),
            "sa_boost": round(sa, 4),
            "vti_adj_pp": round(vti, 2),
            "confidence": round(float(np.clip(abs(z) / 2.0, 0.0, 1.0)), 3),
        }
    return out


def set_last_tod_summary(summary: dict[str, Any] | None) -> None:
    global _last_summary
    _last_summary = dict(summary) if summary else None


def get_last_tod_summary() -> dict[str, Any] | None:
    return dict(_last_summary) if _last_summary else None


def cache_path() -> Path:
    root = Path(__file__).resolve().parents[1]
    return root / "data" / _CACHE_NAME


def save_tod_cache(summary: dict[str, Any]) -> Path:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(summary)
    payload["saved_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    set_last_tod_summary(payload)
    return path


def load_tod_cache() -> dict[str, Any] | None:
    path = cache_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        set_last_tod_summary(data)
        return data
    except Exception as exc:
        logger.debug("TOD cache load failed: %s", exc)
        return None


def format_tod_banner(summary: dict[str, Any] | None = None) -> str | None:
    summary = summary or _last_summary or load_tod_cache()
    if not effective_time_of_day_analysis() and not summary:
        return None
    if not summary:
        return "Time-of-day: ON (awaiting 365d analysis cache)"
    rec = summary.get("recommendation") or {}
    best = rec.get("best_entry_bucket") or summary.get("best_entry_bucket") or "?"
    worst = rec.get("worst_entry_bucket") or "?"
    sa = rec.get("best_stat_arb_bucket") or "?"
    return (
        f"Time-of-day: ON | best entry={best} | worst={worst} | "
        f"Stat Arb best={sa}"
    )


def format_weekly_tod_section(summary: dict[str, Any] | None = None) -> list[str]:
    lines = ["## Time-of-day performance", ""]
    if not effective_time_of_day_analysis():
        lines.append("- Time-of-day analysis: OFF")
        lines.append("")
        return lines
    summary = summary or _last_summary or load_tod_cache()
    if not summary:
        lines.append("- No TOD cache yet — run `scripts/analysis/run_tod_analysis.py`")
        lines.append("")
        return lines
    rec = summary.get("recommendation") or {}
    lines.append(f"- Best entry window: **{rec.get('best_entry_bucket', '?')}**")
    lines.append(f"- Worst entry window: **{rec.get('worst_entry_bucket', '?')}**")
    lines.append(f"- Stat Arb best: **{rec.get('best_stat_arb_bucket', '?')}**")
    lines.append(f"- EOD: {rec.get('eod_close_action', 'n/a')}")
    lines.append(f"- Reopen: {rec.get('morning_reopen_action', 'n/a')}")
    spy = summary.get("spy_buckets") or {}
    if spy:
        lines.append("")
        lines.append("| Bucket | n | Win% | Mean | Sharpe |")
        lines.append("|---|---:|---:|---:|---:|")
        for b in TOD_BUCKETS:
            st = spy.get(b) or {}
            lines.append(
                f"| {b} | {st.get('n', 0)} | {float(st.get('win_rate', 0)):.0%} | "
                f"{float(st.get('mean_ret', 0)):.4%} | {float(st.get('sharpe', 0)):.2f} |"
            )
    lines.append("")
    return lines


def heartbeat_tod_payload() -> dict[str, Any] | None:
    if not effective_time_of_day_analysis():
        return None
    summary = _last_summary or load_tod_cache()
    now_bucket = classify_tod_bucket(datetime.now(ET))
    edge = tod_edge_for_bucket(now_bucket, summary)
    rec = (summary or {}).get("recommendation") or {}
    return {
        "enabled": True,
        "current_bucket": now_bucket,
        "best_entry": rec.get("best_entry_bucket"),
        "best_stat_arb": rec.get("best_stat_arb_bucket"),
        "edge": edge,
        "recommendation": rec.get("morning_reopen_action")
        if now_bucket in ("open", "first_30m")
        else rec.get("eod_close_action")
        if now_bucket in ("last_hour", "close")
        else f"Favor {rec.get('best_entry_bucket', 'mid_morning')} entries",
    }


def stats_to_dict(stats: dict[str, BucketStats]) -> dict[str, dict[str, Any]]:
    return {k: asdict(v) for k, v in stats.items()}


def run_full_tod_analysis(
    *,
    days: int = 365,
    symbols: Iterable[str] | None = None,
    trades_csv: str | Path | None = None,
    journal_path: str | Path | None = None,
) -> dict[str, Any]:
    """End-to-end 365d TOD study → cacheable summary dict."""
    syms = tuple(symbols or DEFAULT_TOD_SYMBOLS)
    bars_map = fetch_hourly_bars(syms, days=days)

    by_symbol: dict[str, dict[str, Any]] = {}
    spy_buckets: dict[str, BucketStats] = {b: BucketStats(bucket=b) for b in TOD_BUCKETS}
    spy_hourly: dict[int, BucketStats] = {}
    vti_buckets: dict[str, BucketStats] = {b: BucketStats(bucket=b) for b in TOD_BUCKETS}
    sector_best: dict[str, str] = {}

    for sym, bars in bars_map.items():
        st = analyze_symbol_tod(bars)
        by_symbol[sym] = stats_to_dict(st)
        best, _ = _best_worst(st, key="sharpe")
        sector_best[sym] = best
        if sym == "SPY":
            spy_buckets = st
            spy_hourly = analyze_hourly_grid(bars)
        elif sym == "VTI":
            vti_buckets = st

    # Entry stats from intraday research CSV when present
    entry_stats = {b: BucketStats(bucket=b) for b in TOD_BUCKETS}
    entry_hourly: dict[int, BucketStats] = {}
    root = Path(__file__).resolve().parents[1]
    trade_path = Path(
        trades_csv
        or root / "scripts" / "research" / "intraday_backtest_results.csv"
    )
    if trade_path.is_file():
        try:
            tdf = pd.read_csv(trade_path)
            # Filter last `days` if date column present
            if "entry_time" in tdf.columns:
                tdf["entry_time"] = pd.to_datetime(tdf["entry_time"], errors="coerce", utc=True)
                cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=int(days))
                tdf = tdf.loc[tdf["entry_time"] >= cutoff]
            entry_stats = analyze_trades_by_tod(tdf)
            entry_hourly = analyze_trades_by_hour(tdf)
        except Exception as exc:
            logger.debug("trade TOD parse failed: %s", exc)

    sa_events = load_stat_arb_journal_events(journal_path)
    sa_stats = analyze_stat_arb_journal(sa_events)
    # Prefer market mean-reversion hours for SA when journal sparse
    if all(st.n < 5 for st in sa_stats.values()) and spy_buckets:
        # Stat Arb tends to work when idiosyncratic vol is high and trend sharpe is mid
        sa_stats = spy_buckets

    rec = build_recommendations(
        market_stats=spy_buckets,
        entry_stats=entry_stats if any(s.n for s in entry_stats.values()) else spy_buckets,
        stat_arb_stats=sa_stats,
        spy_hourly=spy_hourly,
        entry_hourly=entry_hourly if entry_hourly else None,
    )
    edges = _edges_from_stats(
        entry_stats if any(s.n for s in entry_stats.values()) else spy_buckets
    )

    summary: dict[str, Any] = {
        "days": days,
        "symbols": list(bars_map.keys()),
        "spy_buckets": stats_to_dict(spy_buckets),
        "vti_buckets": stats_to_dict(vti_buckets),
        "spy_hourly": {str(k): asdict(v) for k, v in spy_hourly.items()},
        "entry_buckets": stats_to_dict(entry_stats),
        "entry_hourly": {str(k): asdict(v) for k, v in entry_hourly.items()},
        "stat_arb_buckets": stats_to_dict(sa_stats),
        "sector_best_bucket": sector_best,
        "by_symbol": by_symbol,
        "edges": edges,
        "recommendation": asdict(rec),
        "best_entry_bucket": rec.best_entry_bucket,
        "worst_entry_bucket": rec.worst_entry_bucket,
    }
    save_tod_cache(summary)
    return summary


def format_tod_report(summary: dict[str, Any]) -> str:
    """Human-readable report for CLI / logs."""
    lines: list[str] = []
    lines.append("=" * 64)
    lines.append(f"TIME-OF-DAY ANALYSIS ({summary.get('days', '?')}d)")
    lines.append("=" * 64)
    lines.append(f"Symbols: {', '.join(summary.get('symbols') or [])}")
    lines.append("")
    lines.append("SPY forward-1h by bucket:")
    lines.append(f"{'bucket':<14} {'n':>5} {'win%':>7} {'mean':>10} {'sharpe':>8}")
    for b in TOD_BUCKETS:
        st = (summary.get("spy_buckets") or {}).get(b) or {}
        lines.append(
            f"{b:<14} {int(st.get('n', 0)):>5} {float(st.get('win_rate', 0)):>6.1%} "
            f"{float(st.get('mean_ret', 0)):>9.4%} {float(st.get('sharpe', 0)):>8.2f}"
        )
    lines.append("")
    lines.append("VTI forward-1h by bucket:")
    lines.append(f"{'bucket':<14} {'n':>5} {'win%':>7} {'mean':>10} {'sharpe':>8}")
    for b in TOD_BUCKETS:
        st = (summary.get("vti_buckets") or {}).get(b) or {}
        lines.append(
            f"{b:<14} {int(st.get('n', 0)):>5} {float(st.get('win_rate', 0)):>6.1%} "
            f"{float(st.get('mean_ret', 0)):>9.4%} {float(st.get('sharpe', 0)):>8.2f}"
        )
    lines.append("")
    hourly = summary.get("spy_hourly") or {}
    if hourly:
        lines.append("SPY by hour (ET):")
        lines.append(f"{'hour':<8} {'n':>5} {'win%':>7} {'mean':>10} {'sharpe':>8}")
        for h in sorted(hourly.keys(), key=lambda x: int(x)):
            st = hourly[h]
            lines.append(
                f"{int(h):02d}:00   {int(st.get('n', 0)):>5} {float(st.get('win_rate', 0)):>6.1%} "
                f"{float(st.get('mean_ret', 0)):>9.4%} {float(st.get('sharpe', 0)):>8.2f}"
            )
        lines.append("")
    entry_h = summary.get("entry_hourly") or {}
    if entry_h:
        lines.append("NYSE momentum entries by hour (research trades):")
        lines.append(f"{'hour':<8} {'n':>5} {'win%':>7} {'mean':>10} {'sharpe':>8}")
        for h in sorted(entry_h.keys(), key=lambda x: int(x)):
            st = entry_h[h]
            lines.append(
                f"{int(h):02d}:00   {int(st.get('n', 0)):>5} {float(st.get('win_rate', 0)):>6.1%} "
                f"{float(st.get('mean_ret', 0)):>9.4%} {float(st.get('sharpe', 0)):>8.2f}"
            )
        lines.append("")
    sa = summary.get("stat_arb_buckets") or {}
    lines.append("Stat Arb by bucket:")
    lines.append(f"{'bucket':<14} {'n':>5} {'win%':>7} {'mean':>10} {'sharpe':>8}")
    for b in TOD_BUCKETS:
        st = sa.get(b) or {}
        lines.append(
            f"{b:<14} {int(st.get('n', 0)):>5} {float(st.get('win_rate', 0)):>6.1%} "
            f"{float(st.get('mean_ret', 0)):>9.4%} {float(st.get('sharpe', 0)):>8.2f}"
        )
    lines.append("")
    sector = summary.get("sector_best_bucket") or {}
    if sector:
        lines.append("Best bucket by symbol:")
        for sym, b in sorted(sector.items()):
            lines.append(f"  {sym}: {b}")
        lines.append("")
    rec = summary.get("recommendation") or {}
    lines.append("RECOMMENDATIONS")
    lines.append("-" * 64)
    lines.append(f"Best entry:     {rec.get('best_entry_bucket')}")
    lines.append(f"Worst entry:    {rec.get('worst_entry_bucket')}")
    lines.append(f"Stat Arb best:  {rec.get('best_stat_arb_bucket')}")
    lines.append(f"Stat Arb worst: {rec.get('worst_stat_arb_bucket')}")
    lines.append(f"EOD close:      {rec.get('eod_close_action')}")
    lines.append(f"Morning reopen: {rec.get('morning_reopen_action')}")
    for note in rec.get("notes") or []:
        lines.append(f"  • {note}")
    lines.append("=" * 64)
    return "\n".join(lines)
