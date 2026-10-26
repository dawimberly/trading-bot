"""Grid search backtest parameters — standard + tail-risk focused sweep."""

from __future__ import annotations

import argparse
import itertools
import json
import random
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from backtester import _ensure_daily_data, run_backtest, simulation_warmup_bars
from modules.backtester_core import (
    RUN_OPTIONS,
    apply_default_execution_costs,
    apply_run_options_to_config,
    release_backtest_memory,
    reset_caches,
)
from modules.market_context import reset_regime_hysteresis

DEFAULT_EXPORT = Path(__file__).with_name("parameter_tune_last.json")
DEFAULT_CSV = Path(__file__).with_name("parameter_tune_last.csv")
TAIL_EXPORT = Path(__file__).with_name("parameter_tune_tail_last.json")
TAIL_CSV = Path(__file__).with_name("parameter_tune_tail_last.csv")

FOCUSED_GRID: dict[str, list] = {
    "spy_ma_window": [150, 200, 250],
    "nyse_ma_window": [30, 50, 70],
    "risk_per_trade": [0.01, 0.022, 0.04],
    "regime_e_multiplier": [1.0, 1.4, 1.6],
    "max_hold_bars": [20, 45, 60],
}

WIDE_GRID: dict[str, list] = {
    "spy_ma_window": [150, 175, 200, 225, 250],
    "nyse_ma_window": [30, 45, 60, 75],
    "risk_per_trade": [0.01, 0.015, 0.022, 0.03, 0.04],
    "regime_e_multiplier": [1.0, 1.2, 1.4, 1.6],
    "max_hold_bars": [20, 30, 45, 60],
}

TAIL_GRID: dict[str, list] = {
    "risk_per_trade": [0.008, 0.014, 0.020],
    "regime_b_multiplier": [0.2, 0.35, 0.5],
    "max_hold_bars": [20, 30, 40],
    "vol_ceiling_pct": [0.12, 0.17, 0.22],
    "spy_ma_window": [140, 160, 180],
}


@dataclass
class TuneResult:
    spy_ma_window: int
    nyse_ma_window: int
    risk_per_trade: float
    regime_e_multiplier: float
    max_hold_bars: int
    total_return_pct: float
    sharpe: float
    max_drawdown_pct: float
    final_equity: float
    composite_score: float
    elapsed_sec: float


@dataclass
class TailTuneResult:
    risk_per_trade: float
    regime_b_multiplier: float
    max_hold_bars: int
    vol_ceiling_pct: float
    spy_ma_window: int
    total_return_pct: float
    sharpe: float
    p5_return_pct: float
    max_drawdown_pct: float
    composite_score: float
    elapsed_sec: float


def composite_score(total_return_pct: float, sharpe: float, max_drawdown_pct: float) -> float:
    dd = abs(float(max_drawdown_pct)) or 1e-6
    calmar = float(total_return_pct) / dd
    return round(float(sharpe) * 0.45 + (float(total_return_pct) / 100.0) * 0.30 + calmar * 0.025, 4)


def tail_composite_score(total_return_pct: float, sharpe: float, p5_return_pct: float) -> float:
    ret = float(total_return_pct) / 100.0
    p5 = abs(float(p5_return_pct)) / 100.0
    tail = 1.0 / (1.0 + p5)
    return round(float(sharpe) * 0.40 + ret * 0.30 + tail * 0.30, 4)


def _trim_data_window(data: pd.DataFrame, days: int | None) -> pd.DataFrame:
    sim_target_days = days or config.BACKTEST_DAYS
    target_sim_bars = max(5, int(sim_target_days * 0.80))
    n_bars = len(data)
    warmup = simulation_warmup_bars(n_bars)
    desired_total = warmup + target_sim_bars
    if len(data) > desired_total:
        return data.iloc[-desired_total:].copy()
    return data.copy()


def apply_run_options_from_args(args: argparse.Namespace) -> None:
    RUN_OPTIONS.fast_mode = bool(args.fast_mode)
    RUN_OPTIONS.no_thinking = True
    RUN_OPTIONS.full_accuracy = not RUN_OPTIONS.fast_mode
    apply_default_execution_costs()
    apply_run_options_to_config()


