"""Final comparison: old baseline vs intermediate vs current recommended stack.

Run from repo root:
  python scripts/analysis/final_recommended_stack_comparison.py
  python scripts/analysis/final_recommended_stack_comparison.py --refresh
"""

from __future__ import annotations

import argparse
import contextlib
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from backtester import MIN_HISTORY, _ensure_daily_data, run_backtest
from modules.wayback_sentiment import load_monthly_web_sentiment

OUT_MD = Path(__file__).with_name("final_recommended_stack_comparison.md")

CONFIG_KEYS = (
    "GAME_PLAN_ENABLED",
    "GAME_PLAN_YIELD_GATE_ONLY",
    "YIELD_GATE_ENABLED",
    "NYSE_OVERLAP_FILTER_ENABLED",
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

WINDOWS = (
    ("365d", 365, False),
    ("1000d", 1000, False),
    ("max", 0, True),
)


@dataclass
class StackVersion:
    name: str
    label: str
    wisdom_mode: str | None
    vti_core_pct: float
    flags: dict = field(default_factory=dict)
    strengths: str = ""
    weaknesses: str = ""


OLD_BASELINE = StackVersion(
    "old_baseline",
    "Old baseline (pre-optimization)",
    wisdom_mode="governor",
    vti_core_pct=0.0,
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
    strengths="Simple discrete wisdom; full game-plan config (0.9 long scale in live).",
    weaknesses="Governor mode; no VTI ballast; one-way halt; metal/stress not in daily sim.",
)

INTERMEDIATE = StackVersion(
    "intermediate",
    "Intermediate (dynamic + yield-gate + 80/20 VTI)",
    wisdom_mode="dynamic",
    vti_core_pct=0.80,
    flags={
        "GAME_PLAN_ENABLED": True,
        "GAME_PLAN_YIELD_GATE_ONLY": True,
        "YIELD_GATE_ENABLED": True,
        "NYSE_OVERLAP_FILTER_ENABLED": False,
        "NYSE_BETA_SCALING_ENABLED": False,
        "ADAPTIVE_CHUNK_ENABLED": False,
        "COFIRE_BUDGET_ENABLED": False,
        "SPY_EXIT_ON_MA_BREAK": False,
        "HALT_RESUME_DRAWDOWN_PCT": 0.0,
        "HALT_LIQUIDATE_ON_BREACH": False,
        "DERIVED_BEAR_PAUSE_ENABLED": False,
        "SENTIMENT_GAP_THRESHOLD_AGGRESSIVE": 0.25,
        "SENTIMENT_GAP_THRESHOLD_NORMAL": 0.35,
        "SENTIMENT_GAP_THRESHOLD_DEFENSIVE": 0.40,
    },
    strengths="Dynamic sizing; yield-gate-only; passive VTI core — major Sharpe uplift vs old.",
    weaknesses="Legacy halt (no resume); small active sleeve limits overlap/sizing tweaks.",
)

CURRENT = StackVersion(
    "current_recommended",
    "Current recommended (README stack)",
    wisdom_mode="dynamic",
    vti_core_pct=0.80,
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
    strengths="Same as intermediate + halt resume 8% and breach liquidation; live default.",
    weaknesses="Optional flags (overlap, adaptive, SPY exit) tested — not worth default on.",
)

VERSIONS = (OLD_BASELINE, INTERMEDIATE, CURRENT)


@contextlib.contextmanager
def _patch_version(v: StackVersion):
    saved = {k: getattr(config, k) for k in CONFIG_KEYS}
    saved_social = config.SOCIAL_SLEEVE_ENABLED
    for key, val in v.flags.items():
        setattr(config, key, val)
    config.NYSE_ANTI_OVERLAP_ENABLED = v.flags.get(
        "NYSE_OVERLAP_FILTER_ENABLED", config.NYSE_OVERLAP_FILTER_ENABLED
    )
    config.SOCIAL_SLEEVE_ENABLED = False
    try:
        yield
    finally:
        for key, val in saved.items():
            setattr(config, key, val)
        config.NYSE_ANTI_OVERLAP_ENABLED = saved["NYSE_OVERLAP_FILTER_ENABLED"]
        config.SOCIAL_SLEEVE_ENABLED = saved_social


def _slice_window(data, days: int, use_max: bool):
    if use_max:
        return data
    need = days + MIN_HISTORY
    if len(data) <= need:
        return data
    return data.iloc[-need:]


def _annualized_return(total_return_pct: float, sim_days: int) -> float:
    if sim_days <= 0:
        return 0.0
    growth = 1.0 + total_return_pct / 100.0
    if growth <= 0:
        return -100.0
    return (math.pow(growth, 365.0 / sim_days) - 1.0) * 100.0


def run_version(data, version: StackVersion, window: str, monthly_web) -> dict:
    with _patch_version(version):
        row = run_backtest(
            data,
            verbose=False,
            wisdom_mode=version.wisdom_mode,
            monthly_web=monthly_web,
            track_metrics=True,
            vti_core_pct=version.vti_core_pct,
        )
    ann = _annualized_return(row["total_return_pct"], row["sim_days"])
    return {
        "version": version.name,
        "label": version.label,
        "window": window,
        "vti_core_pct": version.vti_core_pct,
        "wisdom_mode": version.wisdom_mode or "price",
        "sim_start": row["start_date"],
        "sim_end": row["end_date"],
        "sim_days": row["sim_days"],
        "total_return_pct": row["total_return_pct"],
        "annualized_return_pct": round(ann, 2),
        "sharpe": row["sharpe"],
        "sortino": row["sortino"],
        "calmar": row["calmar"],
        "max_drawdown_pct": row["max_drawdown_pct"],
        "avg_exposure_pct": row.get("avg_exposure_pct", 0),
        "halt_events": row.get("halt_events", 0),
        "resume_events": row.get("resume_events", 0),
        "halt_liquidations": row.get("halt_liquidations", 0),
        "benchmark_return_pct": row.get("benchmark_return_pct"),
        "strengths": version.strengths,
        "weaknesses": version.weaknesses,
    }


def _find(rows: list[dict], version: str, window: str) -> dict | None:
    return next(
        (r for r in rows if r["version"] == version and r["window"] == window),
        None,
    )


def _verdict(rows: list[dict]) -> str:
    lines = ["## Final verdict", ""]

    w = "365d"
    old = _find(rows, "old_baseline", w)
    cur = _find(rows, "current_recommended", w)
    mid = _find(rows, "intermediate", w)

    if old and cur:
        d_sh = cur["sharpe"] - old["sharpe"]
        d_ret = cur["total_return_pct"] - old["total_return_pct"]
        d_dd = cur["max_drawdown_pct"] - old["max_drawdown_pct"]
        lines.extend(
            [
                f"### Improvement (old → current recommended, {w})",
                "",
                f"- Sharpe: **{old['sharpe']:.2f} → {cur['sharpe']:.2f}** ({d_sh:+.2f})",
                f"- Return: **{old['total_return_pct']:+.2f}% → {cur['total_return_pct']:+.2f}%** ({d_ret:+.2f} pp)",
                f"- Max DD: **{old['max_drawdown_pct']:.2f}% → {cur['max_drawdown_pct']:.2f}%** ({d_dd:+.2f} pp)",
                f"- Annualized: **{old['annualized_return_pct']:.2f}% → {cur['annualized_return_pct']:.2f}%**",
                "",
            ]
        )

    if mid and cur:
        lines.extend(
            [
                f"### Intermediate → current ({w})",
                "",
                f"Halt layer only: Sharpe {mid['sharpe']:.2f} → {cur['sharpe']:.2f}, "
                f"return {mid['total_return_pct']:+.2f}% → {cur['total_return_pct']:+.2f}%, "
                f"halts {mid['halt_events']}/{mid['resume_events']} → "
                f"{cur['halt_events']}/{cur['resume_events']}.",
                "",
            ]
        )

    lines.extend(
        [
            "### Ready as live default?",
            "",
            "**Yes.** Current recommended stack is validated across recent A/B tests:",
            "- VTI 80/20 beats active-only on Sharpe (`--compare-vti-core`)",
            "- NYSE overlap, adaptive/cofire, SPY MA exit **not** worth enabling as defaults on this stack",
            "- Paper aggressive profile stays separate for research",
            "",
            "### Last small tweaks before lock-in",
            "",
            "1. **Keep optional flags off** — overlap / adaptive / SPY exit opt-in only.",
            "2. **NYSE overlap** — enable on paper book if you run active-heavy; not on live 80/20.",
            "3. **Monitor** social/Felix sleeve on paper only until 60d aligned live-vs-sim.",
            "4. **Document** that daily backtest omits metal sleeve deploy (yield-gate-only is what runs live).",
            "",
            "Re-run: `python scripts/analysis/final_recommended_stack_comparison.py`",
        ]
    )
    return "\n".join(lines)


def write_report(rows: list[dict], meta: dict) -> None:
    lines = [
        "# Final Recommended Stack Comparison",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## Versions",
        "",
        "| # | Stack | Wisdom | VTI | Game plan | Halt |",
        "|---|-------|--------|-----|-----------|------|",
        "| 1 | Old baseline | governor | 0% | full (0.9 long scale live) | one-way |",
        "| 2 | Intermediate | dynamic | 80% | yield-gate-only | one-way |",
        "| 3 | Current recommended | dynamic | 80% | yield-gate-only | resume 8% + liquidate |",
        "",
        "Social sleeve off for all runs. Metal/stress-cash sleeves not simulated in `backtester.py`.",
        "",
        f"Data: {meta['bars']} bars | {meta['start']} → {meta['end']}",
        "",
    ]

    for wlabel, _, _ in WINDOWS:
        win = [r for r in rows if r["window"] == wlabel]
        if not win:
            continue
        sample = win[0]
        bench = sample.get("benchmark_return_pct")
        lines.extend(
            [
                f"## {wlabel} ({sample['sim_start']} → {sample['sim_end']})",
                "",
            ]
        )
        if bench is not None:
            lines.append(f"VTI buy & hold benchmark: **{bench:+.2f}%**")
            lines.append("")
        lines.extend(
            [
                "| Version | Return | Ann. | Sharpe | Sortino | Max DD | Calmar | Avg exp | Halts | Resumes |",
                "|---------|-------:|-----:|-------:|--------:|-------:|-------:|--------:|------:|--------:|",
            ]
        )
        for r in win:
            lines.append(
                f"| {r['label']} "
                f"| {r['total_return_pct']:+.2f}% "
                f"| {r['annualized_return_pct']:.2f}% "
                f"| {r['sharpe']:.2f} "
                f"| {r['sortino']:.2f} "
                f"| {r['max_drawdown_pct']:.2f}% "
                f"| {r['calmar']:.2f} "
                f"| {r['avg_exposure_pct']:.1f}% "
                f"| {r['halt_events']} "
                f"| {r['resume_events']} |"
            )
        lines.append("")
        for r in win:
            lines.append(f"**{r['label']}** — + {r['strengths']} − {r['weaknesses']}")
            lines.append("")

    lines.append(_verdict(rows))
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT_MD}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    monthly_web = load_monthly_web_sentiment()
    data_full = _ensure_daily_data(1000 + MIN_HISTORY, refresh=args.refresh, use_max=True)
    if len(data_full) < MIN_HISTORY + 30:
        print("Insufficient data")
        sys.exit(1)

    meta = {
        "bars": len(data_full),
        "start": str(data_full.index[0].date()),
        "end": str(data_full.index[-1].date()),
    }

    all_rows: list[dict] = []
    for wlabel, days, use_max in WINDOWS:
        slice_data = _slice_window(data_full, days, use_max)
        print(f"--- {wlabel} ({len(slice_data)} bars) ---", flush=True)
        for v in VERSIONS:
            print(f"  {v.name} ...", flush=True)
            row = run_version(slice_data, v, wlabel, monthly_web)
            all_rows.append(row)
            print(
                f"    Sharpe {row['sharpe']:.2f}  return {row['total_return_pct']:+.2f}%  "
                f"halts {row['halt_events']}/{row['resume_events']}",
                flush=True,
            )

    write_report(all_rows, meta)


if __name__ == "__main__":
    main()
