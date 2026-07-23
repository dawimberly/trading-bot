"""Generate local weekly monitoring report (Markdown + HTML).

  python scripts/generate_weekly_report.py --test
  python scripts/generate_weekly_report.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from modules.weekly_report import generate_weekly_report, weekly_report_due


def main() -> int:
    from modules.logging_utils import setup_project_logging

    setup_project_logging()
    parser = argparse.ArgumentParser(
        description="Generate weekly monitoring report (reports/weekly/)"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Generate now (skip Friday 16:30 ET schedule gate)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview paths and first lines; do not write files",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the HTML report in your browser after generation",
    )
    args = parser.parse_args()

    if not args.test and not args.dry_run and not weekly_report_due(market_open=False):
        print(
            f"Not due: Friday after {config.TELEGRAM_WEEKLY_SUMMARY_TIME} ET "
            "with market closed, or already generated this week. Use --test."
        )
        return 0

    paths = generate_weekly_report(test_mode=args.test, dry_run=args.dry_run)
    if paths is None:
        print("Weekly report not generated (schedule gate).")
        return 0

    _md_path, html_path = paths
    if args.open and not args.dry_run and html_path.is_file():
        webbrowser.open(html_path.resolve().as_uri())
        print(f"Opened {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
