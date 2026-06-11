"""Cloud bot backtests — uses parent repo backtester with best-paper profile."""

from __future__ import annotations

import sys
from pathlib import Path

from cloud_bot.config.profile import CLOUD_BACKTEST_KWARGS, apply_to_config_module
from cloud_bot.config.settings import REPO_ROOT


def _ensure_repo_path() -> None:
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def run_compare(*, days: int | None = 365, use_max: bool = False, refresh: bool = False) -> int:
    """Run final-style comparison for one window (best paper vs legacy vs VTI)."""
    _ensure_repo_path()
    apply_to_config_module()

    from backtester import (
        FINAL_PAPER_BOT_KWARGS,
        LEGACY_PAPER_KWARGS,
        MIN_HISTORY,
        _benchmark_return,
        _ensure_daily_data,
        _format_final_table,
        _result_row,
        _run_final_window,
        run_backtest,
    )

    import config

    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
        label = "max"
    else:
        d = days or 365
        data = _ensure_daily_data(d, refresh=refresh, use_max=False)
        label = f"{d}d"

    if len(data) < MIN_HISTORY:
        print(f"Need {MIN_HISTORY} bars; got {len(data)}.")
        return 1

    print(f"--- CLOUD BOT BACKTEST ({label}) — Best Paper stack ---")
    window = _run_final_window(data, window_label=label)
    print(_format_final_table(window["rows"]))

    bench = window["vti_benchmark_pct"]
    final = window["final"]
    print(
        f"\nCloud profile: {final['total_return_pct']:+.2f}% | "
        f"Sharpe {final['sharpe']:.2f} | MaxDD {final['max_drawdown_pct']:.2f}%"
    )
    if bench is not None:
        print(f"VTI benchmark: {bench:+.2f}% ({final['total_return_pct'] - bench:+.2f} pp)")

    return 0


def run_single(*, days: int | None = 365, use_max: bool = False, refresh: bool = False) -> int:
    """Run best-paper backtest only (no compare table)."""
    _ensure_repo_path()
    apply_to_config_module()

    from backtester import MIN_HISTORY, _ensure_daily_data, run_backtest

    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        data = _ensure_daily_data(days or 365, refresh=refresh, use_max=False)

    if len(data) < MIN_HISTORY:
        print(f"Need {MIN_HISTORY} bars; got {len(data)}.")
        return 1

    result = run_backtest(
        data, track_metrics=True, track_active_exposure=True, **CLOUD_BACKTEST_KWARGS
    )
    print(
        f"Return {result['total_return_pct']:+.2f}% | "
        f"Sharpe {result['sharpe']:.2f} | "
        f"MaxDD {result['max_drawdown_pct']:.2f}% | "
        f"Pairs {result.get('pairs_traded', 0)}"
    )
    return 0
