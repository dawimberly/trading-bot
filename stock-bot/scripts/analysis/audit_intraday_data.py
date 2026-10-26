"""Audit the local five-minute OHLC cache before intraday research.

This is a data-only check: it never calculates strategy returns, refreshes the
cache, writes to the trading database, or sends orders.

Usage (from stock-bot/):
  python scripts/analysis/audit_intraday_data.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import config  # noqa: E402

CACHE_DIR = ROOT / "data" / "intraday_cache"
REQUIRED = ("timestamp", "Open", "High", "Low", "Close")
EXPECTED_BARS_PER_SESSION = 78


def _read_one(path: Path) -> tuple[pd.DataFrame | None, list[str]]:
    issues: list[str] = []
    try:
        raw = pd.read_pickle(path)
    except Exception as exc:
        return None, [f"unreadable pickle: {type(exc).__name__}"]
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        return None, ["not a non-empty DataFrame"]
    missing = [column for column in REQUIRED if column not in raw.columns]
    if missing:
        return None, [f"missing columns: {', '.join(missing)}"]

    out = raw.loc[:, list(REQUIRED)].copy()
    if "Volume" in raw.columns:
        out["Volume"] = pd.to_numeric(raw["Volume"], errors="coerce")
    ts = pd.to_datetime(out["timestamp"], errors="coerce")
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize("America/New_York", ambiguous="NaT", nonexistent="NaT")
    else:
        ts = ts.dt.tz_convert("America/New_York")
    out["timestamp"] = ts
    invalid_ts = int(ts.isna().sum())
    if invalid_ts:
        issues.append(f"{invalid_ts} invalid timestamps")
    out = out.dropna(subset=["timestamp"])
    local_time = out["timestamp"].dt.time
    out = out[(local_time >= pd.Timestamp("09:30").time()) & (local_time < pd.Timestamp("16:00").time())]
    duplicates = int(out["timestamp"].duplicated(keep="last").sum())
    if duplicates:
        issues.append(f"{duplicates} duplicate timestamps")
    out = out.sort_values("timestamp").drop_duplicates("timestamp", keep="last")

    for column in ("Open", "High", "Low", "Close"):
        out[column] = pd.to_numeric(out[column], errors="coerce")
    invalid_ohlc = out[list(REQUIRED[1:])].isna().any(axis=1)
    invalid_ohlc |= (out[["Open", "High", "Low", "Close"]] <= 0).any(axis=1)
    invalid_ohlc |= out["High"] < out[["Open", "Low", "Close"]].max(axis=1)
    invalid_ohlc |= out["Low"] > out[["Open", "High", "Close"]].min(axis=1)
    n_bad = int(invalid_ohlc.sum())
    if n_bad:
        issues.append(f"{n_bad} invalid OHLC rows")
        out = out.loc[~invalid_ohlc]
    return (out if len(out) else None), issues


def audit(
    *,
    as_of: pd.Timestamp,
    min_universe_coverage: float,
    min_sessions: int,
    min_coverage: float,
    max_stale_sessions: int,
) -> dict:
    paths = sorted(CACHE_DIR.glob("*.pkl"))
    target_universe = {
        config.normalize_symbol(symbol)
        for symbol in config.get_nyse_universe()
        if config.normalize_symbol(symbol)
    }
    frames: dict[str, pd.DataFrame] = {}
    file_issues: dict[str, list[str]] = {}
    volume_symbols: set[str] = set()
    for path in paths:
        frame, issues = _read_one(path)
        if issues:
            file_issues[path.stem.upper()] = issues
        if frame is not None:
            frames[path.stem.upper()] = frame
            if "Volume" in frame.columns:
                volume_symbols.add(path.stem.upper())

    if not frames:
        return {
            "ready": False,
            "blockers": [f"No valid OHLC cache files found in {CACHE_DIR}"],
            "cache_dir": str(CACHE_DIR),
            "files_found": len(paths),
            "symbols": 0,
            "sessions": 0,
        }

    cached_symbols = set(frames)
    target_cached = target_universe & cached_symbols
    target_coverage = len(target_cached) / len(target_universe) if target_universe else 0.0
    target_dates = sorted(
        set().union(*(set(frames[symbol]["timestamp"].dt.date) for symbol in target_cached))
        if target_cached
        else set()
    )
    target_start = min(
        (frames[symbol]["timestamp"].min() for symbol in target_cached),
        default=pd.NaT,
    )
    target_end = max(
        (frames[symbol]["timestamp"].max() for symbol in target_cached),
        default=pd.NaT,
    )
    expected_provenance = {"bar_interval": "5Min", "feed": "iex", "adjustment": "split"}
    provenance_ok: set[str] = set()
    provenance_issues: dict[str, str] = {}
    for symbol in target_universe:
        meta_path = CACHE_DIR / f"{symbol}.meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if all(meta.get(key) == value for key, value in expected_provenance.items()):
                provenance_ok.add(symbol)
            else:
                provenance_issues[symbol] = "feed, adjustment, or bar interval does not match the pinned research source"
        except Exception:
            provenance_issues[symbol] = "missing or unreadable source metadata"
    provenance_coverage = len(provenance_ok) / len(target_universe) if target_universe else 0.0

    session_sets = [set(frame["timestamp"].dt.date) for frame in frames.values()]
    sessions = sorted(set.union(*session_sets))
    session_index = pd.Index(sessions)
    panel_end = max(frame["timestamp"].max() for frame in frames.values())
    session_counts: dict[str, int] = {}
    session_bar_counts: dict[str, pd.Series] = {}
    symbol_start_days: dict[str, object] = {}
    symbol_end_days: dict[str, object] = {}
    symbol_ranges: dict[str, dict] = {}
    per_symbol_coverage: list[float] = []
    per_session_rows: dict = {}
    for symbol, frame in frames.items():
        counts = frame.groupby(frame["timestamp"].dt.date).size()
        session_counts[symbol] = int(len(counts))
        session_bar_counts[symbol] = counts
        symbol_start_days[symbol] = counts.index.min()
        symbol_end_days[symbol] = counts.index.max()
        per_symbol_coverage.append(float(counts.reindex(session_index, fill_value=0).mean() / EXPECTED_BARS_PER_SESSION))
        symbol_ranges[symbol] = {
            "start": frame["timestamp"].min().isoformat(),
            "end": frame["timestamp"].max().isoformat(),
            "sessions": int(len(counts)),
            "median_bars_per_session": round(float(counts.median()), 1),
            "coverage_fraction": round(per_symbol_coverage[-1], 4),
            "max_sessions_per_day": int(counts.max()),
        }
        for day, count in counts.items():
            per_session_rows.setdefault(day, []).append(int(count))

    target_daily_coverage = []
    target_total_seen = 0
    target_total_expected = 0
    for day in target_dates:
        active = [
            symbol
            for symbol in target_cached
            if symbol_start_days[symbol] <= day <= symbol_end_days[symbol]
        ]
        seen = sum(
            int(session_bar_counts[symbol].get(day, 0))
            for symbol in active
        )
        expected = len(active) * EXPECTED_BARS_PER_SESSION
        if expected:
            target_daily_coverage.append(seen / expected)
            target_total_seen += seen
            target_total_expected += expected

    panel_end_day = panel_end.tz_convert("America/New_York").date()
    as_of_local = as_of.tz_convert("America/New_York")
    # Business-day distance is intentionally conservative around market holidays.
    stale_sessions = max(0, len(pd.bdate_range(panel_end_day, as_of_local.date())) - 1)
    all_counts = [count for values in per_session_rows.values() for count in values]
    aggregate_coverage = float(sum(all_counts) / (len(frames) * len(sessions) * EXPECTED_BARS_PER_SESSION))
    daily_median_coverage = float(
        np.median(
            [sum(values) / (len(frames) * EXPECTED_BARS_PER_SESSION) for values in per_session_rows.values()]
        )
    )
    target_median_coverage = (
        float(np.median(target_daily_coverage)) if target_daily_coverage else 0.0
    )
    target_aggregate_coverage = (
        target_total_seen / target_total_expected if target_total_expected else 0.0
    )

    blockers = []
    if target_universe and target_coverage < min_universe_coverage:
        missing = sorted(target_universe - target_cached)
        blockers.append(
            f"Only {target_coverage:.1%} of configured bot-universe names have bars; "
            f"need {min_universe_coverage:.1%}; missing: {', '.join(missing[:12])}"
        )
    if target_universe and provenance_coverage < min_universe_coverage:
        blockers.append(
            f"Only {provenance_coverage:.1%} of configured bot-universe caches have verified "
            "5-minute IEX, split-adjusted provenance; refresh them with the cache-only data command"
        )
    if len(sessions) < min_sessions:
        blockers.append(f"Only {len(sessions)} sessions; need at least {min_sessions}")
    if target_median_coverage < min_coverage:
        blockers.append(
            f"Configured bot-universe median bar coverage is {target_median_coverage:.1%}; "
            f"need at least {min_coverage:.1%}"
        )
    if stale_sessions > max_stale_sessions:
        blockers.append(
            f"Latest bar is {stale_sessions} business days behind as-of; maximum allowed is {max_stale_sessions}"
        )
    bad_ohlc_symbols = sum(any("invalid OHLC" in issue for issue in issues) for issues in file_issues.values())
    if bad_ohlc_symbols:
        blockers.append(f"{bad_ohlc_symbols} cache files contain invalid OHLC rows; inspect file_issues")

    return {
        "ready": not blockers,
        "blockers": blockers,
        "as_of": as_of_local.isoformat(),
        "cache_dir": str(CACHE_DIR),
        "symbols": len(frames),
        "configured_bot_universe_symbols": len(target_universe),
        "configured_bot_universe_covered": len(target_cached),
        "configured_bot_universe_coverage": round(target_coverage, 4),
        "configured_bot_universe_provenance_coverage": round(provenance_coverage, 4),
        "configured_bot_universe_sessions": len(target_dates),
        "configured_bot_universe_start": target_start.isoformat() if pd.notna(target_start) else None,
        "configured_bot_universe_end": target_end.isoformat() if pd.notna(target_end) else None,
        "provenance_issues": provenance_issues,
        "readiness_scope": "configured bot universe; this does not establish a survivorship-free broad-market universe",
        "files_found": len(paths),
        "sessions": len(sessions),
        "start": str(sessions[0]),
        "end": str(panel_end),
        "stale_business_days": stale_sessions,
        "expected_bars_per_session": EXPECTED_BARS_PER_SESSION,
        "median_cross_sectional_coverage": round(target_median_coverage, 4),
        "aggregate_coverage": round(target_aggregate_coverage, 4),
        "all_cached_symbols_median_coverage": round(daily_median_coverage, 4),
        "all_cached_symbols_aggregate_coverage": round(aggregate_coverage, 4),
        "symbols_with_volume": len(volume_symbols),
        "volume_symbol_fraction": round(len(volume_symbols) / len(frames), 4),
        "median_sessions_per_symbol": round(float(np.median(list(session_counts.values()))), 1),
        "lowest_coverage_symbols": sorted(
            ((symbol, row["coverage_fraction"]) for symbol, row in symbol_ranges.items()),
            key=lambda item: item[1],
        )[:15],
        "file_issues": file_issues,
        "provenance": {
            "bar_interval": "5-minute, Alpaca IEX; verified per configured ticker through sidecar metadata",
            "price_adjustment": "split-adjusted; verified per configured ticker through sidecar metadata",
            "feed": "IEX; verified per configured ticker through sidecar metadata",
            "point_in_time_universe": "not established by these cache files",
            "volume": (
                f"present in {len(volume_symbols)} of {len(frames)} cache files and retained by the "
                "panel loader; the strategy replay still uses flat slippage, so volume does not "
                "yet calibrate liquidity or market impact"
            ),
        },
        "gates": {
            "min_configured_universe_coverage": min_universe_coverage,
            "min_sessions": min_sessions,
            "min_median_cross_sectional_coverage": min_coverage,
            "max_stale_business_days": max_stale_sessions,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", help="ISO timestamp for reproducible freshness checks; defaults to now")
    parser.add_argument("--min-universe-coverage", type=float, default=0.95)
    parser.add_argument("--min-sessions", type=int, default=252)
    parser.add_argument("--min-coverage", type=float, default=0.80)
    parser.add_argument(
        "--max-stale-sessions",
        type=int,
        default=5,
        help="Known minute-bar history lag is about five trading days on this paper data plan",
    )
    parser.add_argument("--json", type=Path, help="Optional path to save the audit JSON")
    args = parser.parse_args()
    as_of = pd.Timestamp(args.as_of) if args.as_of else pd.Timestamp.now(tz="America/New_York")
    if as_of.tzinfo is None:
        as_of = as_of.tz_localize("America/New_York")
    else:
        as_of = as_of.tz_convert("America/New_York")
    report = audit(
        as_of=as_of,
        min_universe_coverage=args.min_universe_coverage,
        min_sessions=args.min_sessions,
        min_coverage=args.min_coverage,
        max_stale_sessions=args.max_stale_sessions,
    )
    rendered = json.dumps(report, indent=2)
    print(rendered)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report.get("ready") else 2


if __name__ == "__main__":
    raise SystemExit(main())
