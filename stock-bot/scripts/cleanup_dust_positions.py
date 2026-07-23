"""Close fractional dust positions on Alpaca (paper or live).

Dust = market value < $1 OR abs(qty) < 0.001. Uses close_position API
(qty-safe); falls back to qty market orders if needed.

Run:
  python scripts/cleanup_dust_positions.py
  python scripts/cleanup_dust_positions.py --live
  python scripts/cleanup_dust_positions.py --live --execute
  python scripts/cleanup_dust_positions.py --symbols SPCX SPY GLD --execute
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402  — loads .env on import

from modules.alpaca_executor import AlpacaExecutor
from modules.dust_cleanup import DustCloseResult, format_cleanup_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Close Alpaca dust positions")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use live credentials (default: paper / PAPER_TRADING from .env)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Submit close orders (default: dry-run list only)",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        metavar="TICKER",
        help="Only check these tickers (default: scan all positions)",
    )
    parser.add_argument(
        "--max-notional",
        type=float,
        default=None,
        help="Dust threshold in USD (default: DUST_MAX_NOTIONAL env or 1)",
    )
    parser.add_argument(
        "--max-qty",
        type=float,
        default=None,
        help="Dust qty threshold (default: DUST_MAX_QTY env or 0.001)",
    )
    args = parser.parse_args()

    paper = not args.live
    if not paper and not config.ALLOW_LIVE_TRADING:
        print(
            "Live trading disabled. Set ALLOW_LIVE_TRADING=yes in .env to close live dust.",
            file=sys.stderr,
        )
        return 1

    if args.execute and not paper:
        confirm = input("Type 'close' to liquidate LIVE dust positions: ").strip()
        if confirm != "close":
            print("Aborted.")
            return 1

    executor = AlpacaExecutor(paper=paper)
    kwargs: dict = {"dry_run": not args.execute, "symbols": args.symbols}
    if args.max_notional is not None:
        kwargs["max_notional"] = args.max_notional
    if args.max_qty is not None:
        kwargs["max_qty"] = args.max_qty

    raw = executor.cleanup_dust_positions(**kwargs)
    results = [DustCloseResult(**r) for r in raw]
    print(format_cleanup_report(results, paper=paper, dry_run=not args.execute))

    errors = sum(1 for r in results if r.status == "error")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
