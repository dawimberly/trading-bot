"""Monte Carlo A/B: v1.1c paper with vs without opportunistic shorts."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from backtester import MIN_HISTORY, _ensure_daily_data, run_backtest
from modules.backtester_core import RUN_OPTIONS, release_backtest_memory
from scripts.analysis.monte_carlo_backtest import (
    _percentile_table,
    _trim_data_window,
    perturb_market_data,
)


def main() -> int:
    RUN_OPTIONS.fast_mode = True
    RUN_OPTIONS.no_thinking = True
    RUN_OPTIONS.full_accuracy = False

    days = 365
    mc_runs = 10
    data = _ensure_daily_data(days, refresh=False, use_max=False)
    base = _trim_data_window(data, days)
    bt_kwargs = {
        "track_spy_fill": False,
        "verbose": False,
        "paper_aggressive": True,
        "stat_arb_report": False,
    }
    rng = np.random.default_rng(42)

    saved_short = config.PROTECTIVE_SHORT_ENABLED
    saved_opp = config.SHORT_OPPORTUNISTIC_ENABLED

    try:
        for label, short_on in [("shorts OFF", False), ("shorts ON (15%)", True)]:
            config.PROTECTIVE_SHORT_ENABLED = short_on
            config.SHORT_OPPORTUNISTIC_ENABLED = short_on
            rets: list[float] = []
            print(f"=== Monte Carlo {label} ({mc_runs} runs, fast-mode) ===")
            t0 = time.perf_counter()
            for _ in range(mc_runs):
                perturbed, _, _ = perturb_market_data(
                    base, noise_level=0.01, regime_noise=0.1, rng=rng
                )
                release_backtest_memory(collect=False)
                result = run_backtest(perturbed, **bt_kwargs)
                rets.append(float(result["total_return_pct"]))
            arr = np.array(rets, dtype=float)
            t = _percentile_table(arr, "Return %")
            print(
                f"p5={t['p5']:.2f}% median={t['median']:.2f}% "
                f"mean={t['mean']:.2f}% p95={t['p95']:.2f}%"
            )
            print(f"elapsed {time.perf_counter() - t0:.0f}s\n")
    finally:
        config.PROTECTIVE_SHORT_ENABLED = saved_short
        config.SHORT_OPPORTUNISTIC_ENABLED = saved_opp
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
