"""Research-only comparison of current 5m cache vs an E: archive prefix.

Reads the primary cache, the read-only E: source backup, and local PIT
membership/daily ATR data. The merged cache exists only in a temporary folder.
No source cache, database, bot setting, or order path is modified.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from modules.pit_universe import load_sp500_membership  # noqa: E402
from nyse_momentum_walkforward import (  # noqa: E402
    _member_on,
    load_intraday_panel,
    score_span,
    simulate_intraday,
    COOLDOWN_BASE,
    COOLDOWN_LONG,
    GAP_BASE,
    GAP_TIGHT,
    HOLDOUT_SESSIONS,
)

CURRENT_CACHE = ROOT / "data" / "intraday_cache"
ARCHIVE_CACHE = Path(r"E:\stockbot-source-20261011\stock-bot\data\intraday_cache")
REPORT = Path(__file__).with_name("usb_cache_extension_backtest_20261014_last.md")
VARIANTS = {
    "baseline": (COOLDOWN_BASE, GAP_BASE),
    "cooldown_1030": (COOLDOWN_LONG, GAP_BASE),
    "gap_1_5": (COOLDOWN_BASE, GAP_TIGHT),
}
EXPECTED_PROVENANCE = {"bar_interval": "5Min", "feed": "iex", "adjustment": "split"}


def _read_cache(path: Path) -> pd.DataFrame:
    df = pd.read_pickle(path)
    required = {"timestamp", "Open", "High", "Low", "Close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{path.name}: missing columns {sorted(missing)}")
    out = df.copy()
    ts = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
    if ts.isna().any():
        raise ValueError(f"{path.name}: {int(ts.isna().sum())} invalid timestamps")
    out["timestamp"] = ts
    out = out.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    for col in ("Open", "High", "Low", "Close"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if "Volume" in out:
        out["Volume"] = pd.to_numeric(out["Volume"], errors="coerce")
    return out.reset_index(drop=True)


def _provenance(symbols: list[str]) -> tuple[int, list[str]]:
    ok = []
    for symbol in symbols:
        try:
            meta = json.loads((CURRENT_CACHE / f"{symbol}.meta.json").read_text(encoding="utf-8"))
            if all(meta.get(k) == v for k, v in EXPECTED_PROVENANCE.items()):
                ok.append(symbol)
        except Exception:
            pass
    return len(ok), ok


def _merge_symbol(symbol: str) -> tuple[pd.DataFrame, dict]:
    current = _read_cache(CURRENT_CACHE / f"{symbol}.pkl")
    archived = _read_cache(ARCHIVE_CACHE / f"{symbol}.pkl")
    c_min = current["timestamp"].min()
    common = current.merge(
        archived,
        on="timestamp",
        how="inner",
        suffixes=("_c", "_e"),
        validate="one_to_one",
    )
    if common.empty:
        raise ValueError(f"{symbol}: no overlapping timestamps to estimate price scale")
    common_days = pd.Index(common["timestamp"].dt.tz_convert("America/New_York").dt.date.unique())
    fit_days = set(common_days[:10])
    fit = common[common["timestamp"].dt.tz_convert("America/New_York").dt.date.isin(fit_days)]
    ratios = []
    for field in ("Open", "High", "Low", "Close"):
        c = fit[f"{field}_c"].to_numpy(dtype=float)
        e = fit[f"{field}_e"].to_numpy(dtype=float)
        valid = np.isfinite(c) & np.isfinite(e) & (np.abs(e) > 1e-12)
        ratios.extend((c[valid] / e[valid]).tolist())
    ratios = np.asarray(ratios, dtype=float)
    if not len(ratios):
        raise ValueError(f"{symbol}: no valid overlapping OHLC prices")
    scale = float(np.median(ratios))
    deviation = np.abs(ratios / scale - 1.0) * 100.0
    p95 = float(np.quantile(deviation, 0.95))
    max_dev = float(np.max(deviation))
    prefix = archived[archived["timestamp"] < c_min].copy()
    # Conservative threshold: reject archive prefixes whose boundary prices
    # disagree by more than 3% after a robust scalar adjustment.
    accepted = bool(0.01 < scale < 100.0 and p95 <= 1.0 and max_dev <= 3.0)
    if accepted and len(prefix):
        for field in ("Open", "High", "Low", "Close"):
            prefix[field] *= scale
        if "Volume" in prefix.columns and scale > 0:
            prefix["Volume"] /= scale
    merged = pd.concat([prefix if accepted else prefix.iloc[0:0], current], ignore_index=True)
    merged = merged.sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(drop=True)
    prices = merged[["Open", "High", "Low", "Close"]]
    invalid = (
        prices.isna().any(axis=1)
        | (prices <= 0).any(axis=1)
        | (merged["High"] < prices.max(axis=1))
        | (merged["Low"] > prices.min(axis=1))
    )
    if int(invalid.sum()):
        raise ValueError(f"{symbol}: {int(invalid.sum())} invalid OHLC rows after merge")
    return merged, {
        "symbol": symbol,
        "current_start": c_min.isoformat(),
        "archive_start": archived["timestamp"].min().isoformat(),
        "archive_end": archived["timestamp"].max().isoformat(),
        "overlap_bars": int(len(common)),
        "scale_e_to_c": scale,
        "fit_p95_pct": p95,
        "fit_max_pct": max_dev,
        "prefix_bars_available": int(len(prefix)),
        "prefix_bars_used": int(len(prefix)) if accepted else 0,
        "prefix_accepted": accepted,
    }


def _span_indices(result: dict, begin_day: str, end_day: str) -> tuple[int, int]:
    sessions = result["sessions"]
    begin = sessions.index(begin_day)
    end = sessions.index(end_day) + 1
    return begin, end


def _run_variants(panel: dict, spans: dict) -> dict:
    return {
        name: simulate_intraday(panel, spans, cooldown_end=cooldown, gap_limit=gap)
        for name, (cooldown, gap) in VARIANTS.items()
    }


def _fmt(row: dict) -> str:
    return (
        f"{row['net_return_pct']:+.2f}% / {row['sharpe']:.2f} / "
        f"{row['max_dd_pct']:.2f}% / {row['trades']} / {row['turnover']:.2f}x / "
        f"${row['costs']:,.0f}"
    )


def _clip_membership_before(spans: dict, first_day) -> dict:
    """Keep PIT eligibility but prevent any position entering before scoring."""
    clipped = {}
    for symbol, intervals in spans.items():
        kept = []
        for start, end in intervals:
            new_start = max(start, first_day)
            if end is not None and end <= new_start:
                continue
            kept.append((new_start, end))
        if kept:
            clipped[symbol] = kept
    return clipped


def main() -> int:
    symbols = sorted({config.normalize_symbol(s) for s in config.get_nyse_universe_fixed()})
    missing = [s for s in symbols if not (CURRENT_CACHE / f"{s}.pkl").exists() or not (ARCHIVE_CACHE / f"{s}.pkl").exists()]
    if missing:
        raise SystemExit(f"Missing cache files for current fixed universe: {', '.join(missing)}")
    if not ARCHIVE_CACHE.is_dir():
        raise SystemExit(f"USB archive path is unavailable: {ARCHIVE_CACHE}")

    current_meta_count, current_meta_symbols = _provenance(symbols)
    original_cache = __import__("nyse_momentum_walkforward").CACHE_DIR
    module = sys.modules["nyse_momentum_walkforward"]
    module.CACHE_DIR = CURRENT_CACHE
    current_panel, current_meta = load_intraday_panel(require_ready=False, symbols=symbols)
    membership = load_sp500_membership()
    common_start_day = pd.Timestamp(current_meta["start"]).tz_convert("America/New_York").date()
    spans = _clip_membership_before(_member_on(membership), common_start_day)
    current_variants = _run_variants(current_panel, spans)

    symbol_checks = []
    with tempfile.TemporaryDirectory(prefix="usb_cache_extension_") as tmp:
        merged_dir = Path(tmp)
        for symbol in symbols:
            frame, check = _merge_symbol(symbol)
            symbol_checks.append(check)
            frame.to_pickle(merged_dir / f"{symbol}.pkl")
        module.CACHE_DIR = merged_dir
        extended_panel, extended_meta = load_intraday_panel(require_ready=False, symbols=symbols)
        extended_variants = _run_variants(extended_panel, spans)
    module.CACHE_DIR = original_cache

    common_start = current_meta["start"]
    common_end = current_meta["end"]
    common_start_day = pd.Timestamp(common_start).tz_convert("America/New_York").date().isoformat()
    common_end_day = pd.Timestamp(common_end).tz_convert("America/New_York").date().isoformat()
    holdout_days = min(HOLDOUT_SESSIONS, len(current_panel["sessions"]))
    holdout_start_day = str(current_panel["sessions"][-holdout_days])
    rows = []
    for name in VARIANTS:
        base = current_variants[name]
        ext = extended_variants[name]
        b0, b1 = _span_indices(base, common_start_day, common_end_day)
        e0, e1 = _span_indices(ext, common_start_day, common_end_day)
        bh0, bh1 = _span_indices(base, holdout_start_day, common_end_day)
        eh0, eh1 = _span_indices(ext, holdout_start_day, common_end_day)
        rows.append((name, score_span(base, b0, b1), score_span(ext, e0, e1), score_span(base, bh0, bh1), score_span(ext, eh0, eh1)))

    accepted = [r for r in symbol_checks if r["prefix_accepted"]]
    rejected = [r for r in symbol_checks if not r["prefix_accepted"]]
    total_prefix = sum(r["prefix_bars_used"] for r in symbol_checks)
    all_start = extended_meta["start"]
    all_start_day = pd.Timestamp(all_start).tz_convert("America/New_York").date()
    per_symbol_start = min(r["archive_start"] for r in symbol_checks)

    lines = [
        "# E: USB cache extension backtest — diagnostic",
        "",
        f"Run date: {pd.Timestamp.now(tz='America/Chicago').date()}",
        "",
        "## Data construction",
        "",
        f"- Compared the current fixed {len(symbols)}-name universe using the current C: cache against a backtest-only merge: accepted E: rows strictly before each C: cache start, followed by C: rows for the full overlapping and recent period.",
        f"- The current cache contains verified Alpaca 5-minute IEX split-adjusted metadata for {current_meta_count}/{len(symbols)} names. The E: per-file cache has no matching metadata sidecars, so the merged data is explicitly exploratory and does not pass the provenance gate.",
        f"- Scale factors were estimated from OHLC ratios in the first 10 shared sessions. NFLX's E: prefix was scaled by {next((r['scale_e_to_c'] for r in symbol_checks if r['symbol']=='NFLX'), float('nan')):.6g} to align the older split basis. OHLC was scaled and volume inversely scaled. Current C: bars always win on duplicate timestamps.",
        f"- Added {total_prefix:,} earlier 5-minute bars across {len(accepted)}/{len(symbols)} names. Earliest archive bar: {per_symbol_start}; merged panel begins {all_start_day} and ends {extended_meta['end']}.",
        f"- Prefixes rejected by the boundary consistency gate (95th-percentile OHLC discrepancy >1% or max >3%): {', '.join(r['symbol'] for r in rejected) or 'none'}.",
        "- Daily ATR, point-in-time S&P 500 membership, strategy rules, slippage, and the common evaluation dates were held constant. This reruns the current 10:15 cooldown/2% gap baseline plus the two single-change variants; it does not change bot settings or send orders.",
        "",
        "## Results on the same C: evaluation dates",
        "",
        "Entries are blocked before the common C: start date in both runs. The E: rows provide indicator warm-up only; they cannot create earlier trades or positions. Return and drawdown are measured over the same 504 sessions, with both books starting at $100,000. Costs use the same 5 bps per side.",
        "",
        "| Variant | C: return / Sharpe / max DD / trades / turnover / costs | E: prefix + C: return / Sharpe / max DD / trades / turnover / costs | C: last 6m | E: prefix + C: last 6m |",
        "|---|---|---|---|---|",
    ]
    for name, base, ext, base_h, ext_h in rows:
        lines.append(f"| {name} | {_fmt(base)} | {_fmt(ext)} | {_fmt(base_h)} | {_fmt(ext_h)} |")
    lines.extend([
        "",
        "## Interpretation",
        "",
        "This isolates the effect of using the older cache only to initialize indicators and stops before the common evaluation window. Treat any performance difference as diagnostic, not a candidate setting: the E: per-file provenance is missing, several archived bars differ slightly from refreshed bars, and the candidate universe still reflects current saved names rather than a survivorship-free historical universe. The walk-forward variants must still pass the existing promotion gate before consideration.",
        "",
        "No files on E: were written. The merged cache was temporary and deleted after the replay. No C: market cache, database, bot setting, journal, or order path was changed.",
        "",
        "## Per-symbol reconciliation",
        "",
        "| Symbol | E: prefix bars used / available | E→C scale | First-10-session p95 / max OHLC difference |",
        "|---|---:|---:|---:|",
    ])
    for r in symbol_checks:
        lines.append(f"| {r['symbol']} | {r['prefix_bars_used']:,} / {r['prefix_bars_available']:,} | {r['scale_e_to_c']:.7g} | {r['fit_p95_pct']:.3f}% / {r['fit_max_pct']:.3f}% |")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {REPORT}")
    for name, base, ext, base_h, ext_h in rows:
        print(name, "same-window C", _fmt(base), "E+C", _fmt(ext), "holdout C", _fmt(base_h), "E+C", _fmt(ext_h))
    print(f"Merged {total_prefix:,} prefix bars; accepted {len(accepted)}/{len(symbols)} symbols; rejected {', '.join(r['symbol'] for r in rejected) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
