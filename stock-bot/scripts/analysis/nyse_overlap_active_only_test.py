"""Active-only NYSE overlap filter A/B (0% VTI, full sleeve caps).

Run from repo root:
  python scripts/analysis/nyse_overlap_active_only_test.py
  python scripts/analysis/nyse_overlap_active_only_test.py --refresh
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from backtester import MIN_HISTORY, _ensure_daily_data, run_backtest
from modules.pipeline_strategies import (
    PAUSED_REGIMES,
    _equity_momentum_candidates,
    _equity_momentum_ranked,
    _filter_nyse_anti_overlap,
    _on_cooldown,
    _spy_buy_intent,
    _spy_sleeve_active,
    _nyse_buy_intent,
)
from modules.wayback_sentiment import load_monthly_web_sentiment

OUT_MD = Path(__file__).with_name("nyse_overlap_active_only_test.md")

STACK_KEYS = (
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
)

STACK_BASE = {
    "GAME_PLAN_ENABLED": True,
    "GAME_PLAN_YIELD_GATE_ONLY": True,
    "YIELD_GATE_ENABLED": True,
    "NYSE_BETA_SCALING_ENABLED": False,
    "ADAPTIVE_CHUNK_ENABLED": False,
    "COFIRE_BUDGET_ENABLED": False,
    "SPY_EXIT_ON_MA_BREAK": False,
    "HALT_RESUME_DRAWDOWN_PCT": 0.08,
    "HALT_LIQUIDATE_ON_BREACH": True,
    "DERIVED_BEAR_PAUSE_ENABLED": False,
}


@dataclass
class Variant:
    name: str
    label: str
    overlap: bool
    flags: dict = field(default_factory=dict)


def _variant(name: str, label: str, overlap: bool) -> Variant:
    flags = dict(STACK_BASE)
    flags["NYSE_OVERLAP_FILTER_ENABLED"] = overlap
    flags["NYSE_SPY_CORR_MAX"] = 0.80
    return Variant(name, label, overlap, flags)


VARIANTS = [
    _variant("baseline", "Active-only (overlap off)", False),
    _variant("overlap", "Active-only + NYSE overlap (corr ≤ 0.80)", True),
]

WINDOWS = (
    ("365d", 365, False),
    ("1000d", 1000, False),
    ("max", 0, True),
)


@contextlib.contextmanager
def _patch_stack(v: Variant):
    saved = {k: getattr(config, k) for k in STACK_KEYS}
    saved_social = config.SOCIAL_SLEEVE_ENABLED
    for key, val in v.flags.items():
        setattr(config, key, val)
    config.NYSE_ANTI_OVERLAP_ENABLED = v.overlap
    config.SOCIAL_SLEEVE_ENABLED = False
    try:
        yield
    finally:
        for key, val in saved.items():
            setattr(config, key, val)
        config.NYSE_ANTI_OVERLAP_ENABLED = saved["NYSE_OVERLAP_FILTER_ENABLED"]
        config.SOCIAL_SLEEVE_ENABLED = saved_social


def _slice_window(data: pd.DataFrame, days: int, use_max: bool) -> pd.DataFrame:
    if use_max:
        return data
    need = days + MIN_HISTORY
    if len(data) <= need:
        return data
    return data.iloc[-need:]


def _nyse_universe_columns(data: pd.DataFrame) -> list[str]:
    return [
        c
        for c in data.columns
        if not config.is_crypto(c)
        and c != config.SPY_BOT_SYMBOL
        and c != config.VTI_CORE_SYMBOL
        and not config.is_metal_symbol(c)
    ]


def _instrument(
    data: pd.DataFrame,
    v: Variant,
    *,
    monthly_web,
) -> dict:
    """Pick / co-fire / overlap-trigger stats (no portfolio simulation)."""
    from modules.wisdom_sentiment import resolve_backtest_regime

    equity_cols = _nyse_universe_columns(data)
    cooldown_bars = 1
    pair_cooldown: dict = {}
    metrics_cd: dict = {}

    spy_active_days = 0
    cofire_days = 0
    overlap_triggers = 0
    symbols_filtered_events = 0
    pick_raw: Counter = Counter()
    pick_filt: Counter = Counter()
    trade_days = 0

    for i in range(MIN_HISTORY, len(data)):
        window = data.iloc[: i + 1]
        regime, vol, _, _ = resolve_backtest_regime(
            window, data.index[i], monthly_web, wisdom_mode="dynamic"
        )
        trade_days += 1

        spy_active = _spy_sleeve_active(window, yield_gated=False, regime=regime)
        if spy_active:
            spy_active_days += 1

        raw_ranked = _equity_momentum_candidates(window, equity_cols)
        raw_top = raw_ranked[0] if raw_ranked else None
        filtered_ranked = _equity_momentum_ranked(
            window, equity_cols, yield_gated=False, regime=regime
        )
        filt_top = filtered_ranked[0] if filtered_ranked else None

        if raw_top:
            pick_raw[raw_top] += 1
        if filt_top:
            pick_filt[filt_top] += 1

        if v.overlap and spy_active and raw_ranked:
            pre = raw_ranked
            if config.NYSE_SECTOR_TECH_CAP > 0:
                from modules.pipeline_strategies import _apply_sector_tech_cap

                pre = _apply_sector_tech_cap(pre)
            post = _filter_nyse_anti_overlap(window, pre) if v.overlap else pre
            dropped = len(pre) - len(post)
            if dropped > 0:
                symbols_filtered_events += 1
            if raw_top and filt_top and raw_top != filt_top:
                overlap_triggers += 1

        spy_wants = _spy_buy_intent(
            window, regime, i, pair_cooldown, cooldown_bars=cooldown_bars
        )
        nyse_wants = _nyse_buy_intent(
            window, regime, i, pair_cooldown, cooldown_bars=cooldown_bars
        )
        if spy_wants and nyse_wants:
            cofire_days += 1

    return {
        "spy_active_pct": round(100 * spy_active_days / trade_days, 1) if trade_days else 0,
        "cofire_pct": round(100 * cofire_days / trade_days, 1) if trade_days else 0,
        "cofire_days": cofire_days,
        "overlap_triggers": overlap_triggers,
        "symbols_filtered_events": symbols_filtered_events,
        "top_picks_raw": pick_raw.most_common(8),
        "top_picks_filtered": pick_filt.most_common(8),
        "trade_days": trade_days,
    }


def _run_perf(data: pd.DataFrame, v: Variant, monthly_web) -> dict:
    with _patch_stack(v):
        row = run_backtest(
            data,
            track_spy_fill=False,
            verbose=False,
            wisdom_mode="dynamic",
            monthly_web=monthly_web,
            track_metrics=True,
            vti_core_pct=0.0,
        )
    return row


def _fmt_picks(picks: list[tuple[str, int]]) -> str:
    if not picks:
        return "—"
    return ", ".join(f"{sym} ({n})" for sym, n in picks)


def _recommendation(rows: list[dict]) -> str:
    """See nyse_overlap_active_only_test.md for full narrative (edited after review)."""
    return (
        "## Conclusion\n\n"
        "See full analysis in this file (concentration, risk-adjusted returns, default recommendation).\n"
    )


def write_report(rows: list[dict], data_meta: dict) -> None:
    lines = [
        "# NYSE Overlap Filter — Active-Only A/B",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## Setup",
        "",
        "- **VTI core:** 0% (active-only, full caps: SPY 45% / NYSE 20% / crypto 20%)",
        "- **Stack:** yield-gate-only game plan + dynamic wisdom (same as live)",
        "- **Variants:** baseline (overlap off) vs overlap on (`NYSE_SPY_CORR_MAX=0.80`)",
        "- **Social sleeve:** off for this test",
        "",
        f"Data: {data_meta.get('bars')} daily bars | "
        f"full range {data_meta.get('start')} → {data_meta.get('end')}",
        "",
    ]

    for wlabel, _, _ in WINDOWS:
        win_rows = [r for r in rows if r["window"] == wlabel]
        if not win_rows:
            continue
        sample = win_rows[0]
        lines.extend(
            [
                f"## {wlabel} ({sample.get('sim_start')} → {sample.get('sim_end')})",
                "",
                "| Variant | Return | Sharpe | Sortino | Max DD | Calmar | Avg SPY exp | Avg NYSE exp | Co-fire % |",
                "|---------|-------:|-------:|--------:|-------:|-------:|------------:|-------------:|----------:|",
            ]
        )
        for r in win_rows:
            lines.append(
                f"| {r['label']} "
                f"| {r['total_return_pct']:+.2f}% "
                f"| {r['sharpe']:.2f} "
                f"| {r['sortino']:.2f} "
                f"| {r['max_drawdown_pct']:.2f}% "
                f"| {r['calmar']:.2f} "
                f"| {r['avg_spy_exposure_pct']:.1f}% "
                f"| {r['avg_nyse_exposure_pct']:.1f}% "
                f"| {r['cofire_pct']:.1f}% |"
            )
        lines.append("")
        for r in win_rows:
            lines.extend(
                [
                    f"### {r['label']} — overlap diagnostics",
                    "",
                    f"- Overlap filter changed top pick: **{r['overlap_triggers']}** days",
                    f"- Days with ≥1 symbol filtered (when SPY active): **{r['symbols_filtered_events']}**",
                    f"- SPY sleeve active: **{r['spy_active_pct']:.1f}%** of sim days",
                    f"- Top NYSE pick (unfiltered rank): {_fmt_picks(r['top_picks_raw'])}",
                    f"- Top NYSE pick (after filter): {_fmt_picks(r['top_picks_filtered'])}",
                    "",
                ]
            )

    lines.append(_recommendation(rows))
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT_MD}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Active-only NYSE overlap A/B")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    monthly_web = load_monthly_web_sentiment()
    data_full = _ensure_daily_data(1000 + MIN_HISTORY, refresh=args.refresh, use_max=True)
    if len(data_full) < MIN_HISTORY + 30:
        print("Insufficient daily data; run: python fetch_data.py --daily --max")
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
        for v in VARIANTS:
            print(f"  {v.name} perf ...", flush=True)
            perf = _run_perf(slice_data, v, monthly_web)
            print(f"  {v.name} instrument ...", flush=True)
            with _patch_stack(v):
                inst = _instrument(slice_data, v, monthly_web=monthly_web)

            row = {
                "window": wlabel,
                "variant": v.name,
                "label": v.label,
                "sim_start": perf["start_date"],
                "sim_end": perf["end_date"],
                "total_return_pct": perf["total_return_pct"],
                "sharpe": perf["sharpe"],
                "sortino": perf["sortino"],
                "calmar": perf["calmar"],
                "max_drawdown_pct": perf["max_drawdown_pct"],
                "avg_spy_exposure_pct": perf.get("avg_spy_exposure_pct", 0),
                "avg_nyse_exposure_pct": perf.get("avg_nyse_exposure_pct", 0),
                "cofire_pct": perf.get("cofire_pct", inst["cofire_pct"]),
                "spy_active_pct": inst["spy_active_pct"],
                "overlap_triggers": inst["overlap_triggers"],
                "symbols_filtered_events": inst["symbols_filtered_events"],
                "top_picks_raw": inst["top_picks_raw"],
                "top_picks_filtered": inst["top_picks_filtered"],
            }
            all_rows.append(row)
            print(
                f"    Sharpe {row['sharpe']:.2f}  return {row['total_return_pct']:+.2f}%  "
                f"overlap triggers {row['overlap_triggers']}",
                flush=True,
            )

    write_report(all_rows, meta)


if __name__ == "__main__":
    main()
