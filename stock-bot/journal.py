"""Query paper_chase_journal.csv and show open positions.

Examples:
  python journal.py --positions
  python journal.py --ticker AAPL
  python journal.py --ticker AAPL --history
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.env_loader import ensure_dotenv_loaded

ensure_dotenv_loaded()

from modules.console_output import safe_print as emit
from modules.logging_utils import setup_project_logging
from modules.paper_journal import (
    build_position_summary,
    format_positions_table,
    format_ticker_history,
    journal_paths,
    query_ticker_events,
    read_journal,
)


def main() -> None:
    setup_project_logging()
    parser = argparse.ArgumentParser(description="Paper journal queries and position summary")
    parser.add_argument("--ticker", "-t", help="Filter by ticker (e.g. AAPL)")
    parser.add_argument("--positions", "-p", action="store_true", help="Show open positions")
    parser.add_argument("--history", action="store_true", help="With --ticker, show journal events")
    parser.add_argument("--journal", help="Journal CSV path (default: paper_chase_journal.csv)")
    parser.add_argument("--live", action="store_true", help="Use live Alpaca book (default: paper)")
    parser.add_argument("--limit", type=int, default=30, help="Max history rows for --ticker")
    args = parser.parse_args()

    paper = not args.live
    journal_path = Path(args.journal) if args.journal else None

    if args.positions or (args.ticker and not args.history):
        rows, err = build_position_summary(paper=paper, ticker=args.ticker)
        for line in format_positions_table(
            rows,
            title=f"Open positions ({'paper' if paper else 'live'})",
            err=err,
        ):
            emit(line)
        if args.ticker and not rows and not err:
            emit(f"No open position for {args.ticker.upper()}.")
        if not args.history:
            return

    if args.ticker and args.history:
        events = query_ticker_events(
            args.ticker,
            limit=args.limit,
            journal_path=journal_path,
        )
        for line in format_ticker_history(args.ticker, events):
            emit(line)
        return

    if not args.positions and not args.ticker:
        paths = [journal_path] if journal_path else journal_paths()
        emit("Paper journal paths:")
        for p in paths:
            emit(f"  {p} ({'exists' if p.is_file() else 'missing'})")
        df = read_journal(path=journal_path)
        emit(f"Rows: {len(df)} | tickers with events: {df['ticker'].nunique() if not df.empty else 0}")
        emit()
        emit("Usage:")
        emit("  python journal.py --positions")
        emit("  python journal.py --ticker AAPL")
        emit("  python journal.py --ticker AAPL --history")


if __name__ == "__main__":
    main()
