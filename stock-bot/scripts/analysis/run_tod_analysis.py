#!/usr/bin/env python3
"""365-day time-of-day predictability analysis (Realistic Research).

Dedicated path — does not run the full paper backtest stack.

Usage:
  python scripts/analysis/run_tod_analysis.py
  python scripts/analysis/run_tod_analysis.py --days 365
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Time-of-day 365d analysis")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument(
        "--out",
        type=str,
        default=str(ROOT / "scripts" / "analysis" / "_tod_analysis_365.log"),
    )
    args = parser.parse_args()

    from modules.time_of_day import format_tod_report, run_full_tod_analysis

    print(f"Running TOD analysis ({args.days}d)…", flush=True)
    summary = run_full_tod_analysis(days=int(args.days))
    report = format_tod_report(summary)
    print(report)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report + "\n", encoding="utf-8")
    print(f"\nSaved: {out}", flush=True)
    print(f"Cache: {ROOT / 'data' / 'tod_analysis_cache.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
