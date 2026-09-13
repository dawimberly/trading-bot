"""Chaotic backtest suite — stress-test paper-aggressive stack under extreme paths.

Uses historical closes + synthetic shocks (crash, COVID vol, inflation bear,
flash crash, rate-hike drag, correlation breakdown, liquidity gaps, …).

Each scenario runs:
  1) Full paper-aggressive backtest (Smart Dynamic VTI, RVOL/ORB/Catalyst,
     ATR, conviction, etc. via the normal research profile)
  2) Monte Carlo perturbations *within* that chaotic path

Examples:
  python scripts/analysis/chaotic_backtest.py --scenarios 5 --days 365
  python scripts/analysis/chaotic_backtest.py --scenario-ids crash_2008,covid_2020,flash_crash --days 250 --mc-runs 15
  python scripts/analysis/chaotic_backtest.py --list
  python scripts/analysis/chaotic_backtest.py --scenarios 5 --days 180 --fast-mode --mc-runs 8

Add new chaos types in ``modules/chaos_scenarios.py`` with ``@register_scenario``.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from backtester import MIN_HISTORY, _ensure_daily_data, run_backtest
from modules.backtester_core import (
    RUN_OPTIONS,
    apply_default_execution_costs,
    apply_run_options_to_config,
    release_backtest_memory,
    reset_caches,
)
from modules.chaos_scenarios import (
    DEFAULT_CATALOG_ORDER,
    list_scenarios,
    select_scenarios,
)
from scripts.analysis.monte_carlo_backtest import perturb_market_data

DEFAULT_EXPORT = Path(__file__).with_name("chaotic_backtest_last.json")


@dataclass
class ScenarioResult:
    scenario_id: str
    name: str
    description: str
    return_pct: float
    sharpe: float
    max_dd_pct: float
    recovery_bars: int | None
    recovery_days: float | None
    final_equity: float
    mc_runs: int = 0
    mc_return_mean: float | None = None
    mc_return_p5: float | None = None
    mc_return_p95: float | None = None
    mc_sharpe_mean: float | None = None
    mc_max_dd_mean: float | None = None
    mc_prob_loss_pct: float | None = None
    elapsed_s: float = 0.0
    notes: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


def _trim_data_window(data: pd.DataFrame, days: int | None) -> pd.DataFrame:
    sim_target_days = days or config.BACKTEST_DAYS
    target_sim_bars = max(5, int(sim_target_days * 0.80))
    n_bars = len(data)
    warmup = min(MIN_HISTORY, max(0, n_bars - 5))
    desired_total = warmup + target_sim_bars
    if len(data) > desired_total:
        return data.iloc[-desired_total:].copy()
    return data.copy()


def _paper_aggressive_kwargs() -> dict[str, Any]:
    return {
        "paper_aggressive": True,
        "paper_sleeve_features": True,
        "paper_dynamic_vti": True,
        "stat_arb_report": False,
        "track_metrics": True,
        "track_active_exposure": False,
        "verbose": False,
    }


def recovery_time_bars(equity_values: list[float] | None) -> tuple[int | None, float | None]:
    """Bars from max-drawdown trough until equity recovers prior peak.

    Returns (bars, calendar_days_approx) — days ≈ bars * 365/252.
    """
    if not equity_values or len(equity_values) < 3:
        return None, None
    eq = np.asarray(equity_values, dtype=float)
    if not np.isfinite(eq).all():
        eq = np.nan_to_num(eq, nan=float(eq[np.isfinite(eq)][0]) if np.isfinite(eq).any() else 0.0)
    peak = eq[0]
    peak_i = 0
    trough_i = 0
    max_dd = 0.0
    dd_peak_i = 0
    dd_trough_i = 0
    for i, v in enumerate(eq):
        if v >= peak:
            peak = v
            peak_i = i
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
            dd_peak_i = peak_i
            dd_trough_i = i
            trough_i = i
    if max_dd < 1e-6:
        return 0, 0.0
    recover_i = None
    target = eq[dd_peak_i]
    for j in range(dd_trough_i, len(eq)):
        if eq[j] >= target:
            recover_i = j
            break
    if recover_i is None:
        return None, None
    bars = int(recover_i - dd_trough_i)
    days = round(bars * (365.0 / 252.0), 1)
    return bars, days


def _run_one_backtest(data: pd.DataFrame, **kwargs) -> dict[str, Any]:
    release_backtest_memory(collect=False)
    reset_caches()
    # Mute per-bar deploy chatter; keep real errors visible via logging if needed.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        return run_backtest(data, **kwargs)


def _mc_within_scenario(
    chaotic: pd.DataFrame,
    *,
    mc_runs: int,
    seed: int,
    noise_level: float,
    regime_noise: float,
    bt_kwargs: dict[str, Any],
) -> dict[str, float]:
    if mc_runs <= 0:
        return {}
    rng = np.random.default_rng(seed)
    rets: list[float] = []
    sharpes: list[float] = []
    dds: list[float] = []
    for _ in range(mc_runs):
        perturbed, _, _ = perturb_market_data(
            chaotic,
            noise_level=noise_level,
            regime_noise=regime_noise,
            rng=rng,
        )
        result = _run_one_backtest(perturbed, **bt_kwargs)
        rets.append(float(result.get("total_return_pct") or 0))
        sharpes.append(float(result.get("sharpe") or 0))
        dds.append(float(result.get("max_drawdown_pct") or 0))
    arr_r = np.asarray(rets, dtype=float)
    arr_s = np.asarray(sharpes, dtype=float)
    arr_d = np.asarray(dds, dtype=float)
    return {
        "mc_runs": float(mc_runs),
        "mc_return_mean": round(float(np.mean(arr_r)), 2),
        "mc_return_p5": round(float(np.percentile(arr_r, 5)), 2),
        "mc_return_p95": round(float(np.percentile(arr_r, 95)), 2),
        "mc_sharpe_mean": round(float(np.mean(arr_s)), 2),
        "mc_max_dd_mean": round(float(np.mean(arr_d)), 2),
        "mc_prob_loss_pct": round(float(np.mean(arr_r < 0) * 100), 1),
    }


def run_scenario(
    scenario,
    base: pd.DataFrame,
    *,
    min_history: int,
    seed: int,
    mc_runs: int,
    noise_level: float,
    regime_noise: float,
    bt_kwargs: dict[str, Any],
) -> ScenarioResult:
    t0 = time.perf_counter()
    rng = np.random.default_rng(seed)
    chaotic = scenario.apply(base, rng, min_history)
    primary = _run_one_backtest(chaotic, **bt_kwargs)
    rec_bars, rec_days = recovery_time_bars(primary.get("equity_values"))
    mc = _mc_within_scenario(
        chaotic,
        mc_runs=mc_runs,
        seed=seed + 17,
        noise_level=noise_level,
        regime_noise=regime_noise,
        bt_kwargs=bt_kwargs,
    )
    elapsed = time.perf_counter() - t0
    return ScenarioResult(
        scenario_id=scenario.id,
        name=scenario.name,
        description=scenario.description,
        return_pct=float(primary.get("total_return_pct") or 0),
        sharpe=float(primary.get("sharpe") or 0),
        max_dd_pct=float(primary.get("max_drawdown_pct") or 0),
        recovery_bars=rec_bars,
        recovery_days=rec_days,
        final_equity=float(primary.get("final_equity") or 0),
        mc_runs=int(mc.get("mc_runs") or 0),
        mc_return_mean=mc.get("mc_return_mean"),
        mc_return_p5=mc.get("mc_return_p5"),
        mc_return_p95=mc.get("mc_return_p95"),
        mc_sharpe_mean=mc.get("mc_sharpe_mean"),
        mc_max_dd_mean=mc.get("mc_max_dd_mean"),
        mc_prob_loss_pct=mc.get("mc_prob_loss_pct"),
        elapsed_s=round(elapsed, 1),
        notes=scenario.era_hint or "",
    )


def _fmt_rec(bars: int | None, days: float | None) -> str:
    if bars is None:
        return "never"
    if bars == 0:
        return "0"
    if days is not None:
        return f"{bars}b/~{days:.0f}d"
    return f"{bars}b"


def print_comparison_table(rows: list[ScenarioResult]) -> None:
    print()
    print("=" * 108)
    print("CHAOTIC BACKTEST — normal vs stressed scenarios (paper-aggressive)")
    print("=" * 108)
    hdr = (
        f"{'Scenario':<28} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Recover':>12} {'MC μRet':>8} {'MC p5':>7} {'MC P(loss)':>10}"
    )
    print(hdr)
    print("-" * 108)
    for r in rows:
        mc_ret = f"{r.mc_return_mean:+.1f}%" if r.mc_return_mean is not None else "—"
        mc_p5 = f"{r.mc_return_p5:+.1f}%" if r.mc_return_p5 is not None else "—"
        mc_loss = f"{r.mc_prob_loss_pct:.0f}%" if r.mc_prob_loss_pct is not None else "—"
        print(
            f"{r.name[:28]:<28} "
            f"{r.return_pct:>+7.2f}% "
            f"{r.sharpe:>7.2f} "
            f"{r.max_dd_pct:>7.2f}% "
            f"{_fmt_rec(r.recovery_bars, r.recovery_days):>12} "
            f"{mc_ret:>8} "
            f"{mc_p5:>7} "
            f"{mc_loss:>10}"
        )
    print("-" * 108)
    baseline = next((r for r in rows if r.scenario_id == "normal"), rows[0] if rows else None)
    if baseline and len(rows) > 1:
        print(
            f"Baseline ({baseline.name}): "
            f"{baseline.return_pct:+.2f}% return | Sharpe {baseline.sharpe:.2f} | "
            f"MaxDD {baseline.max_dd_pct:.2f}%"
        )
        stressed = [r for r in rows if r.scenario_id != "normal"]
        if stressed:
            worst = min(stressed, key=lambda x: x.return_pct)
            deepest = min(stressed, key=lambda x: x.max_dd_pct)
            print(
                f"Worst return: {worst.name} ({worst.return_pct:+.2f}%) | "
                f"Deepest DD: {deepest.name} ({deepest.max_dd_pct:.2f}%)"
            )
    print()


def print_scenario_details(rows: list[ScenarioResult]) -> None:
    print("Scenario detail")
    print("-" * 72)
    for r in rows:
        print(f"  [{r.scenario_id}] {r.name}")
        print(f"    {r.description}")
        if r.mc_runs:
            print(
                f"    MC({r.mc_runs}): μSharpe={r.mc_sharpe_mean} "
                f"μMaxDD={r.mc_max_dd_mean}%  elapsed={r.elapsed_s:.0f}s"
            )
        else:
            print(f"    elapsed={r.elapsed_s:.0f}s")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Chaotic backtest suite (paper-aggressive stress tests)"
    )
    parser.add_argument(
        "--scenarios",
        type=int,
        default=5,
        help="Number of scenarios from the default catalog (includes normal). Default 5.",
    )
    parser.add_argument(
        "--scenario-ids",
        type=str,
        default="",
        help="Comma-separated scenario ids (overrides --scenarios). Example: normal,crash_2008,flash_crash",
    )
    parser.add_argument("--days", type=int, default=365, help="Simulation days (default 365)")
    parser.add_argument("--refresh", action="store_true", help="Refresh daily data cache")
    parser.add_argument("--max", action="store_true", help="Use max available history then trim")
    parser.add_argument(
        "--mc-runs",
        type=int,
        default=12,
        help="Monte Carlo runs within each chaotic scenario (default 12; 0 to skip)",
    )
    parser.add_argument("--noise-level", type=float, default=0.008, help="MC idiosyncratic noise")
    parser.add_argument("--regime-noise", type=float, default=0.12, help="MC regime/vol noise")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed")
    parser.add_argument("--fast-mode", action="store_true", help="Smaller universe / faster path")
    parser.add_argument("--list", action="store_true", help="List registered chaos scenarios and exit")
    parser.add_argument(
        "--export-json",
        type=str,
        default=str(DEFAULT_EXPORT),
        help=f"Write results JSON (default {DEFAULT_EXPORT.name})",
    )
    parser.add_argument("--no-export", action="store_true", help="Skip JSON export")
    args = parser.parse_args(argv)

    if args.list:
        print("Registered chaos scenarios:")
        for sc in select_scenarios(n=None):
            mark = "*" if sc.id in DEFAULT_CATALOG_ORDER[:5] else " "
            print(f"  {mark} {sc.id:<22} {sc.name}")
            print(f"      {sc.description}")
        print("\n* = included in default --scenarios 5")
        return 0

    apply_run_options_to_config()
    if args.fast_mode:
        RUN_OPTIONS.fast_mode = True
        apply_run_options_to_config()
    apply_default_execution_costs()

    ids = [s.strip() for s in args.scenario_ids.split(",") if s.strip()] or None
    scenarios = select_scenarios(n=args.scenarios if not ids else None, ids=ids)
    if not scenarios:
        print("No scenarios selected.")
        return 1

    print("=== Chaotic backtest suite ===")
    print(f"Profile: paper-aggressive (full research stack)")
    print(f"Scenarios ({len(scenarios)}): {', '.join(s.id for s in scenarios)}")
    print(f"Days={args.days} | MC/scenario={args.mc_runs} | seed={args.seed}")
    if RUN_OPTIONS.fast_mode:
        print("FAST MODE on")

    data = _ensure_daily_data(
        args.days if not args.max else 0,
        refresh=args.refresh,
        use_max=bool(args.max),
    )
    base = _trim_data_window(data, args.days)
    if len(base) < MIN_HISTORY + 20:
        print(f"Need more history; got {len(base)} bars (min {MIN_HISTORY + 20}).")
        return 1

    warmup = min(MIN_HISTORY, max(0, len(base) - 5))
    print(
        f"Window: {base.index[warmup].date()} -> {base.index[-1].date()} "
        f"({len(base) - warmup} sim bars, {warmup} warmup)"
    )

    bt_kwargs = _paper_aggressive_kwargs()
    rows: list[ScenarioResult] = []
    suite_t0 = time.perf_counter()
    for i, sc in enumerate(scenarios):
        print(f"\n--- [{i + 1}/{len(scenarios)}] {sc.id}: {sc.name} ---")
        row = run_scenario(
            sc,
            base,
            min_history=warmup,
            seed=args.seed + i * 101,
            mc_runs=max(0, int(args.mc_runs)),
            noise_level=args.noise_level,
            regime_noise=args.regime_noise,
            bt_kwargs=bt_kwargs,
        )
        print(
            f"  return={row.return_pct:+.2f}% sharpe={row.sharpe:.2f} "
            f"maxDD={row.max_dd_pct:.2f}% recover={_fmt_rec(row.recovery_bars, row.recovery_days)} "
            f"({row.elapsed_s:.0f}s)"
        )
        rows.append(row)

    print_comparison_table(rows)
    print_scenario_details(rows)
    print(f"Suite elapsed: {time.perf_counter() - suite_t0:.1f}s")

    if not args.no_export and args.export_json:
        out = Path(args.export_json)
        payload = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "days": args.days,
            "seed": args.seed,
            "mc_runs": args.mc_runs,
            "fast_mode": bool(RUN_OPTIONS.fast_mode),
            "window": {
                "start": str(base.index[warmup].date()),
                "end": str(base.index[-1].date()),
                "sim_bars": len(base) - warmup,
            },
            "scenarios": [asdict(r) for r in rows],
        }
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
