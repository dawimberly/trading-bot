"""Sharpe optimization phase: Old vs current dynamic vs new optimized stack.

Run from repo root:
  python scripts/analysis/sharpe_phase_compare.py
  python scripts/analysis/sharpe_phase_compare.py --days 500 1000 2000
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

OUT_MD = Path(__file__).with_name("sharpe_phase_results.md")
OUT_CSV = Path(__file__).with_name("sharpe_phase_results.csv")

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
    "AUTO_DYNAMIC_ENABLED",
    "SENTIMENT_GAP_THRESHOLD_AGGRESSIVE",
    "SENTIMENT_GAP_THRESHOLD_NORMAL",
    "SENTIMENT_GAP_THRESHOLD_DEFENSIVE",
)


@dataclass
class Variant:
    name: str
    label: str
    wisdom_mode: str | None
    flags: dict = field(default_factory=dict)


# Old: pre-optimization stack (OPTIMIZED_SYSTEM_SUMMARY pre-session assumptions).
# Metal/stress-cash sleeves are not simulated in backtester.py — 0.9 long scale only.
OLD = Variant(
    "old",
    "Pre-recent stack (governor, full game plan, no overlap/sizing exits)",
    wisdom_mode="governor",
    flags={
        "GAME_PLAN_ENABLED": True,
        "GAME_PLAN_YIELD_GATE_ONLY": False,
        "YIELD_GATE_ENABLED": True,
        "NYSE_OVERLAP_FILTER_ENABLED": False,
        "NYSE_BETA_SCALING_ENABLED": False,
        "ADAPTIVE_CHUNK_ENABLED": False,
        "COFIRE_BUDGET_ENABLED": False,
        "SPY_EXIT_ON_MA_BREAK": False,
        "HALT_RESUME_DRAWDOWN_PCT": 0.0,
        "HALT_LIQUIDATE_ON_BREACH": False,
        "DERIVED_BEAR_PAUSE_ENABLED": False,
    },
)

# Current dynamic: wisdom dynamic + yield-gate-only + halt resume; key opts partial/off.
CURRENT_DYNAMIC = Variant(
    "current_dynamic",
    "WISDOM_MODE=dynamic, yield-gate-only, halt resume; optimizations partial",
    wisdom_mode="dynamic",
    flags={
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
    },
)

# New optimized: dynamic + full recommended stack (all four key optimizations + beta).
NEW_OPTIMIZED = Variant(
    "new_optimized",
    "Dynamic + yield-gate + overlap + adaptive/cofire + SPY MA exit + NYSE beta",
    wisdom_mode="dynamic",
    flags={
        "GAME_PLAN_ENABLED": True,
        "GAME_PLAN_YIELD_GATE_ONLY": True,
        "YIELD_GATE_ENABLED": True,
        "NYSE_OVERLAP_FILTER_ENABLED": True,
        "NYSE_SPY_CORR_MAX": 0.80,
        "NYSE_BETA_SCALING_ENABLED": True,
        "ADAPTIVE_CHUNK_ENABLED": True,
        "COFIRE_BUDGET_ENABLED": True,
        "SPY_EXIT_ON_MA_BREAK": True,
        "HALT_RESUME_DRAWDOWN_PCT": 0.08,
        "HALT_LIQUIDATE_ON_BREACH": True,
        "DERIVED_BEAR_PAUSE_ENABLED": False,
        "SENTIMENT_GAP_THRESHOLD_AGGRESSIVE": 0.25,
        "SENTIMENT_GAP_THRESHOLD_NORMAL": 0.35,
        "SENTIMENT_GAP_THRESHOLD_DEFENSIVE": 0.40,
    },
)

VARIANTS = (OLD, CURRENT_DYNAMIC, NEW_OPTIMIZED)


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


def run_variant(
    data,
    variant: Variant,
    window: str,
    monthly_web,
) -> dict:
    with _patch_variant(variant):
        row = run_backtest(
            data,
            track_spy_fill=False,
            verbose=False,
            wisdom_mode=variant.wisdom_mode,
            monthly_web=monthly_web,
            track_metrics=True,
        )
    row["variant"] = variant.name
    row["variant_label"] = variant.label
    row["wisdom_mode"] = variant.wisdom_mode or "price_only"
    row["window"] = window
    return row


def _fmt_table(rows: list[dict]) -> str:
    lines = [
        "| Variant | Return | Sharpe | Sortino | Max DD | Calmar | Avg Exp% | Co-fire% | Crypto% |",
        "|---------|-------:|-------:|--------:|-------:|-------:|---------:|---------:|--------:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['variant']} "
            f"| {r['total_return_pct']:+.2f}% "
            f"| {r['sharpe']:.2f} "
            f"| {r.get('sortino', 0):.2f} "
            f"| {r['max_drawdown_pct']:.2f}% "
            f"| {r.get('calmar', 0):.2f} "
            f"| {r.get('avg_exposure_pct', 0):.1f} "
            f"| {r.get('cofire_pct', 0):.1f} "
            f"| {r.get('crypto_contribution_pct', 0):+.2f} |"
        )
    return "\n".join(lines)


def _delta_sharpe(rows: list[dict], a: str, b: str) -> float:
    ra = next(r for r in rows if r["variant"] == a)
    rb = next(r for r in rows if r["variant"] == b)
    return rb["sharpe"] - ra["sharpe"]


def _top_changes(all_rows: list[dict]) -> list[tuple[str, float, str]]:
    """Rank single-step Sharpe deltas old->current and current->optimized per window."""
    changes: dict[str, list[float]] = {}
    evidence: dict[str, list[str]] = {}
    windows = sorted({r["window"] for r in all_rows})

    steps = [
        ("old", "current_dynamic", "Dynamic wisdom + yield-gate-only + halt resume/liquidate"),
        ("current_dynamic", "new_optimized", "NYSE overlap + adaptive/cofire + SPY MA exit + beta"),
    ]
    for window in windows:
        wr = [r for r in all_rows if r["window"] == window]
        for src, dst, desc in steps:
            try:
                d = _delta_sharpe(wr, src, dst)
            except StopIteration:
                continue
            changes.setdefault(desc, []).append(d)
            src_r = next(r for r in wr if r["variant"] == src)
            dst_r = next(r for r in wr if r["variant"] == dst)
            evidence.setdefault(desc, []).append(
                f"{window}: Sharpe {src_r['sharpe']:.2f}->{dst_r['sharpe']:.2f} "
                f"({d:+.2f}), return {dst_r['total_return_pct'] - src_r['total_return_pct']:+.2f} pp"
            )

    ranked = sorted(
        ((desc, sum(v) / len(v), evidence[desc]) for desc, v in changes.items()),
        key=lambda x: -x[1],
    )
    return ranked


def write_report(all_rows: list[dict]) -> None:
    sections = [
        "# Sharpe Phase Comparison",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## Variants",
        "",
        "| Key | Old | Current dynamic | New optimized |",
        "|-----|-----|-----------------|---------------|",
        "| Wisdom | governor | dynamic | dynamic |",
        "| Game plan | full (0.9 scale) | yield-gate-only | yield-gate-only |",
        "| NYSE overlap | off | off | on (corr 0.80) |",
        "| Adaptive + co-fire | off | off | on |",
        "| SPY MA exit | off | off | on |",
        "| NYSE beta scaling | off | off | on |",
        "| Halt resume / liquidate | off | 8% / on | 8% / on |",
        "",
        "**Assumption:** integrated `backtester.py` does not deploy metal basket or stress-cash "
        "trims; full game plan effect is mainly the 0.9 long-cap scale.",
        "",
    ]

    target_hits = []
    windows = sorted({r["window"] for r in all_rows}, key=lambda x: int(x.replace("d", "")))
    for w in windows:
        sections.append(f"## Window {w}")
        sections.append("")
        sections.append(_fmt_table([r for r in all_rows if r["window"] == w]))
        sections.append("")
        for r in all_rows:
            if r["window"] == w and 1.2 <= r["sharpe"] <= 1.5:
                target_hits.append(f"{r['variant']} @ {w}: Sharpe {r['sharpe']:.2f}")

    sections.append("## Target Sharpe 1.2–1.5")
    sections.append("")
    if target_hits:
        sections.extend(f"- {h}" for h in target_hits)
    else:
        best = max(all_rows, key=lambda r: r["sharpe"])
        sections.append(
            f"No window hit 1.2–1.5. Best: **{best['variant']}** @ **{best['window']}** "
            f"Sharpe **{best['sharpe']:.2f}** (return {best['total_return_pct']:+.2f}%)."
        )

    sections.append("")
    sections.append("## Top Sharpe improvements (A/B deltas)")
    sections.append("")
    for i, (desc, avg_delta, ev) in enumerate(_top_changes(all_rows)[:3], 1):
        sections.append(f"{i}. **{desc}** — avg Sharpe Δ **{avg_delta:+.2f}** across windows")
        for line in ev:
            sections.append(f"   - {line}")
        sections.append("")

    sections.append("## Reproduce")
    sections.append("")
    sections.append("```bash")
    sections.append("python scripts/analysis/sharpe_phase_compare.py --days 500 1000 2000")
    sections.append("```")

    OUT_MD.write_text("\n".join(sections), encoding="utf-8")

    keys = sorted({k for r in all_rows for k in r if k not in ("regime_counts", "spy_fill")})
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in all_rows:
            w.writerow(r)

    print(f"Wrote {OUT_MD}")
    print(f"Wrote {OUT_CSV}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sharpe phase: old vs dynamic vs optimized")
    parser.add_argument(
        "--days",
        type=int,
        nargs="+",
        default=[500, 1000, 2000],
        help="Backtest windows (default: 500 1000 2000)",
    )
    parser.add_argument("--refresh", action="store_true", help="Refresh daily data from API")
    args = parser.parse_args()

    monthly_web = load_monthly_web_sentiment()
    if monthly_web.empty:
        print("Warning: wayback_sentiment.csv missing — dynamic/governor use price-only fallback.")

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

    print("\n=== SUMMARY ===")
    for w in args.days:
        label = f"{w}d"
        print(f"\n{label}:")
        for r in [x for x in all_rows if x["window"] == label]:
            print(
                f"  {r['variant']:16} return {r['total_return_pct']:+7.2f}%  "
                f"Sharpe {r['sharpe']:5.2f}  Sortino {r.get('sortino', 0):5.2f}  "
                f"MaxDD {r['max_drawdown_pct']:6.2f}%  cofire {r.get('cofire_pct', 0):4.1f}%"
            )


if __name__ == "__main__":
    main()