def apply_params(params: dict) -> None:
    config.SPY_MA_WINDOW = int(params["spy_ma_window"])
    config.PAPER_SPY_MA_WINDOW = int(params["spy_ma_window"])
    if "nyse_ma_window" in params:
        config.NYSE_MA_WINDOW = int(params["nyse_ma_window"])
        config.PAPER_NYSE_MA_WINDOW = int(params["nyse_ma_window"])
    risk = float(params["risk_per_trade"])
    config.RISK_PER_TRADE = risk
    config.PAPER_RISK_CALM_BULL_PCT = risk
    config.PAPER_RISK_MODERATE_PCT = round(risk * 0.75, 4)
    config.PAPER_RISK_STRESS_PCT = round(risk * 0.5, 4)
    if "regime_e_multiplier" in params:
        config.PAPER_REGIME_E_SIZING_MULT = float(params["regime_e_multiplier"])
    config.PAPER_POSITION_MAX_HOLD_BARS = int(params["max_hold_bars"])
    if "regime_b_multiplier" in params:
        config.PAPER_REGIME_B_SIZING_MULT = float(params["regime_b_multiplier"])
    if "vol_ceiling_pct" in params:
        config.VOL_CEILING_PCT = float(params["vol_ceiling_pct"])
        config.VOL_CEILING_ENABLED = True


def build_backtest_kwargs(args: argparse.Namespace) -> dict:
    vti_core = max(0.0, min(1.0, args.vti_core))
    if args.paper_aggressive and vti_core <= 0 and not config.PAPER_DYNAMIC_VTI_ENABLED:
        vti_core = config.PAPER_VTI_CORE_PCT
    return {
        "track_spy_fill": False,
        "verbose": False,
        "vti_core_pct": vti_core,
        "paper_aggressive": bool(args.paper_aggressive),
        "small_account": bool(args.small_account),
        "stat_arb_report": False,
        "paper_crypto_enabled": False if args.paper_aggressive else None,
    }


def iter_combos(grid: dict[str, list], *, sample: int | None, seed: int) -> list[dict]:
    keys = list(grid.keys())
    values = [grid[k] for k in keys]
    all_combos = [dict(zip(keys, combo)) for combo in itertools.product(*values)]
    if sample is None or sample >= len(all_combos):
        return all_combos
    return random.Random(seed).sample(all_combos, sample)


def _quiet_run(fn):
    import modules.market_context as mc

    saved = mc.announce_regime_change
    mc.announce_regime_change = lambda regime: regime
    try:
        return fn()
    finally:
        mc.announce_regime_change = saved


def mini_mc_p5_return(
    base: pd.DataFrame,
    bt_kwargs: dict,
    *,
    mc_runs: int,
    noise_level: float,
    regime_noise: float,
    seed: int,
) -> float:
    if mc_runs <= 0:
        return 0.0
    from scripts.analysis.monte_carlo_backtest import perturb_market_data

    rng = np.random.default_rng(seed)
    returns: list[float] = []
    for _ in range(mc_runs):
        perturbed, _, _ = perturb_market_data(
            base, noise_level=noise_level, regime_noise=regime_noise, rng=rng
        )
        release_backtest_memory(collect=False)
        reset_regime_hysteresis()
        result = _quiet_run(lambda: run_backtest(perturbed, **bt_kwargs))
        returns.append(float(result["total_return_pct"]))
    return round(float(np.percentile(returns, 5)), 2) if returns else 0.0


def run_one(data: pd.DataFrame, params: dict, bt_kwargs: dict) -> TuneResult:
    apply_params(params)
    reset_regime_hysteresis()
    release_backtest_memory(collect=False)
    t0 = time.perf_counter()
    result = _quiet_run(lambda: run_backtest(data, **bt_kwargs))
    elapsed = time.perf_counter() - t0
    ret = float(result["total_return_pct"])
    sharpe = float(result["sharpe"])
    dd = float(result["max_drawdown_pct"])
    return TuneResult(
        spy_ma_window=int(params["spy_ma_window"]),
        nyse_ma_window=int(params.get("nyse_ma_window", config.PAPER_NYSE_MA_WINDOW)),
        risk_per_trade=float(params["risk_per_trade"]),
        regime_e_multiplier=float(params.get("regime_e_multiplier", config.PAPER_REGIME_E_SIZING_MULT)),
        max_hold_bars=int(params["max_hold_bars"]),
        total_return_pct=round(ret, 2),
        sharpe=round(sharpe, 3),
        max_drawdown_pct=round(dd, 2),
        final_equity=round(float(result["final_equity"]), 2),
        composite_score=composite_score(ret, sharpe, dd),
        elapsed_sec=round(elapsed, 1),
    )


