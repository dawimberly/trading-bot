"""Run wisdom self-evaluation now (same logic as daily run_all hook).

Run:  python scripts/maintenance/evaluate_wisdom.py
      python scripts/maintenance/evaluate_wisdom.py --force
      python scripts/maintenance/evaluate_wisdom.py --monthly --force
      python scripts/maintenance/evaluate_wisdom.py --monthly --month 2026-04
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from modules.wisdom_evaluator import maybe_run_monthly_rollup, run_evaluation, run_monthly_rollup


def main() -> None:
    parser = argparse.ArgumentParser(description="Wisdom rolling self-evaluation")
    parser.add_argument("--force", action="store_true", help="Re-run even if already done today")
    parser.add_argument("--monthly", action="store_true", help="Calendar-month rollup (not daily)")
    parser.add_argument(
        "--month",
        help="Roll up specific month YYYY-MM (with --monthly --force)",
    )
    args = parser.parse_args()

    if args.monthly:
        if args.month:
            year, month = args.month.split("-")
            scorecard = run_monthly_rollup(int(year), int(month), force=True)
        else:
            scorecard = maybe_run_monthly_rollup(force=args.force)
    else:
        scorecard = run_evaluation(force=args.force)

    if scorecard is None:
        print("Evaluation disabled or already up to date.")
        return
    print(json.dumps(scorecard, indent=2))


if __name__ == "__main__":
    main()
