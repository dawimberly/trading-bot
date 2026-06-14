"""Check SpaceX IPO ↔ crypto headline monitor (manual).

Run:  python scripts/maintenance/check_spacex_monitor.py
      python scripts/maintenance/check_spacex_monitor.py --refresh
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modules.spacex_ipo_monitor import format_monitor_line, get_spacex_ipo_monitor


def main() -> None:
    parser = argparse.ArgumentParser(description="SpaceX IPO / BTC headline monitor")
    parser.add_argument("--refresh", action="store_true", help="Bypass cache")
    args = parser.parse_args()

    snapshot = get_spacex_ipo_monitor(force_refresh=args.refresh)
    print(format_monitor_line(snapshot))
    if snapshot:
        print(json.dumps(snapshot, indent=2))


if __name__ == "__main__":
    main()
