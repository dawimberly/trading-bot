"""Strategy rating feedback + small paper parameter search (365d).

Paper-only research helper. Does not auto-apply .env changes.

Examples:
  # Refresh ratings from journals / strategy_metrics (fast)
  python scripts/analysis/run_strategy_rating_opt.py --days 365

  # Small 365d backtest grid (≤8 configs, fast-mode)
  python scripts/analysis/run_strategy_rating_opt.py --days 365 --backtest --fast-mode --sample 6

  # Enable feedback loop in paper .env (manual):
  #   STRATEGY_RATING_ENABLED=true
  #   STRATEGY_RATING_LIVE_ENABLED=false
"""

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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config

DEFAULT_EXPORT = Path(__file__).with_name("strategy_rating_opt_last.json")

# Small paper knob grid — keep ≤8 combos by default (sample).
PAPER_OPT_GRID: dict[str, list] = {
    "risk_per_trade": [0.014, 0.018, 0.022],
    "sector_rs_min": [-0.02, -0.01, 0.0],
    "stat_arb_adv": [40_000_000, 50_000_000],
    "regime_e_mult": [1.2, 1.4],
}


@dataclass
class OptResult:
    risk_per_trade: float
    sector_rs_min: float
    stat_arb_adv: float
    regime_e_mult: float
    total_return_pct: float
    sharpe: float
    max_drawdown_pct: float
    composite_score: float
    elapsed_sec: float


def composite_score(total_return_pct: float, sharpe: float, max_drawdown_pct: float) -> float:
    dd = abs(float(max_drawdown_pct)) or 1e-6
    calmar = float(total_return_pct) / dd
    return round(float(sharpe) * 0.45 + (float(total_return_pct) / 100.0) * 0.30 + calmar * 0.025, 4)


def iter_combos(grid: dict[str, list], *, sample: int | None, seed: int) -> list[dict]:
    keys = list(grid.keys())
    values = [grid[k] for k in keys]
    all_combos = [dict(zip(keys, combo)) for combo in itertools.product(*values)]
    if sample is None or sample >= len(all_combos):
        return all_combos
    return random.Random(seed).sample(all_combos, max(1, int(sample)))


def run_ratings_report(*, days: int) -> dict:
    from modules.strategy_rating import rankings_table, refresh_ratings, score_to_size_multiplier

    # Force rating path on for report (does not persist to .env).
    prev = bool(config.STRATEGY_RATING_ENABLED)
    config.STRATEGY_RATING_ENABLED = True
    try:
        snap = refresh_ratings(days=days, force=True)
        rows = rankings_table(days=days)
    finally:
        config.STRATEGY_RATING_ENABLED = prev

    print(f"\n=== Strategy rating feedback ({days}d lookback) ===")
    print(
        f"Enabled (config): {config.STRATEGY_RATING_ENABLED} | "
        f"Live: {config.STRATEGY_RATING_LIVE_ENABLED} | "
        f"Mult clip [{config.STRATEGY_RATING_MULT_MIN}, {config.STRATEGY_RATING_MULT_MAX}]"
    )
    print(f"Stack mult: x{float(snap.get('stack_mult') or 1.0):.3f}")
    print(
        f"{'Strategy':<28} {'Score':>5} {'Rating':<10} {'Trades':>6} "
        f"{'Ret%':>7} {'Sharpe':>6} {'Mult':>5}"
    )
    print("-" * 78)
    for row in rows:
        print(
            f"{str(row.get('label') or '')[:28]:<28} "
            f"{float(row.get('score') or 0):>5.0f} "
            f"{str(row.get('rating') or ''):<10} "
            f"{int(row.get('trade_count') or 0):>6} "
            f"{float(row.get('return_pct') or 0):>+7.1f} "
            f"{float(row.get('sharpe') or 0):>6.2f} "
            f"x{float(row.get('size_mult') or 1):>4.2f}"
        )
    # Sanity: empty / thin → 1.0
    assert score_to_size_multiplier(50.0, trade_count=0) == 1.0
    return {"snapshot": snap, "rows": rows}


def apply_opt_params(params: dict) -> None:
    risk = float(params["risk_per_trade"])
    config.RISK_PER_TRADE = risk
    config.PAPER_RISK_CALM_BULL_PCT = risk
    config.PAPER_RISK_MODERATE_PCT = round(risk * 0.75, 4)
    config.PAPER_RISK_STRESS_PCT = round(risk * 0.5, 4)
    config.SECTOR_RS_MIN = float(params["sector_rs_min"])
    config.PAPER_STAT_ARB_MIN_DOLLAR_VOLUME = float(params["stat_arb_adv"])
    config.PAPER_REGIME_E_SIZING_MULT = float(params["regime_e_mult"])