def run_tail_one(
    data: pd.DataFrame,
    params: dict,
    bt_kwargs: dict,
    *,
    mc_runs: int,
    mc_noise: float,
    mc_regime_noise: float,
    mc_seed: int,
    combo_index: int,
) -> TailTuneResult:
    apply_params(params)
    reset_regime_hysteresis()
    release_backtest_memory(collect=False)
    t0 = time.perf_counter()
    result = _quiet_run(lambda: run_backtest(data, **bt_kwargs))
    if mc_runs > 0:
        p5 = mini_mc_p5_return(
            data,
            bt_kwargs,
            mc_runs=mc_runs,
            noise_level=mc_noise,
            regime_noise=mc_regime_noise,
            seed=mc_seed + combo_index * 997,
        )
    else:
        # Fast tail proxy when mini-MC skipped (use path max DD as left-tail stand-in).
        p5 = float(result["max_drawdown_pct"])
    elapsed = time.perf_counter() - t0
    ret = float(result["total_return_pct"])
    sharpe = float(result["sharpe"])
    dd = float(result["max_drawdown_pct"])
    return TailTuneResult(
        risk_per_trade=float(params["risk_per_trade"]),
        regime_b_multiplier=float(params["regime_b_multiplier"]),
        max_hold_bars=int(params["max_hold_bars"]),
        vol_ceiling_pct=float(params["vol_ceiling_pct"]),
        spy_ma_window=int(params["spy_ma_window"]),
        total_return_pct=round(ret, 2),
        sharpe=round(sharpe, 3),
        p5_return_pct=p5,
        max_drawdown_pct=round(dd, 2),
        composite_score=tail_composite_score(ret, sharpe, p5),
        elapsed_sec=round(elapsed, 1),
    )


def print_ranked_table(rows: list[TuneResult], *, top_n: int = 20) -> None:
    ranked = sorted(rows, key=lambda r: (-r.composite_score, -r.sharpe, -r.total_return_pct))
    print(f"\n=== Top {min(top_n, len(ranked))} parameter combos ===")
    for i, row in enumerate(ranked[:top_n], start=1):
        print(
            f"{i:>2} SPY={row.spy_ma_window} NYSE={row.nyse_ma_window} "
            f"risk={row.risk_per_trade*100:.1f}% E={row.regime_e_multiplier} "
            f"hold={row.max_hold_bars} ret={row.total_return_pct:+.1f}% "
            f"sh={row.sharpe:.2f} dd={row.max_drawdown_pct:.1f}% sc={row.composite_score:.3f}"
        )


def print_tail_ranked_table(rows: list[TailTuneResult], *, top_n: int = 8) -> None:
    ranked = sorted(rows, key=lambda r: (-r.composite_score, -r.sharpe, r.p5_return_pct))
    print(f"\n=== Top {min(top_n, len(ranked))} tail-tune combos ===")
    header = (
        f"{'#':>2} {'Risk%':>5} {'RHYME_B':>6} {'Hold':>4} {'VolCap':>6} "
        f"{'SPY':>4} {'Ret%':>7} {'Sharpe':>6} {'p5 Ret%':>8} {'Score':>6}"
    )
    print(header)
    print("-" * len(header))
    for i, row in enumerate(ranked[:top_n], start=1):
        print(
            f"{i:>2} {row.risk_per_trade*100:>4.1f}% {row.regime_b_multiplier:>6.2f} "
            f"{row.max_hold_bars:>4} {row.vol_ceiling_pct:>6.0%} {row.spy_ma_window:>4} "
            f"{row.total_return_pct:>7.2f} {row.sharpe:>6.2f} {row.p5_return_pct:>8.2f} "
            f"{row.composite_score:>6.3f}"
        )
    if ranked:
        b = ranked[0]
        print(
            f"\nRecommended balanced default:\n"
            f"  PAPER_RISK_CALM_BULL_PCT={b.risk_per_trade}\n"
            f"  PAPER_REGIME_B_SIZING_MULT={b.regime_b_multiplier}\n"
            f"  PAPER_POSITION_MAX_HOLD_BARS={b.max_hold_bars}\n"
            f"  VOL_CEILING_PCT={b.vol_ceiling_pct}\n"
            f"  PAPER_SPY_MA_WINDOW={b.spy_ma_window}"
        )


