"""Run VTI level + thinking compares (365d) with quiet logging."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING)
for name in ("modules.stat_arb_sleeve", "root", "events"):
    logging.getLogger(name).setLevel(logging.WARNING)

import config  # noqa: E402
from backtester import (  # noqa: E402
    run_compare_vti_levels,
    run_simulate_live_thinking_compare,
)


def main() -> None:
    days = 365
    print("=== PAPER AGGRESSIVE + THINKING @ FIXED VTI ===")
    run_compare_vti_levels(days=days, refresh=False, use_max=False)
    print()
    print("=== LIVE SMALL-ACCOUNT + THINKING @ VTI LEVELS ===")
    run_simulate_live_thinking_compare(
        days=days,
        refresh=False,
        use_max=False,
        start_equity=config.SMALL_ACCOUNT_BACKTEST_EQUITY,
        vti_levels=True,
    )


if __name__ == "__main__":
    main()
