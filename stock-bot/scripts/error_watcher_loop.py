#!/usr/bin/env python3
"""Consume recent cycle errors and apply allowlisted runtime auto-fix.

Never edits source. Ollama is consulted only for unknown error classes.

  python scripts/error_watcher_loop.py --once
  python scripts/error_watcher_loop.py --sleep 30
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_ERRORS = ROOT / "logs" / "bot_errors.jsonl"


def _iter_recent_errors(limit: int = 20) -> list[dict]:
    if not _ERRORS.is_file():
        return []
    rows: list[dict] = []
    try:
        lines = _ERRORS.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def run_once() -> int:
    from modules.error_autofix import handle_cycle_error

    applied = 0
    for row in _iter_recent_errors():
        klass = str(row.get("error_class") or "")
        if klass not in ("other", "transient_api", "transient_network"):
            continue
        err = str(row.get("error") or row.get("context") or "")
        if not err:
            continue
        handle_cycle_error(err, error_class=klass or None)
        applied += 1
        break
    print(f"error_watcher_loop: processed {applied} error(s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Error auto-fix loop (runtime only)")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.0, help="Repeat every N seconds")
    args = parser.parse_args()
    if args.once or args.sleep <= 0:
        return run_once()
    while True:
        run_once()
        time.sleep(args.sleep)


if __name__ == "__main__":
    raise SystemExit(main())
