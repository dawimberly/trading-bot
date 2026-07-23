"""Quick health check for paper and/or live Alpaca bots.

Run:
  python check_status.py --paper
  python check_status.py --live
  python check_status.py --paper --live
  python check_status.py          # both books
"""

from __future__ import annotations

import argparse
import os
import sys

from modules.health_check import (
    format_multi_book_report,
    resolve_live_heartbeat_path,
    resolve_paper_heartbeat_path,
    run_health_check,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Alpaca bot health check")
    parser.add_argument(
        "--paper",
        action="store_true",
        help="Include paper / research book",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Include live book",
    )
    args = parser.parse_args()

    if args.paper and not args.live:
        books = [True]
    elif args.live and not args.paper:
        books = [False]
    else:
        books = [False, True]

    reports = []
    exit_code = 0
    for paper in books:
        try:
            if paper:
                paper_hb = resolve_paper_heartbeat_path()
                os.environ["HEARTBEAT_FILE"] = str(paper_hb)
            elif not paper:
                live_hb = resolve_live_heartbeat_path()
                os.environ["HEARTBEAT_FILE"] = str(live_hb)
            reports.append(run_health_check(paper=paper))
        except Exception as exc:  # noqa: BLE001
            book = "PAPER" if paper else "LIVE"
            print(f"=== {book} health check ===\nERROR: {exc}", file=sys.stderr)
            exit_code = 1

    if reports:
        print(format_multi_book_report(reports))

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
