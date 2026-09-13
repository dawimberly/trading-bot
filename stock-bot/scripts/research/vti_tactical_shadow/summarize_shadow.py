"""Summarize VTI tactical shadow JSONL with provenance filters."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run_shadow_once import load_events, write_summary  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--eligible-only",
        action="store_true",
        help="Print count of promote-eligible rows only",
    )
    args = ap.parse_args()
    rows = load_events(eligible_only=False)
    path = write_summary(rows)
    eligible = [r for r in rows if r.get("promote_eligible")]
    print(f"main_rows={len(rows)} eligible={len(eligible)} -> {path}")
    if args.eligible_only:
        print(f"eligible_only_count={len(eligible)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
