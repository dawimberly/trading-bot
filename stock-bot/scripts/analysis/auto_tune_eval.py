#!/usr/bin/env python3
"""Nightly / on-demand auto-tune with tiered apply.

  python scripts/analysis/auto_tune_eval.py
  python scripts/analysis/auto_tune_eval.py --nightly
  python scripts/analysis/auto_tune_eval.py --force

Tiers: T0/T1 hygiene auto; T2 strategy auto if <= AUTO_APPLY_MAX_TIER;
T3+ propose only. Never live / never VTI%.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=False)
try:
    from modules.portal_paths import resolve_primary_paper_book_dir

    load_dotenv(resolve_primary_paper_book_dir() / ".env", override=False)
except Exception:
    pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--nightly", action="store_true", help="mark as nightly eval")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--enable-check", action="store_true")
    args = ap.parse_args()

    from modules import auto_tune

    if args.enable_check:
        print(f"AUTO_TUNE_ENABLED={auto_tune.enabled()}")
        print(f"AUTO_TUNE_APPLY={auto_tune.apply_enabled()}")
        print(f"AUTO_APPLY_MAX_TIER={auto_tune.max_apply_tier()}")
        print(f"min_closed={os.getenv('AUTO_TUNE_MIN_CLOSED', '40')}")
        print(f"cooldown_days={os.getenv('AUTO_TUNE_COOLDOWN_DAYS', '14')}")
        return 0

    result = auto_tune.evaluate(force=args.force, nightly=args.nightly)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"decision: {result.get('decision')}")
        print(f"max_tier: {result.get('max_apply_tier')}")
        print(f"message:  {result.get('message')}")
        gates = result.get("gates") or {}
        for k, v in gates.items():
            if k == "uptime_snap":
                continue
            print(f"  gate[{k}]: {v}")
        prop = result.get("proposal")
        if prop:
            print(
                f"proposal: T{prop.get('tier')} {prop.get('key')} "
                f"{prop.get('from')} -> {prop.get('to')}"
            )
            print(f"  why: {prop.get('reason')}")
        hy = result.get("hygiene") or []
        if hy:
            print(f"hygiene: {len(hy)} action(s)")
            for h in hy[:5]:
                print(f"  T{h.get('tier')} {h.get('action')} {h.get('file')}")
        led = result.get("ledger") or {}
        print(
            f"ledger: n={led.get('n')} net=${led.get('net')} "
            f"exp=${led.get('expectancy')}"
        )
        if result.get("applied"):
            print("APPLIED — restart paper bot to load.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
