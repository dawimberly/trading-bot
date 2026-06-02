"""Grid A/B: SPY exit rule + dynamic sizing refinements on recommended stack.

Run from repo root:
  python scripts/analysis/refinements_grid_ab.py
  python scripts/analysis/refinements_grid_ab.py --days 500 2000 --max
  python scripts/analysis/refinements_grid_ab.py --quick
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import itertools
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from backtester import MIN_HISTORY, _ensure_daily_data, run_backtest

OUT_MD = Path(__file__).with_name("refinements_grid_results.md")
OUT_CSV = Path(__file__).with_name("refinements_grid_ab.csv")


@dataclass
class Variant:
    name: str
    spy_exit_on_ma_break: bool | None = None
    spy_ladder: bool | None = None
    nyse_beta: bool | None = None
    cofire_pct: float | None = None
    chunk_pct: float | None = None


def _baseline_variant() -> Variant:
    return Variant("baseline")


def generate_variants(*, quick: bool = False) -> list[Variant]:
    """Baseline + singles + factorial combo grid."""
    variants: list[Variant] = [_baseline_variant()]
    seen = {variants[0].name}

    def add(v: Variant) -> None:
        if v.name not in seen:
            seen.add(v.name)
            variants.append(v)

    # --- single toggles ---
    add(Variant("spy_exit_off", spy_exit_on_ma_break=False))
    add(Variant("spy_ladder", spy_ladder=True))
    add(Variant("nyse_beta", nyse_beta=True))
    for pct in (0.05, 0.08):
        add(Variant(f"cofire_{int(pct * 100)}", cofire_pct=pct))
    for pct in (0.04, 0.06, 0.07):
        add(Variant(f"chunk_{int(pct * 100)}", chunk_pct=pct))

    if quick:
        return variants

    # --- factorial: exit × ladder × beta × cofire ---
    for exit_on, ladder, beta, cofire in itertools.product(
        (True, False),
        (False, True),
        (False, True),
        (0.05, 0.06, 0.08),
    ):
        if exit_on and not ladder and not beta and cofire == 0.06:
            continue  # baseline
        name = f"grid_e{int(exit_on)}_l{int(ladder)}_b{int(beta)}_c{int(cofire * 100)}"
        add(
            Variant(
                name,
                spy_exit_on_ma_break=exit_on,
                spy_ladder=ladder,
                nyse_beta=beta,
                cofire_pct=cofire,
            )
        )

    return variants


@contextlib.contextmanager
def _patch(v: Variant):
    saved = {
        "GAME_PLAN_ENABLED": config.GAME_PLAN_ENABLED,
        "GAME_PLAN_YIELD_GATE_ONLY": config.GAME_PLAN_YIELD_GATE_ONLY,
        "YIELD_GATE_ENABLED": config.YIELD_GATE_ENABLED,
        "NYSE_OVERLAP_FILTER_ENABLED": config.NYSE_OVERLAP_FILTER_ENABLED,
        "NYSE_SPY_CORR_MAX": config.NYSE_SPY_CORR_MAX,
        "ADAPTIVE_CHUNK_ENABLED": config.ADAPTIVE_CHUNK_ENABLED,
        "COFIRE_BUDGET_ENABLED": config.COFIRE_BUDGET_ENABLED,
        "ADAPTIVE_CHUNK_MAX_PCT": config.ADAPTIVE_CHUNK_MAX_PCT,
        "COFIRE_BUDGET_PCT": config.COFIRE_BUDGET_PCT,
        "HALT_RESUME_DRAWDOWN_PCT": config.HALT_RESUME_DRAWDOWN_PCT,
        "HALT_LIQUIDATE_ON_BREACH": config.HALT_LIQUIDATE_ON_BREACH,
        "SPY_EXIT_ON_MA_BREAK": config.SPY_EXIT_ON_MA_BREAK,
        "SPY_LADDER_SIZING_ENABLED": config.SPY_LADDER_SIZING_ENABLED,
        "NYSE_BETA_SCALING_ENABLED": config.NYSE_BETA_SCALING_ENABLED,
    }
    # Recommended stack baseline
    config.GAME_PLAN_ENABLED = True
    config.GAME_PLAN_YIELD_GATE_ONLY = True
    config.YIELD_GATE_ENABLED = True
    config.NYSE_OVERLAP_FILTER_ENABLED = True
    config.NYSE_SPY_CORR_MAX = 0.80
    config.ADAPTIVE_CHUNK_ENABLED = True
    config.COFIRE_BUDGET_ENABLED = True
    config.HALT_RESUME_DRAWDOWN_PCT = 0.08
    config.HALT_LIQUIDATE_ON_BREACH = True

    if v.spy_exit_on_ma_break is not None:
        config.SPY_EXIT_ON_MA_BREAK = v.spy_exit_on_ma_break
    if v.spy_ladder is not None:
        config.SPY_LADDER_SIZING_ENABLED = v.spy_ladder
    if v.nyse_beta is not None:
        config.NYSE_BETA_SCALING_ENABLED = v.nyse_beta
    if v.cofire_pct is not None:
        config.COFIRE_BUDGET_PCT = v.cofire_pct
    if v.chunk_pct is not None:
        config.ADAPTIVE_CHUNK_MAX_PCT = v.chunk_pct

    try:
        yield
    finally:
        for key, val in saved.items():
            setattr(config, key, val)


def _slice_window(data, days: int | None, use_max: bool):
    if use_max:
        return data
    if days is None:
        return data
    need = days + MIN_HISTORY
    if len(data) <= need:
        return data
    return data.iloc[-need:]


def run_variant(data, v: Variant, window: str) -> dict:
    with _patch(v):
        row = run_backtest(data, track_spy_fill=False, verbose=False)
    row["variant"] = v.name
    row["window"] = window
    row["spy_exit"] = (
        v.spy_exit_on_ma_break
        if v.spy_exit_on_ma_break is not None
        else config.SPY_EXIT_ON_MA_BREAK
    )
    row["spy_ladder"] = (
        v.spy_ladder if v.spy_ladder is not None else config.SPY_LADDER_SIZING_ENABLED
    )
    row["nyse_beta"] = (
        v.nyse_beta if v.nyse_beta is not None else config.NYSE_BETA_SCALING_ENABLED
    )
    row["cofire_pct"] = v.cofire_pct if v.cofire_pct is not None else config.COFIRE_BUDGET_PCT
    row["chunk_pct"] = (
        v.chunk_pct if v.chunk_pct is not None else config.ADAPTIVE_CHUNK_MAX_PCT
    )
    return row


def _fmt_table(rows: list[dict]) -> str:
    lines = [
        "| Variant | Return | Sharpe | Max DD | SPY | NYSE | Crypto | Orders |",
        "|---------|-------:|-------:|-------:|----:|-----:|-------:|-------:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['variant']} "
            f"| {r['total_return_pct']:+.2f}% "
            f"| {r['sharpe']:.2f} "
            f"| {r['max_drawdown_pct']:.2f}% "
            f"| {r['spy_signals']} "
            f"| {r['nyse_signals']} "
            f"| {r['crypto_signals']} "
            f"| {r['total_orders']} |"
        )
    return "\n".join(lines)


def _score(row: dict) -> tuple:
    """Rank: Sharpe first, then return, then shallower drawdown."""
    return (row["sharpe"], row["total_return_pct"], row["max_drawdown_pct"])


def _recommend(all_rows: list[dict], variants: list[Variant]) -> str:
    by_window: dict[str, list[dict]] = {}
    for r in all_rows:
        by_window.setdefault(r["window"], []).append(r)

    lines = ["## Recommendations", ""]
    best_overall: tuple[str, dict] | None = None
    best_score = (-999.0, -999.0, -999.0)

    for window, rows in sorted(by_window.items()):
        baseline = next((r for r in rows if r["variant"] == "baseline"), rows[0])
        best = max(rows, key=_score)
        lines.append(f"### Window {window}")
        lines.append(
            f"- Baseline: return {baseline['total_return_pct']:+.2f}%, "
            f"Sharpe {baseline['sharpe']:.2f}, max DD {baseline['max_drawdown_pct']:.2f}%"
        )
        lines.append(
            f"- Best Sharpe: **{best['variant']}** — return {best['total_return_pct']:+.2f}%, "
            f"Sharpe {best['sharpe']:.2f}, max DD {best['max_drawdown_pct']:.2f}% "
            f"(SPY/NYSE/crypto signals {best['spy_signals']}/{best['nyse_signals']}/{best['crypto_signals']})"
        )
        delta_sh = best["sharpe"] - baseline["sharpe"]
        delta_ret = best["total_return_pct"] - baseline["total_return_pct"]
        lines.append(
            f"- vs baseline: Sharpe {delta_sh:+.2f}, return {delta_ret:+.2f} pp, "
            f"max DD {best['max_drawdown_pct'] - baseline['max_drawdown_pct']:+.2f} pp"
        )
        lines.append("")

        sc = _score(best)
        if sc > best_score:
            best_score = sc
            best_overall = (window, best)

    lines.append("### Suggested config overrides")
    lines.append("")
    if best_overall:
        w, b = best_overall
        overrides = []
        if b["variant"] != "baseline":
            if b.get("spy_exit") is not None and b["spy_exit"] != True:
                overrides.append("SPY_EXIT_ON_MA_BREAK=false")
            elif b.get("spy_exit") is True:
                overrides.append("SPY_EXIT_ON_MA_BREAK=true")
            if b.get("spy_ladder"):
                overrides.append("SPY_LADDER_SIZING_ENABLED=true")
            if b.get("nyse_beta"):
                overrides.append("NYSE_BETA_SCALING_ENABLED=true")
            if b.get("cofire_pct") and b["cofire_pct"] != 0.06:
                overrides.append(f"COFIRE_BUDGET_PCT={b['cofire_pct']}")
            if b.get("chunk_pct") and b["chunk_pct"] != 0.05:
                overrides.append(f"ADAPTIVE_CHUNK_MAX_PCT={b['chunk_pct']}")
        if overrides:
            lines.append(f"Best combo on **{w}** (`{b['variant']}`):")
            for o in overrides:
                lines.append(f"- `{o}`")
        else:
            lines.append(f"Keep recommended defaults on **{w}** (baseline wins).")
    lines.append("")
    lines.append(
        "Baseline stack: yield-gate-only game plan, NYSE overlap 0.80, "
        "adaptive chunk + co-fire, halt resume 8% + liquidate, SPY_EXIT_ON_MA_BREAK=true."
    )
    return "\n".join(lines)


def write_report(all_rows: list[dict], variants: list[Variant]) -> None:
    windows = sorted({r["window"] for r in all_rows})
    sections = [
        "# Refinements Grid A/B Results",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "Tests SPY MA-break exit, SPY ladder sizing, NYSE beta scaling, "
        "and co-fire / adaptive-chunk tuning on the recommended live stack.",
        "",
        f"Variants: {len(variants)} | Windows: {', '.join(windows)}",
        "",
    ]
    for w in windows:
        wrows = sorted(
            [r for r in all_rows if r["window"] == w],
            key=_score,
            reverse=True,
        )
        sections.append(f"## Window {w}")
        sections.append("")
        sections.append(_fmt_table(wrows))
        sections.append("")

    sections.append(_recommend(all_rows, variants))
    OUT_MD.write_text("\n".join(sections), encoding="utf-8")

    keys = sorted({k for r in all_rows for k in r if k != "regime_counts"})
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        for r in all_rows:
            writer.writerow(r)
    print(f"Wrote {OUT_MD}")
    print(f"Wrote {OUT_CSV}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--days",
        type=int,
        nargs="+",
        action="append",
        help="Window lengths, e.g. --days 500 2000 or --days 500 --days 2000",
    )
    ap.add_argument("--max", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument(
        "--quick",
        action="store_true",
        help="Singles only (skip full factorial grid)",
    )
    args = ap.parse_args()

    raw_days = args.days or [500, 2000]
    day_windows = [d for group in raw_days for d in group]
    variants = generate_variants(quick=args.quick)
    print(f"Refinements grid: {len(variants)} variants")

    print("Loading daily data ...", flush=True)
    data_full = _ensure_daily_data(max(day_windows), refresh=args.refresh, use_max=False)
    if len(data_full) < MIN_HISTORY + 10:
        print("Insufficient data; run: python fetch_data.py --daily --days 2000")
        sys.exit(1)

    all_rows: list[dict] = []
    for d in day_windows:
        label = f"{d}d"
        slice_data = _slice_window(data_full, d, False)
        print(f"--- Window {label} ({len(slice_data)} bars, {len(variants)} variants) ---")
        for i, v in enumerate(variants, 1):
            print(f"  [{i}/{len(variants)}] {v.name} ...", flush=True)
            row = run_variant(slice_data, v, label)
            all_rows.append(row)
            print(
                f"    ret {row['total_return_pct']:+.2f}% sh {row['sharpe']:.2f} "
                f"dd {row['max_drawdown_pct']:.2f}% "
                f"spy/nyse/cr {row['spy_signals']}/{row['nyse_signals']}/{row['crypto_signals']}"
            )

    if args.max:
        data_max = _ensure_daily_data(0, refresh=args.refresh, use_max=True)
        label = "max"
        print(f"--- Window {label} ({len(data_max)} bars, {len(variants)} variants) ---")
        for i, v in enumerate(variants, 1):
            print(f"  [{i}/{len(variants)}] {v.name} ...", flush=True)
            row = run_variant(data_max, v, label)
            all_rows.append(row)
            print(
                f"    ret {row['total_return_pct']:+.2f}% sh {row['sharpe']:.2f} "
                f"dd {row['max_drawdown_pct']:.2f}%"
            )

    write_report(all_rows, variants)


if __name__ == "__main__":
    main()
