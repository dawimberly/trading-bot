"""Daily GET-only refresh of Live, Medium, and Lab trade-review reports.

Does not install a scheduled task, start a bot, or leave a worker running.
Example:
  python scripts/analysis/refresh_trade_review.py --max-pages 30
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from modules.trade_review_collect import (  # noqa: E402
    BOOKS,
    CollectionError,
    discover_book_env,
    parse_env_file,
    refresh_book,
    urllib_transport,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book", action="append", choices=BOOKS, dest="books")
    parser.add_argument("--max-pages", type=int, default=30)
    parser.add_argument("--symbol-cap", type=int, default=40)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    books = args.books or list(BOOKS)
    transport = urllib_transport()
    failures = 0
    for book in books:
        envs = discover_book_env(args.root, book)
        if len(envs) != 1:
            print(f"{book}: skipped, expected one isolated env file, found {len(envs)}")
            failures += 1
            continue
        env_path = envs[0]
        try:
            summary = refresh_book(
                book=book,
                env=parse_env_file(env_path),
                root=args.root,
                transport=transport,
                journal_path=env_path.parent / "paper_journal.csv",
                max_pages=args.max_pages,
                symbol_cap=args.symbol_cap,
            )
        except CollectionError as exc:
            print(f"{book}: {exc}")
            failures += 1
            continue
        print(
            f"{book}: purchases={summary['purchases']} sales={summary['sales']} "
            f"fills={summary['fills_stored']} added={summary['fills_added']} "
            f"complete={summary['complete_from_start']} truncated={summary['truncated']} "
            f"error={summary['error']} account_last4={summary['account_last4']}"
        )
        if summary.get("price_errors"):
            print(f"{book}: price gaps={summary['price_errors']}")
        if summary.get("calendar_error"):
            print(f"{book}: calendar gap={summary['calendar_error']}")
        print(f"{book}: wrote {summary['report_json']}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
