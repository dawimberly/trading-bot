#!/usr/bin/env python3
"""Base-rate check: how unusual are SPCX/GOLD/RBRK among bot buys."""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOOKS = ROOT / "data/portal/users/dawimberly/books"
FOCUS = {"SPCX", "GOLD", "RBRK", "BRKR"}


def parse_ts(s: str):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "")[:19])
    except ValueError:
        return None


def main() -> None:
    buy_syms: Counter[str] = Counter()
    first_buy: dict[str, datetime] = {}
    realized: defaultdict[str, float] = defaultdict(float)
    buy_events: list[tuple[datetime, str, str]] = []

    for jpath in BOOKS.glob("*/paper_journal.csv"):
        book = jpath.parent.name
        with jpath.open(encoding="utf-8", errors="replace", newline="") as f:
            for row in csv.DictReader(f):
                ts = parse_ts(row.get("timestamp") or "")
                if not ts or ts < datetime(2026, 8, 1):
                    continue
                sym = (row.get("symbol") or "").upper()
                if not sym or sym in {"VTI", "SPY", "CASH"}:
                    continue
                side = (row.get("side") or "").lower()
                ev = (row.get("event") or "").lower()
                if side == "buy" and ("fill" in ev or ev == "signal" or "fill" == ev):
                    if "fill" in ev or ev == "fill":
                        buy_syms[sym] += 1
                        buy_events.append((ts, sym, book))
                        first_buy[sym] = min(first_buy.get(sym, ts), ts)
                try:
                    pnl = float(row.get("realized_pnl") or 0)
                except ValueError:
                    pnl = 0.0
                if pnl:
                    realized[sym] += pnl

    print(f"unique symbols with buys since Aug1: {len(buy_syms)}")
    print(f"total buy fill/signal rows: {sum(buy_syms.values())}")
    print("\nTop buy symbols:")
    for s, n in buy_syms.most_common(20):
        mark = " <-- focus" if s in FOCUS else ""
        print(f"  {s:6s} n={n:3d} realized={realized[s]:+8.1f}{mark}")

    print("\nFocus first buys:")
    for s in sorted(FOCUS):
        if s in first_buy:
            print(f"  {s}: first={first_buy[s]} buys={buy_syms[s]} realized={realized[s]:+.1f}")
        else:
            print(f"  {s}: no buys in journals")

    # How many symbols have positive realized?
    pos = [s for s, v in realized.items() if v > 50]
    print(f"\nSymbols with realized > $50: {len(pos)} -> {sorted(pos)[:30]}")
    print(f"Focus in that set: {sorted(FOCUS & set(pos))}")


if __name__ == "__main__":
    main()
