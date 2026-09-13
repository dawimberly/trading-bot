"""Run baseline vs deep-history vs deep-indicators-only and print comparison table."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from backtester import run_performance_test
from modules import backtester_core
from modules.core_allocator import reset_core_allocator_state


def _run(label: str, **kwargs) -> dict:
    print(f"\n{'=' * 72}\n{label}\n{'=' * 72}")
    reset_core_allocator_state()
    run_performance_test(days=365, paper_aggressive=True, refresh=False, **kwargs)
    result = dict(backtester_core.LAST_BACKTEST_RESULT or {})
    core = result.get("core_allocator") or {}
    choice = str(core.get("choice", "?")).upper()
    pct = float(core.get("vti_pct", 0.0))
    result["_core_label"] = f"{choice} @ {pct:.0%}"
    return result


def main() -> None:
    config.DYNAMIC_CORE_ENABLED = True
    rows = [
        ("Baseline (no deep)", _run("Baseline", deep_history=False)),
        (
            "Deep (full)",
            _run("Deep full", deep_history=True, deep_history_indicators_only=False),
        ),
        (
            "Deep (indicators-only)",
            _run(
                "Deep indicators-only",
                deep_history=True,
                deep_history_indicators_only=True,
            ),
        ),
    ]

    print("\n| Metric | Baseline (no deep) | Deep (full) | Deep (indicators-only) |")
    print("|--------|-------------------|-------------|------------------------|")
    metrics = [
        ("Total return", "total_return_pct", "{:+.2f}%"),
        ("Sharpe", "sharpe", "{:.2f}"),
        ("Max drawdown", "max_drawdown_pct", "{:.2f}%"),
        ("Core allocator", "_core_label", "{}"),
    ]
    for label, key, fmt in metrics:
        vals = [fmt.format(r.get(key, 0)) for _, r in rows]
        print(f"| {label} | {vals[0]} | {vals[1]} | {vals[2]} |")


if __name__ == "__main__":
    main()
