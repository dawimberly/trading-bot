"""Send the Friday weekly summary to Telegram.

  python scripts/weekly_telegram_summary.py --test
  python scripts/weekly_telegram_summary.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from modules.weekly_telegram_summary import send_weekly_telegram_summary, weekly_telegram_due


def main() -> int:
    from modules.logging_utils import setup_project_logging

    setup_project_logging()
    parser = argparse.ArgumentParser(description="Friday weekly Telegram summary")
    parser.add_argument("--test", action="store_true", help="Send now (skip schedule gate)")
    parser.add_argument("--dry-run", action="store_true", help="Print only; do not send")
    args = parser.parse_args()

    if not config.telegram_weekly_summary_enabled() and not args.test:
        print("Weekly Telegram disabled (TELEGRAM_WEEKLY_SUMMARY_ENABLED / live book).")
        return 0

    if not args.test and not args.dry_run and not weekly_telegram_due(market_open=False):
        print(
            f"Not due: Friday after {config.TELEGRAM_WEEKLY_SUMMARY_TIME} ET "
            "with market closed, or already sent this week. Use --test."
        )
        return 0

    ok = send_weekly_telegram_summary(
        test_mode=args.test or args.dry_run,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        return 0 if ok else 1
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
