"""Purged walk-forward validation for the paper research profile.

Splits the simulation window into N out-of-sample folds with an embargo gap
between train and test segments (see modules/backtester_core.walk_forward_purged).

Examples:
  python scripts/analysis/walk_forward.py --days 365 --paper-aggressive --walk-steps 4
  python scripts/analysis/walk_forward.py --days 365 --paper-aggressive --walk-steps 4 --vti-core 0.20
  python scripts/analysis/walk_forward.py --days 365 --paper-aggressive --paper-crypto --walk-steps 3
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from backtester import MIN_HISTORY, _ensure_daily_data, run_backtest
from modules.backtester_core import (
    RUN_OPTIONS,
    apply_default_execution_costs,
    apply_run_options_to_config,
    format_walk_forward_table,
    release_backtest_memory,
    reset_caches,
    walk_forward_purged,
)
from modules.console_output import print_table

DEFAULT_EXPORT = Path(__file__).with_name("walk_forward_last.json")


def _trim_data_window(data, days: int | None):
    sim_target_days = days or config.BACKTEST_DAYS
    target_sim_bars = max(5, int(sim_target_days * 0.80))
    n_bars = len(data)
    warmup = min(MIN_HISTORY, max(0, n_bars - 5))
    desired_total = warmup + target_sim_bars
    if len(data) > desired_total:
        return data.iloc[-desired_total:].copy()
    return data.copy()


def apply_run_options_from_args(args: argparse.Namespace) -> None:
    RUN_OPTIONS.fast_mode = bool(args.fast_mode)
    RUN_OPTIONS.no_thinking = bool(args.no_thinking)
    RUN_OPTIONS.realistic_costs = not args.no_realistic_costs
    if args.equity_slippage_bps is not None:
        RUN_OPTIONS.equity_slippage_bps = max(0.0, float(args.equity_slippage_bps))
    if args.crypto_slippage_bps is not None:
        RUN_OPTIONS.crypto_slippage_bps = max(0.0, float(args.crypto_slippage_bps))
    RUN_OPTIONS.equity_commission_bps = max(0.0, float(args.equity_commission_bps))
    RUN_OPTIONS.crypto_commission_bps = max(0.0, float(args.crypto_commission_bps))
    RUN_OPTIONS.full_accuracy = not RUN_OPTIONS.fast_mode
    apply_default_execution_costs()
    apply_run_options_to_config()


def build_backtest_kwargs(args: argparse.Namespace) -> dict:
    vti_core = max(0.0, min(1.0, args.vti_core))
    if args.small_account and vti_core <= 0:
        vti_core = config.SMALL_ACCOUNT_VTI_CORE_PCT
    elif args.paper_aggressive and vti_core <= 0 and not config.PAPER_DYNAMIC_VTI_ENABLED:
        vti_core = config.PAPER_VTI_CORE_PCT
    if args.no_nyse_conditional and args.paper_aggressive:
        config.PAPER_NYSE_CONDITIONAL_ON_SPY = False
    if args.paper_crypto:
        paper_crypto_enabled = True
    elif args.paper_aggressive:
        paper_crypto_enabled = False
    else:
        paper_crypto_enabled = None
    kwargs = {
        "track_spy_fill": False,
        "verbose": False,
        "vti_core_pct": vti_core,
        "paper_aggressive": bool(args.paper_aggressive),
        "small_account": bool(args.small_account),
        "stat_arb_report": False,
        "paper_crypto_enabled": paper_crypto_enabled,
    }
    if args.start_equity is not None:
        kwargs["live_thinking_start_equity"] = float(args.start_equity)
    return kwargs


def run_walk_forward(args: argparse.Namespace) -> int:
    apply_run_options_from_args(args)
    if args.refresh:
        reset_caches(disk=True)

    days = args.days or config.BACKTEST_DAYS
    n_folds = max(2, int(args.walk_steps))

    try:
        data = _ensure_daily_data(days, refresh=args.refresh, use_max=args.max)
    except Exception as exc:
        print(f"Database error: {exc}")
        return 1
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
        return 1

    base = _trim_data_window(data, days)
    warmup = min(MIN_HISTORY, max(0, len(base) - 5))
    start_date = base.index[warmup]
    end_date = base.index[-1]
    bt_kwargs = build_backtest_kwargs(args)

    if args.paper_aggressive and not args.paper_crypto:
        config.PAPER_CRYPTO_ENABLED = False
        config.PAPER_CRYPTO_V2_ENABLED = False

    profile = []
    if args.paper_aggressive:
        profile.append("paper-aggressive")
    if args.paper_crypto:
        profile.append("crypto")
    elif args.paper_aggressive:
        profile.append("equity-only")
    if args.small_account:
        profile.append("small-account")
    profile_label = ", ".join(profile) if profile else "default"

    print("=== Purged walk-forward ===")
    print(f"Profile: {profile_label}")
    print(
        f"Window: {start_date.date()} -> {end_date.date()} "
        f"({len(base) - warmup} sim bars, {warmup} warmup)"
    )
    print(f"Folds: {n_folds} | embargo: {args.embargo_bars} bars")

    if args.paper_aggressive:
        config.set_paper_aggressive_context(True)
        banner = config.format_research_mode_banner()
        if banner:
            print(f"--- {banner} ---")

    t0 = time.perf_counter()
    full = run_backtest(
        base,
        track_active_exposure=True,
        track_metrics=True,
        **bt_kwargs,
    )
    print(
        f"\nFull window: return {full['total_return_pct']:+.2f}% | "
        f"Sharpe {full['sharpe']:.2f} | max DD {full['max_drawdown_pct']:.2f}% | "
        f"orders {full.get('total_orders', 0)}"
    )

    release_backtest_memory(collect=False)
    wf = walk_forward_purged(
        base,
        min_history=MIN_HISTORY,
        n_folds=n_folds,
        embargo_bars=int(args.embargo_bars),
        run_fn=lambda d, _tb, _te: run_backtest(
            d,
            track_active_exposure=True,
            track_metrics=True,
            **bt_kwargs,
        ),
    )
    elapsed = time.perf_counter() - t0

    if not wf:
        print(
            "\nWalk-forward: insufficient bars for requested folds "
            f"(need ~{warmup + n_folds * 40}+ bars)."
        )
        return 1

    print_table(format_walk_forward_table(wf, purged=True), title="Purged walk-forward")

    avg_ret = sum(r["return_pct"] or 0 for r in wf) / len(wf)
    avg_sh = sum(r["sharpe"] or 0 for r in wf) / len(wf)
    avg_dd = sum(r["max_dd_pct"] or 0 for r in wf) / len(wf)
    print(
        f"\nOOS avg: return {avg_ret:+.2f}% | Sharpe {avg_sh:.2f} | "
        f"max DD {avg_dd:.2f}% | elapsed {elapsed:.1f}s"
    )

    if args.export_json:
        out_path = Path(args.export_json)
        payload = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "profile": profile_label,
            "days": days,
            "walk_steps": n_folds,
            "embargo_bars": int(args.embargo_bars),
            "window": {
                "start": str(start_date.date()),
                "end": str(end_date.date()),
                "sim_bars": len(base) - warmup,
            },
            "full_window": {
                "return_pct": full.get("total_return_pct"),
                "sharpe": full.get("sharpe"),
                "max_drawdown_pct": full.get("max_drawdown_pct"),
                "total_orders": full.get("total_orders"),
            },
            "folds": wf,
            "oos_avg": {
                "return_pct": round(avg_ret, 2),
                "sharpe": round(avg_sh, 2),
                "max_drawdown_pct": round(avg_dd, 2),
            },
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Exported: {out_path}")

    release_backtest_memory()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Purged walk-forward validation (paper research profile)",
    )
    parser.add_argument(
        "--walk-steps",
        type=int,
        default=4,
        metavar="N",
        help="Number of OOS folds (default: 4)",
    )
    parser.add_argument(
        "--embargo-bars",
        type=int,
        default=5,
        metavar="BARS",
        help="Embargo gap between train and test (default: 5)",
    )
    parser.add_argument(
        "--export-json",
        nargs="?",
        const=str(DEFAULT_EXPORT),
        default=None,
        metavar="PATH",
        help="Write fold results to JSON",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=config.BACKTEST_DAYS,
        help=f"Simulation length in calendar days (default: {config.BACKTEST_DAYS})",
    )
    parser.add_argument("--refresh", action="store_true", help="Re-download daily history")
    parser.add_argument("--max", action="store_true", help="Use max available daily history")
    parser.add_argument(
        "--vti-core",
        type=float,
        default=0.0,
        metavar="PCT",
        help="Passive VTI core fraction (paper default: PAPER_VTI_CORE_PCT)",
    )
    parser.add_argument(
        "--paper-aggressive",
        action="store_true",
        help="Paper research profile (crypto off unless --paper-crypto)",
    )
    parser.add_argument(
        "--paper-crypto",
        action="store_true",
        help="Enable PAPER_CRYPTO_ENABLED",
    )
    parser.add_argument("--small-account", action="store_true")
    parser.add_argument("--no-nyse-conditional", action="store_true")
    parser.add_argument("--fast-mode", action="store_true")
    parser.add_argument("--no-thinking", action="store_true")
    parser.add_argument("--no-realistic-costs", action="store_true")
    parser.add_argument("--equity-slippage-bps", type=float, default=None)
    parser.add_argument("--crypto-slippage-bps", type=float, default=None)
    parser.add_argument("--equity-commission-bps", type=float, default=0.0)
    parser.add_argument("--crypto-commission-bps", type=float, default=0.0)
    parser.add_argument("--start-equity", type=float, default=None)
    return parser


def main() -> int:
    return run_walk_forward(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