def run_tail_tune(args: argparse.Namespace) -> int:
    apply_run_options_from_args(args)
    if args.refresh:
        reset_caches(disk=True)
    if args.paper_aggressive:
        config.PAPER_CRYPTO_ENABLED = False
        config.PAPER_CRYPTO_V2_ENABLED = False
    days = args.days or config.BACKTEST_DAYS
    raw = _ensure_daily_data(days, refresh=args.refresh, use_max=args.max)
    data = _trim_data_window(raw, days)
    bt_kwargs = build_backtest_kwargs(args)
    combos = iter_combos(TAIL_GRID, sample=None if args.exhaustive else args.sample, seed=args.seed)
    mc_runs = max(0, args.mc_runs_per_combo)
    print("=== Tail-risk parameter tuning ===")
    print(f"Combos={len(combos)} mc_runs/combo={mc_runs} fast={RUN_OPTIONS.fast_mode}")
    rows: list[TailTuneResult] = []
    t0 = time.perf_counter()
    for i, params in enumerate(combos, 1):
        rows.append(
            run_tail_one(
                data, params, bt_kwargs,
                mc_runs=mc_runs, mc_noise=args.mc_noise, mc_regime_noise=args.mc_regime_noise,
                mc_seed=args.seed, combo_index=i,
            )
        )
        if args.progress and (i % max(1, len(combos) // 10) == 0 or i == len(combos)):
            print(f"  ... {i}/{len(combos)} score={rows[-1].composite_score:.3f} p5={rows[-1].p5_return_pct:+.1f}%")
    print(f"Elapsed {time.perf_counter()-t0:.0f}s")
    print_tail_ranked_table(rows, top_n=args.top)
    if not args.no_export:
        ranked = sorted(rows, key=lambda r: -r.composite_score)
        TAIL_EXPORT.write_text(json.dumps({"results": [asdict(r) for r in ranked]}, indent=2))
        pd.DataFrame([asdict(r) for r in rows]).sort_values("composite_score", ascending=False).to_csv(TAIL_CSV, index=False)
        print(f"Exported: {TAIL_EXPORT}\nCSV: {TAIL_CSV}")
    release_backtest_memory()
    return 0


def run_tune(args: argparse.Namespace) -> int:
    apply_run_options_from_args(args)
    if args.refresh:
        reset_caches(disk=True)
    grid = WIDE_GRID if args.wide else FOCUSED_GRID
    if args.paper_aggressive:
        config.PAPER_CRYPTO_ENABLED = False
    raw = _ensure_daily_data(days=args.days or config.BACKTEST_DAYS, refresh=args.refresh, use_max=args.max)
    data = _trim_data_window(raw, args.days)
    bt_kwargs = build_backtest_kwargs(args)
    combos = iter_combos(grid, sample=None if args.exhaustive else args.sample, seed=args.seed)
    rows = [run_one(data, p, bt_kwargs) for p in combos]
    print_ranked_table(rows, top_n=args.top)
    release_backtest_memory()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Grid-search backtest parameters")
    p.add_argument("--days", type=int, default=config.BACKTEST_DAYS)
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--max", action="store_true")
    p.add_argument("--paper-aggressive", action="store_true")
    p.add_argument("--small-account", action="store_true")
    p.add_argument("--tail-tune", action="store_true")
    p.add_argument("--vti-core", type=float, default=0.0)
    p.add_argument("--wide", action="store_true")
    p.add_argument("--exhaustive", action="store_true")
    p.add_argument("--sample", type=int, default=48)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--top", type=int, default=8)
    p.add_argument("--fast-mode", action="store_true")
    p.add_argument("--no-export", action="store_true")
    p.add_argument("--no-progress", dest="progress", action="store_false")
    p.add_argument("--mc-runs-per-combo", type=int, default=0, help="Mini-MC p5 (0=max-DD proxy)")
    p.add_argument("--mc-noise", type=float, default=0.01)
    p.add_argument("--mc-regime-noise", type=float, default=0.10)
    p.set_defaults(progress=True)
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.tail_tune:
        if args.sample == 48:
            args.sample = 36
        return run_tail_tune(args)
    return run_tune(args)


if __name__ == "__main__":
    raise SystemExit(main())
