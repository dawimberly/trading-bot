"""Approve the pending Thinking Engine tilt for live trading.

Reads decision_id from thinking_engine_last.json and writes thinking_engine_approval.json.
Required when THINKING_MANUAL_APPROVAL_LIVE=true (default on live).

Usage:
    python scripts/approve_thinking_tilt.py
    python scripts/approve_thinking_tilt.py --show
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from modules.safe_io import write_json_file


def main() -> int:
    parser = argparse.ArgumentParser(description="Approve pending thinking engine tilt")
    parser.add_argument(
        "--show",
        action="store_true",
        help="Print pending tilt from thinking_engine_last.json only",
    )
    args = parser.parse_args()

    last_path = ROOT / config.THINKING_ENGINE_OUTPUT_FILE
    if not last_path.is_file():
        print(f"No {last_path.name} found — run the bot or test script first.")
        return 1

    last = json.loads(last_path.read_text(encoding="utf-8"))
    decision_id = last.get("decision_id")
    if not decision_id:
        print("Last thinking output has no decision_id — refresh thinking first.")
        return 1

    print(f"Decision ID: {decision_id}")
    print(f"Regime: {last.get('regime')}")
    print(f"Narrative: {last.get('narrative')}")
    print(f"Tilt: {json.dumps(last.get('suggested_tilt'), indent=2)}")
    if last.get("validation_errors"):
        print(f"Validation notes: {last.get('validation_errors')}")

    if args.show:
        return 0

    approval_path = ROOT / config.THINKING_APPROVAL_FILE
    write_json_file(
        approval_path,
        {
            "decision_id": decision_id,
            "approved_at": datetime.now().isoformat(),
            "regime": last.get("regime"),
        },
    )
    print(f"Approved — wrote {approval_path.name}. Next bot cycle may apply this tilt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
