#!/usr/bin/env python3
"""Daily win/loss summary from paper journal (fills + trade_closed rows).

Usage:
  python scripts/analysis/daily_win_loss.py
  python scripts/analysis/daily_win_loss.py --days 7
  python scripts/analysis/daily_win_loss.py --journal path/to/paper_journal.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _default_journal() -> Path:
    portal = (
        ROOT
        / "data"
        / "portal"
        / "users"
        / "dawimberly"
        / "books"
        / "alpaca_paper_v2"
        / "paper_journal.csv"
    )
    if portal.is_file():
        return portal
    return ROOT / "paper_journal.csv"


def _f(val) -> float | None:
    try:
        if val is None or str(val).strip() == "":
            return None
        return float(val)
    except (TypeError, ValueError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", type=Path, default=None)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--since", type=str, default="")
    args = ap.parse_args()
    path = args.journal or _default_journal()
    if not path.is_file():
        print(f"Journal not found: {path}")
        return 1

    since = args.since.strip()
    if not since:
        since = (datetime.now() - timedelta(days=max(1, args.days))).strftime("%Y-%m-%d")

    rows = list(csv.DictReader(path.open(encoding="utf-8", errors="replace")))
    # Prefer trade_closed; fall back to sell fills with pnl.
    closed = [r for r in rows if r.get("event") == "trade_closed"]
    if not closed:
        closed = [
            r
            for r in rows
            if r.get("event") == "fill"
            and str(r.get("side") or "").lower() in ("sell", "sell_short")
            and _f(r.get("realized_pnl")) is not None
        ]

    by_day: dict[str, list] = defaultdict(list)
    by_reason: dict[str, list] = defaultdict(list)
    for r in closed:
        ts = str(r.get("timestamp") or "")
        if ts[:10] < since:
            continue
        pnl = _f(r.get("realized_pnl"))
        if pnl is None:
            continue
        day = ts[:10]
        reason = (r.get("exit_reason") or r.get("notes") or "(blank)").strip()
        by_day[day].append(pnl)
        by_reason[reason].append(pnl)

    print(f"Journal: {path}")
    print(f"Since:   {since}")
    print(f"Rows:    {sum(len(v) for v in by_day.values())} closed trades")
    print()
    print(f"{'Date':12} {'N':>4} {'W':>4} {'L':>4} {'WR%':>6} {'Net$':>10} {'AvgW':>8} {'AvgL':>8}")
    all_pnls: list[float] = []
    for day in sorted(by_day):
        pnls = by_day[day]
        all_pnls.extend(pnls)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        wr = 100.0 * len(wins) / len(pnls) if pnls else 0.0
        avg_w = sum(wins) / len(wins) if wins else 0.0
        avg_l = sum(losses) / len(losses) if losses else 0.0
        print(
            f"{day:12} {len(pnls):4d} {len(wins):4d} {len(losses):4d} "
            f"{wr:6.1f} {sum(pnls):10.2f} {avg_w:8.2f} {avg_l:8.2f}"
        )

    if all_pnls:
        wins = [p for p in all_pnls if p > 0]
        losses = [p for p in all_pnls if p < 0]
        print()
        print(
            f"TOTAL n={len(all_pnls)} wr={100*len(wins)/len(all_pnls):.1f}% "
            f"net=${sum(all_pnls):.2f} "
            f"avgW=${(sum(wins)/len(wins) if wins else 0):.2f} "
            f"avgL=${(sum(losses)/len(losses) if losses else 0):.2f}"
        )
        print()
        print("By exit_reason:")
        for reason, pnls in sorted(by_reason.items(), key=lambda x: sum(x[1])):
            w = sum(1 for p in pnls if p > 0)
            l = sum(1 for p in pnls if p < 0)
            print(
                f"  {reason[:40]:40} n={len(pnls):3d} w={w:3d} l={l:3d} "
                f"pnl=${sum(pnls):+.2f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