def run_backtest_opt(args: argparse.Namespace) -> list[OptResult]:
    from backtester import _ensure_daily_data, run_backtest, simulation_warmup_bars
    from modules.backtester_core import (
        RUN_OPTIONS,
        apply_default_execution_costs,
        apply_run_options_to_config,
        release_backtest_memory,
        reset_caches,
    )
    from modules.market_context import reset_regime_hysteresis
    import modules.market_context as mc
    import pandas as pd

    RUN_OPTIONS.fast_mode = bool(args.fast_mode)
    RUN_OPTIONS.no_thinking = True
    RUN_OPTIONS.full_accuracy = not RUN_OPTIONS.fast_mode
    apply_default_execution_costs()
    apply_run_options_to_config()

    if args.refresh:
        reset_caches(disk=True)

    days = int(args.days)
    raw = _ensure_daily_data(days, refresh=args.refresh, use_max=False)
    sim_bars = max(5, int(days * 0.80))
    warmup = simulation_warmup_bars(len(raw))
    desired = warmup + sim_bars
    data = raw.iloc[-desired:].copy() if len(raw) > desired else raw.copy()

    vti_core = max(0.0, min(1.0, float(args.vti_core)))
    if args.paper_aggressive and vti_core <= 0 and not config.PAPER_DYNAMIC_VTI_ENABLED:
        vti_core = config.PAPER_VTI_CORE_PCT
    bt_kwargs = {
        "track_spy_fill": False,
        "verbose": False,
        "vti_core_pct": vti_core,
        "paper_aggressive": bool(args.paper_aggressive),
        "small_account": False,
        "stat_arb_report": False,
        "paper_crypto_enabled": False,
    }

    combos = iter_combos(
        PAPER_OPT_GRID,
        sample=None if args.exhaustive else args.sample,
        seed=args.seed,
    )
    # Hard cap for v1 — avoid huge grids accidentally.
    if len(combos) > 8 and not args.exhaustive:
        combos = combos[:8]

    print(f"\n=== Paper param opt ({days}d, {len(combos)} configs, fast={RUN_OPTIONS.fast_mode}) ===")
    rows: list[OptResult] = []
    saved_announce = mc.announce_regime_change
    mc.announce_regime_change = lambda regime: regime
    t0 = time.perf_counter()
    try:
        for i, params in enumerate(combos, 1):
            apply_opt_params(params)
            reset_regime_hysteresis()
            release_backtest_memory(collect=False)
            t1 = time.perf_counter()
            result = run_backtest(data, **bt_kwargs)
            elapsed = time.perf_counter() - t1
            ret = float(result["total_return_pct"])
            sharpe = float(result["sharpe"])
            dd = float(result["max_drawdown_pct"])
            row = OptResult(
                risk_per_trade=float(params["risk_per_trade"]),
                sector_rs_min=float(params["sector_rs_min"]),
                stat_arb_adv=float(params["stat_arb_adv"]),
                regime_e_mult=float(params["regime_e_mult"]),
                total_return_pct=round(ret, 2),
                sharpe=round(sharpe, 3),
                max_drawdown_pct=round(dd, 2),
                composite_score=composite_score(ret, sharpe, dd),
                elapsed_sec=round(elapsed, 1),
            )
            rows.append(row)
            print(
                f"  [{i}/{len(combos)}] risk={row.risk_per_trade*100:.1f}% "
                f"rs={row.sector_rs_min:+.2f} adv=${row.stat_arb_adv/1e6:.0f}M "
                f"E={row.regime_e_mult:.1f} → ret={row.total_return_pct:+.1f}% "
                f"sh={row.sharpe:.2f} dd={row.max_drawdown_pct:.1f}% sc={row.composite_score:.3f}"
            )
    finally:
        mc.announce_regime_change = saved_announce
        release_backtest_memory()

    print(f"Elapsed {time.perf_counter() - t0:.0f}s")
    ranked = sorted(rows, key=lambda r: (-r.composite_score, -r.sharpe, -r.total_return_pct))
    if ranked:
        b = ranked[0]
        print("\nRecommended (paper research only — do not auto-apply to live):")
        print(f"  PAPER_RISK_CALM_BULL_PCT={b.risk_per_trade}")
        print(f"  SECTOR_RS_MIN={b.sector_rs_min}")
        print(f"  PAPER_STAT_ARB_MIN_DOLLAR_VOLUME={int(b.stat_arb_adv)}")
        print(f"  PAPER_REGIME_E_SIZING_MULT={b.regime_e_mult}")
    return ranked


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Strategy rating feedback + small paper parameter search"
    )
    p.add_argument("--days", type=int, default=365, help="Lookback / backtest window")
    p.add_argument(
        "--backtest",
        action="store_true",
        help="Run small 365d param grid (slow); default is journal ratings only",
    )
    p.add_argument("--paper-aggressive", action="store_true", default=True)
    p.add_argument("--no-paper-aggressive", dest="paper_aggressive", action="store_false")
    p.add_argument("--fast-mode", action="store_true", default=True)
    p.add_argument("--no-fast-mode", dest="fast_mode", action="store_false")
    p.add_argument("--sample", type=int, default=6, help="Max random configs (≤8)")
    p.add_argument("--exhaustive", action="store_true", help="Full grid (can be large)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--vti-core", type=float, default=0.0)
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--export", type=Path, default=DEFAULT_EXPORT)
    p.add_argument("--no-export", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    args.sample = min(8, max(1, int(args.sample)))

    rating_payload = run_ratings_report(days=int(args.days))
    opt_rows: list[OptResult] = []
    if args.backtest:
        opt_rows = run_backtest_opt(args)

    if not args.no_export:
        payload = {
            "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "days": int(args.days),
            "strategy_rating_enabled_default": bool(config.STRATEGY_RATING_ENABLED),
            "ratings": rating_payload.get("rows"),
            "stack_mult": (rating_payload.get("snapshot") or {}).get("stack_mult"),
            "opt_results": [asdict(r) for r in opt_rows],
            "how_to_enable": {
                "paper": "STRATEGY_RATING_ENABLED=true",
                "live": "STRATEGY_RATING_LIVE_ENABLED=true (keep false unless intentional)",
                "lookback": "STRATEGY_RATING_LOOKBACK_DAYS=365",
            },
        }
        args.export.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nExported: {args.export}")

    print(
        "\nEnable paper feedback: set STRATEGY_RATING_ENABLED=true in .env "
        "(STRATEGY_RATING_LIVE_ENABLED stays false)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
