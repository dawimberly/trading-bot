"""CLI entry: python -m nerdminer [--once]"""

from __future__ import annotations

import argparse
import json
import sys

from nerdminer import config
from nerdminer.monitor import detect_port, run_monitor, save_state, snapshot_from_serial


def main() -> None:
    parser = argparse.ArgumentParser(description="NerdMiner v2 serial monitor")
    parser.add_argument("--port", default=config.SERIAL_PORT, help="Serial port (default COM4)")
    parser.add_argument("--baud", type=int, default=config.BAUD)
    parser.add_argument("--once", action="store_true", help="Single snapshot then exit")
    parser.add_argument("--seconds", type=float, default=8.0, help="Read duration for --once")
    parser.add_argument(
        "--no-alerts",
        action="store_true",
        help="Disable Telegram alerts on warning/offline",
    )
    args = parser.parse_args()

    port = detect_port(args.port)
    if not port:
        print("No serial port found. Set NERDMINER_SERIAL_PORT or plug in the NerdMiner.")
        sys.exit(1)

    if args.once:
        state = snapshot_from_serial(port, baud=args.baud, seconds=args.seconds)
        save_state(state)
        print(json.dumps(state, indent=2))
        return

    print(f"Monitoring NerdMiner on {port} @ {args.baud} -> {config.STATE_FILE}")
    print("Press Ctrl+C to stop.")
    run_monitor(
        port=port,
        baud=args.baud,
        alert_on_offline=not args.no_alerts,
    )


if __name__ == "__main__":
    main()
