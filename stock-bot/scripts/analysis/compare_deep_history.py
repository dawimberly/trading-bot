"""Compare baseline vs deep-history-indicators-only across full window and walk-forward folds.

Examples:
  python scripts/analysis/compare_deep_history.py --days 365 --paper-aggressive
  python scripts/analysis/compare_deep_history.py --days 365 --paper-aggressive --walk-forward 4
  python scripts/analysis/compare_deep_history.py --days 365 --paper-aggressive --walk-forward 4 --fast-mode
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from backtester import (
    MIN_HISTORY,
    _build_indicator_context,
    _ensure_daily_data,
    _trim_baseline_backtest_data,
    run_backtest,
)
from modules.backtester_core import (
    PURGE_EMBARGO_BARS,
    RUN_OPTIONS,
    apply_default_execution_costs,
    apply_run_options_to_config,
    release_backtest_memory,
    reset_caches,
)
from modules.core_allocator import reset_core_allocator_state

DEFAULT_CSV = Path(__file__).with_name("compare_deep_history_last.csv")
DEFAULT_JSON = Path(__file__).with_name("compare_deep_history_last.json")


def _target_sim_bars(days: int, available_bars: int) -> int:
    want = max(5, int(days * 0.80))
    warmup = min(MIN_HISTORY, max(0, available_bars - 5))
    return min(want, max(5, available_bars - warmup))


def _prepare_run_data(
    full_data,
    *,
    days: int,
    deep_indicators_only: bool,
    max_years: int,
    refresh: bool,
):
    """Return (trade_data, indicator_context, allocator_data) for one mode."""
    if full_data is None or full_data.empty:
        return full_data, None, None

    target = _target_sim_bars(days, len(full_data))
    if not deep_indicators_only:
        trade = _trim_baseline_backtest_data(full_data, target_sim_bars=target)
        return trade, None, None

    allocator_data = _trim_baseline_backtest_data(full_data, target_sim_bars=target)
    trade = (
        full_data.iloc[-target:].copy()
        if len(full_data) > target
        else full_data.copy()
    )
    indicator_context = _build_indicator_context(
        trade, max_years=max_years, refresh=refresh
    )
    if indicator_context is None or getattr(indicator_context, "empty", True):
        trade = allocator_data
        return trade, None, None
    return trade, indicator_context, allocator_data


def _run_mode(
    full_data,
    *,
    days: int,
    deep_indicators_only: bool,
    max_years: int,
    refresh: bool,
    bt_kwargs: dict,
) -> dict:
    reset_core_allocator_state()
    trade, indicator_context, allocator_data = _prepare_run_data(
        full_data,
        days=days,
        deep_indicators_only=deep_indicators_only,
        max_years=max_years,
        refresh=refresh,
    )
    if trade is None or len(trade) < 20:
        return {}

    config.DEEP_HISTORY_ENABLED = bool(deep_indicators_only and indicator_context is not None)
    config.DEEP_HISTORY_INDICATORS_ONLY = bool(
        deep_indicators_only and indicator_context is not None
    )
    result = run_backtest(
        trade,
        verbose=False,
        track_metrics=True,
        indicator_context=indicator_context,
        allocator_data=allocator_data,
        deep_history_indicators_only=deep_indicators_only,
        max_years=max_years,
        **bt_kwargs,
    )
    warmup = 0 if indicator_context is not None else min(MIN_HISTORY, max(0, len(trade) - 5))
    return {
        "return_pct": float(result.get("total_return_pct", 0.0)),
        "sharpe": float(result.get("sharpe", 0.0)),
        "sortino": float(result.get("sortino", 0.0)),
        "max_drawdown_pct": float(result.get("max_drawdown_pct", 0.0)),
        "sim_bars": max(0, len(trade) - warmup),
        "warmup_bars": warmup,
        "start": str(trade.index[warmup].date()) if len(trade) > warmup else None,
        "end": str(trade.index[-1].date()) if len(trade) else None,
        "core_allocator": result.get("core_allocator"),
    }


def _comparison_row(
    label: str,
    baseline: dict,
    deep: dict,
) -> dict:
    b_ret = float(baseline.get("return_pct", 0.0))
    d_ret = float(deep.get("return_pct", 0.0))
    b_sh = float(baseline.get("sharpe", 0.0))
    d_sh = float(deep.get("sharpe", 0.0))
    return {
        "window_fold": label,
        "baseline_return_pct": round(b_ret, 2),
        "baseline_sharpe": round(b_sh, 2),
        "deep_indicators_return_pct": round(d_ret, 2),
        "deep_indicators_sharpe": round(d_sh, 2),
        "delta_return_pct": round(d_ret - b_ret, 2),
        "delta_sharpe": round(d_sh - b_sh, 2),
        "baseline": baseline,
        "deep_indicators": deep,
    }


def _print_table(rows: list[dict]) -> None:
    print(
        "\n| Window/Fold | Baseline Return | Baseline Sharpe | "
        "Deep-Indicators Return | Deep-Indicators Sharpe | Delta |"
    )
    print(
        "|-------------|-----------------|-----------------|------------------------|------------------------|-------|"
    )
    for row in rows:
        print(
            f"| {row['window_fold']:<11} "
            f"| {row['baseline_return_pct']:+13.2f}% "
            f"| {row['baseline_sharpe']:15.2f} "
            f"| {row['deep_indicators_return_pct']:+22.2f}% "
            f"| {row['deep_indicators_sharpe']:22.2f} "
            f"| {row['delta_return_pct']:+5.2f} |"
        )


def _walk_forward_rows(
    base_data,
    *,
    days: int,
    n_folds: int,
    max_years: int,
    refresh: bool,
    bt_kwargs: dict,
    embargo_bars: int,
) -> list[dict]:
    if n_folds < 2 or len(base_data) < MIN_HISTORY + n_folds * 40:
        return []

    sim_start = min(MIN_HISTORY, max(0, len(base_data) - 5))
    sim_end = len(base_data)
    sim_len = sim_end - sim_start
    test_size = sim_len // n_folds
    rows: list[dict] = []

    for fold in range(n_folds):
        test_begin = sim_start + fold * test_size
        test_end = sim_start + (fold + 1) * test_size if fold < n_folds - 1 else sim_end
        train_end = max(sim_start, test_begin - embargo_bars)
        if test_end - test_begin < 15 or train_end < sim_start:
            continue

        fold_data = base_data.iloc[:test_end].copy()
        label = f"Fold {fold + 1}"
        print(f"\n--- {label}: {fold_data.index[test_begin].date()} -> {fold_data.index[test_end - 1].date()} ---")
        baseline = _run_mode(
            fold_data,
            days=days,
            deep_indicators_only=False,
            max_years=max_years,
            refresh=refresh,
            bt_kwargs=bt_kwargs,
        )
        deep = _run_mode(
            fold_data,
            days=days,
            deep_indicators_only=True,
            max_years=max_years,
            refresh=False,
            bt_kwargs=bt_kwargs,
        )
        rows.append(_comparison_row(label, baseline, deep))
        release_backtest_memory(collect=False)

    return rows


def apply_run_options_from_args(args: argparse.Namespace) -> None:
    RUN_OPTIONS.fast_mode = bool(args.fast_mode)
    RUN_OPTIONS.no_thinking = bool(args.no_thinking)
    RUN_OPTIONS.realistic_costs = not args.no_realistic_costs
    RUN_OPTIONS.full_accuracy = not RUN_OPTIONS.fast_mode
    apply_default_execution_costs()
    apply_run_options_to_config()


def build_backtest_kwargs(args: argparse.Namespace) -> dict:
    if args.paper_aggressive:
        config.set_paper_aggressive_context(True)
        config.set_backtest_paper_sleeves_context(True)
        config.PAPER_CRYPTO_ENABLED = False
        config.PAPER_CRYPTO_V2_ENABLED = False
    return {
        "paper_aggressive": bool(args.paper_aggressive),
        "stat_arb_report": False,
        "paper_crypto_enabled": False if args.paper_aggressive else None,
    }


def save_results(rows: list[dict], *, csv_path: Path, json_path: Path, meta: dict) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "window_fold",
        "baseline_return_pct",
        "baseline_sharpe",
        "deep_indicators_return_pct",
        "deep_indicators_sharpe",
        "delta_return_pct",
        "delta_sharpe",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in fieldnames})

    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        **meta,
        "rows": rows,
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nSaved: {csv_path}")
    print(f"Saved: {json_path}")


def run_compare(args: argparse.Namespace) -> int:
    apply_run_options_from_args(args)
    if args.refresh:
        reset_caches(disk=True)

    days = int(args.days or config.BACKTEST_DAYS)
    n_folds = max(0, int(args.walk_forward))
    max_years = max(1, int(args.max_years))
    bt_kwargs = build_backtest_kwargs(args)

    print("=== Deep history comparison (baseline vs indicators-only) ===")
    print(f"Days: {days} | Walk-forward folds: {n_folds} | Max-years: {max_years}")
    if args.paper_aggressive:
        print("Profile: paper-aggressive")
    if args.fast_mode:
        print("Fast mode: ON (reduced universe / sleeves)")

    try:
        full_data = _ensure_daily_data(
            days, refresh=args.refresh, use_max=args.max
        )
    except Exception as exc:
        print(f"Database error: {exc}")
        return 1
    if len(full_data) < 20:
        print(f"Need at least 20 daily bars; got {len(full_data)}.")
        return 1

    target = _target_sim_bars(days, len(full_data))
    base_data = _trim_baseline_backtest_data(full_data, target_sim_bars=target)
    warmup = min(MIN_HISTORY, max(0, len(base_data) - 5))
    t0 = time.perf_counter()
    rows: list[dict] = []

    print(
        f"\nBase window: {base_data.index[warmup].date()} -> {base_data.index[-1].date()} "
        f"({len(base_data) - warmup} sim bars, {warmup} warmup)"
    )

    print("\n--- Full window ---")
    baseline_full = _run_mode(
        base_data,
        days=days,
        deep_indicators_only=False,
        max_years=max_years,
        refresh=args.refresh,
        bt_kwargs=bt_kwargs,
    )
    deep_full = _run_mode(
        base_data,
        days=days,
        deep_indicators_only=True,
        max_years=max_years,
        refresh=args.refresh,
        bt_kwargs=bt_kwargs,
    )
    rows.append(_comparison_row("Full window", baseline_full, deep_full))
    release_backtest_memory(collect=False)

    if n_folds >= 2:
        fold_rows = _walk_forward_rows(
            base_data,
            days=days,
            n_folds=n_folds,
            max_years=max_years,
            refresh=args.refresh,
            bt_kwargs=bt_kwargs,
            embargo_bars=int(args.embargo_bars),
        )
        if not fold_rows:
            print(
                "\nWalk-forward: insufficient bars for requested folds "
                f"(need ~{warmup + n_folds * 40}+ bars)."
            )
        rows.extend(fold_rows)

    _print_table(rows)

    avg_delta = sum(r["delta_return_pct"] for r in rows) / len(rows) if rows else 0.0
    avg_delta_sh = sum(r["delta_sharpe"] for r in rows) / len(rows) if rows else 0.0
    elapsed = time.perf_counter() - t0
    print(
        f"\nAvg delta (deep - baseline): return {avg_delta:+.2f} pp | "
        f"Sharpe {avg_delta_sh:+.2f} | elapsed {elapsed:.1f}s"
    )

    meta = {
        "days": days,
        "walk_forward_folds": n_folds,
        "max_years": max_years,
        "paper_aggressive": bool(args.paper_aggressive),
        "fast_mode": bool(args.fast_mode),
        "window": {
            "start": str(base_data.index[warmup].date()),
            "end": str(base_data.index[-1].date()),
            "sim_bars": len(base_data) - warmup,
            "warmup_bars": warmup,
        },
        "avg_delta_return_pct": round(avg_delta, 2),
        "avg_delta_sharpe": round(avg_delta_sh, 2),
        "elapsed_sec": round(elapsed, 1),
    }
    save_results(rows, csv_path=Path(args.csv_out), json_path=Path(args.json_out), meta=meta)
    release_backtest_memory()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare baseline vs deep-history-indicators-only backtests",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=config.BACKTEST_DAYS,
        help=f"Simulation length in calendar days (default: {config.BACKTEST_DAYS})",
    )
    parser.add_argument(
        "--walk-forward",
        type=int,
        default=4,
        metavar="N",
        help="Purged walk-forward OOS folds (default: 4; use 0 for full window only)",
    )
    parser.add_argument(
        "--paper-aggressive",
        action="store_true",
        help="Paper research profile (crypto off)",
    )
    parser.add_argument(
        "--fast-mode",
        action="store_true",
        help="Fast mode: smaller universe, thinking/stat-arb/vol/options off",
    )
    parser.add_argument("--refresh", action="store_true", help="Refresh daily + deep caches")
    parser.add_argument("--max", action="store_true", help="Use max available daily history")
    parser.add_argument(
        "--max-years",
        type=int,
        default=20,
        help="Deep indicator lookback cap (default: 20)",
    )
    parser.add_argument(
        "--embargo-bars",
        type=int,
        default=PURGE_EMBARGO_BARS,
        help=f"Walk-forward embargo gap (default: {PURGE_EMBARGO_BARS})",
    )
    parser.add_argument("--no-thinking", action="store_true")
    parser.add_argument("--no-realistic-costs", action="store_true")
    parser.add_argument(
        "--csv-out",
        default=str(DEFAULT_CSV),
        help=f"CSV export path (default: {DEFAULT_CSV.name})",
    )
    parser.add_argument(
        "--json-out",
        default=str(DEFAULT_JSON),
        help=f"JSON export path (default: {DEFAULT_JSON.name})",
    )
    return parser


def main() -> int:
    return run_compare(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
