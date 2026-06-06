"""One-at-a-time flag additions on current_dynamic baseline (Sharpe tuning).

Run from repo root:
  python scripts/analysis/sharpe_flag_grid.py
  python scripts/analysis/sharpe_flag_grid.py --days 500 1000
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from backtester import MIN_HISTORY, _ensure_daily_data, run_backtest
from modules.wayback_sentiment import load_monthly_web_sentiment

OUT_CSV = Path(__file__).with_name("sharpe_flag_grid.csv")
OUT_MD = Path(__file__).with_name("sharpe_flag_grid_results.md")

CONFIG_KEYS = (
    "GAME_PLAN_ENABLED",
    "GAME_PLAN_YIELD_GATE_ONLY",
    "YIELD_GATE_ENABLED",
    "NYSE_OVERLAP_FILTER_ENABLED",
    "NYSE_SPY_CORR_MAX",
    "NYSE_BETA_SCALING_ENABLED",
    "ADAPTIVE_CHUNK_ENABLED",
    "COFIRE_BUDGET_ENABLED",
    "SPY_EXIT_ON_MA_BREAK",
    "HALT_RESUME_DRAWDOWN_PCT",
    "HALT_LIQUIDATE_ON_BREACH",
    "DERIVED_BEAR_PAUSE_ENABLED",
    "SENTIMENT_GAP_THRESHOLD_AGGRESSIVE",
    "SENTIMENT_GAP_THRESHOLD_NORMAL",
    "SENTIMENT_GAP_THRESHOLD_DEFENSIVE",
)

_BASE = {
    "GAME_PLAN_ENABLED": True,
    "GAME_PLAN_YIELD_GATE_ONLY": True,
    "YIELD_GATE_ENABLED": True,
    "NYSE_OVERLAP_FILTER_ENABLED": False,
    "NYSE_BETA_SCALING_ENABLED": False,
    "ADAPTIVE_CHUNK_ENABLED": False,
    "COFIRE_BUDGET_ENABLED": False,
    "SPY_EXIT_ON_MA_BREAK": False,
    "HALT_RESUME_DRAWDOWN_PCT": 0.08,
    "HALT_LIQUIDATE_ON_BREACH": True,
    "DERIVED_BEAR_PAUSE_ENABLED": False,
    "SENTIMENT_GAP_THRESHOLD_AGGRESSIVE": 0.25,
    "SENTIMENT_GAP_THRESHOLD_NORMAL": 0.35,
    "SENTIMENT_GAP_THRESHOLD_DEFENSIVE": 0.40,
}


@dataclass
class Variant:
    name: str
    label: str
    flags: dict = field(default_factory=dict)


def _v(name: str, label: str, **overrides) -> Variant:
    flags = dict(_BASE)
    flags.update(overrides)
    if "NYSE_OVERLAP_FILTER_ENABLED" in overrides:
        flags["NYSE_SPY_CORR_MAX"] = 0.80
    return Variant(name, label, flags)


VARIANTS = [
    _v("baseline", "current_dynamic (all opts off)"),
    _v("plus_overlap", "+NYSE overlap only", NYSE_OVERLAP_FILTER_ENABLED=True),
    _v("plus_spy_exit", "+SPY MA exit only", SPY_EXIT_ON_MA_BREAK=True),
    _v(
        "plus_adaptive_cofire",
        "+adaptive chunk + co-fire only",
        ADAPTIVE_CHUNK_ENABLED=True,
        COFIRE_BUDGET_ENABLED=True,
    ),
    _v("plus_beta", "+NYSE beta scaling only", NYSE_BETA_SCALING_ENABLED=True),
    _v(
        "combo_overlap_spy",
        "+overlap + SPY MA exit",
        NYSE_OVERLAP_FILTER_ENABLED=True,
        SPY_EXIT_ON_MA_BREAK=True,
    ),
    _v(
        "combo_spy_adaptive",
        "+SPY MA exit + adaptive/cofire",
        SPY_EXIT_ON_MA_BREAK=True,
        ADAPTIVE_CHUNK_ENABLED=True,
        COFIRE_BUDGET_ENABLED=True,
    ),
]


@contextlib.contextmanager
def _patch_variant(v: Variant):
    saved = {k: getattr(config, k) for k in CONFIG_KEYS}
    for key, val in v.flags.items():
        setattr(config, key, val)
    if "NYSE_OVERLAP_FILTER_ENABLED" in v.flags:
        config.NYSE_ANTI_OVERLAP_ENABLED = v.flags["NYSE_OVERLAP_FILTER_ENABLED"]
    try:
        yield
    finally:
        for key, val in saved.items():
            setattr(config, key, val)
        config.NYSE_ANTI_OVERLAP_ENABLED = saved["NYSE_OVERLAP_FILTER_ENABLED"]


def _slice_window(data, days: int):
    need = days + MIN_HISTORY
    if len(data) <= need:
        return data
    return data.iloc[-need:]


def run_variant(data, variant: Variant, window: str, monthly_web) -> dict:
    with _patch_variant(variant):
        row = run_backtest(
            data,
            track_spy_fill=False,
            verbose=False,
            wisdom_mode="dynamic",
            monthly_web=monthly_web,
            track_metrics=True,
        )
    row["variant"] = variant.name
    row["variant_label"] = variant.label
    row["window"] = window
    return row


def write_report(all_rows: list[dict]) -> None:
    windows = sorted({r["window"] for r in all_rows}, key=lambda x: int(x.replace("d", "")))
    baseline = {r["window"]: r["sharpe"] for r in all_rows if r["variant"] == "baseline"}

    lines = [
        "# Sharpe Flag Grid (current_dynamic + one-at-a-time)",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
    ]
    for w in windows:
        lines.append(f"## {w}")
        lines.append("")
        lines.append("| Variant | Sharpe | Δ vs baseline | Return | Max DD |")
        lines.append("|---------|-------:|--------------:|-------:|-------:|")
        b_sh = baseline.get(w, 0)
        for r in sorted(
            [x for x in all_rows if x["window"] == w],
            key=lambda x: -x["sharpe"],
        ):
            d = r["sharpe"] - b_sh
            lines.append(
                f"| {r['variant']} | {r['sharpe']:.2f} | {d:+.2f} | "
                f"{r['total_return_pct']:+.2f}% | {r['max_drawdown_pct']:.2f}% |"
            )
        lines.append("")

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    keys = sorted({k for r in all_rows for k in r if k not in ("regime_counts", "spy_fill")})
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in all_rows:
            w.writerow(r)
    print(f"Wrote {OUT_MD}")
    print(f"Wrote {OUT_CSV}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sharpe flag grid on current_dynamic")
    parser.add_argument("--days", type=int, nargs="+", default=[500, 1000])
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    monthly_web = load_monthly_web_sentiment()
    max_days = max(args.days)
    fetch_days = max_days + MIN_HISTORY
    print(f"Loading daily data (need {max_days}d + {MIN_HISTORY} warmup) ...", flush=True)
    data_full = _ensure_daily_data(fetch_days, refresh=args.refresh, use_max=False)
    if len(data_full) < MIN_HISTORY + 10:
        print("Insufficient data; run: python fetch_data.py --daily --days 2000")
        sys.exit(1)

    all_rows: list[dict] = []
    for d in args.days:
        label = f"{d}d"
        slice_data = _slice_window(data_full, d)
        print(f"--- Window {label} ({len(slice_data)} bars) ---", flush=True)
        for v in VARIANTS:
            print(f"  {v.name} ...", flush=True)
            all_rows.append(run_variant(slice_data, v, label, monthly_web))

    write_report(all_rows)

    print("\n=== SUMMARY (Sharpe by variant) ===")
    for w in args.days:
        label = f"{w}d"
        print(f"\n{label}:")
        for r in sorted(
            [x for x in all_rows if x["window"] == label],
            key=lambda x: -x["sharpe"],
        ):
            print(f"  {r['variant']:22} Sharpe {r['sharpe']:5.2f}  return {r['total_return_pct']:+7.2f}%")


if __name__ == "__main__":
    main()
