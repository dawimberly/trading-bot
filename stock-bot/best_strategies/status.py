"""Print the best_strategies ledger. Never deploys, never writes .env."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ledger import AS_OF, BOOKS, STRATEGIES, by_status, queue  # noqa: E402


def _row(s: dict) -> str:
    return (
        f"  {s['priority']:>3}  {s['status']:<8}  {s['book']:<11}  "
        f"{s['id']:<24}  {s['name']}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--queue", action="store_true", help="test+watch only")
    ap.add_argument("--rejects", action="store_true")
    ap.add_argument("--deployed", action="store_true")
    args = ap.parse_args()

    print(f"best_strategies ledger  as of {AS_OF}")
    print("research -> aggressive (agent) -> Medium SoT (owner) -> live (owner)")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("this command does not deploy")
    print()
    for key in ("aggressive", "medium", "live"):
        b = BOOKS[key]
        print(f"  {key:<11}  {b['id']:<18}  {b['now']}")
    print()

    if args.rejects:
        rows = by_status("reject")
        title = "rejects"
    elif args.deployed:
        rows = by_status("deployed") + by_status("hold")
        title = "deployed + hold"
    elif args.queue:
        rows = queue()
        title = "queue (test + watch)"
    else:
        rows = sorted(STRATEGIES, key=lambda s: (s["priority"], s["id"]))
        title = "all"

    print(f"{title}: {len(rows)}")
    print(f"  {'pri':>3}  {'status':<8}  {'book':<11}  {'id':<24}  name")
    for s in rows:
        print(_row(s))
        nxt = s.get("next")
        if nxt and (args.queue or s["status"] == "test"):
            print(f"       next: {nxt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
