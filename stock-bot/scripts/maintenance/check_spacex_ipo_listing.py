"""Check real SpaceX IPO listing status (SEC milestones + Alpaca SPCX).

Run:  python scripts/maintenance/check_spacex_ipo_listing.py
      python scripts/maintenance/check_spacex_ipo_listing.py --refresh
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modules.alpaca_executor import AlpacaExecutor
from modules.spacex_ipo_listing_monitor import (
    format_listing_line,
    get_spacex_ipo_listing_status,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="SpaceX IPO listing monitor (SEC + Alpaca)")
    parser.add_argument("--refresh", action="store_true", help="Refresh SEC/news cache")
    args = parser.parse_args()

    ex = AlpacaExecutor()
    snapshot = get_spacex_ipo_listing_status(
        executor=ex,
        force_refresh=args.refresh,
    )
    print(format_listing_line(snapshot))
    if snapshot:
        print(json.dumps(snapshot, indent=2))


if __name__ == "__main__":
    main()
