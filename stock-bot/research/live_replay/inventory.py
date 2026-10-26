"""What the Live book stored, and what a faithful replay still does not have."""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from pathlib import Path

from research.live_replay.recorded import load_journal, parse_ct, session_rows

BOOK = Path(r"C:\Users\Owner\PythonTrading\stock-bot\data\portal\users\dawimberly\books\alpaca_live")
DB = Path(r"C:\Users\Owner\PythonTrading\stock-bot\market_data.db")
SAFETY = Path(r"C:\Users\Owner\PythonTrading\stock-bot\trading_safety_state.json")

TRADED = ("JNJ", "INTC", "SHOP", "AAPL", "UNH", "NFLX", "CRM", "META", "SPY")
LOG_NEEDLES = (
    "execute_order blocked: max active tickers",
    "execute_order blocked: concentration guard",
    "deploy_skip",
    "[NYSE] Skipping",
    "execute_full_exit",
    "order submitted",
    "auto-dust",
    "concentration_guard",
    "NYSE SLEEVE entry",
)


def journal_inventory(days: tuple[str, ...]) -> dict:
    rows = load_journal(BOOK / "paper_journal.csv")
    out = {"rows": len(rows), "days": {}}
    for day in days:
        day_rows = session_rows(rows, day)
        events = Counter((row.get("event") or "") for row in day_rows)
        hours = Counter()
        for row in day_rows:
            if (row.get("event") or "") != "cycle":
                continue
            stamp = parse_ct(row.get("timestamp") or "")
            if stamp is not None:
                hours[f"{stamp.hour:02d}"] += 1
        startups = [
            row.get("timestamp")
            for row in day_rows
            if (row.get("event") or "") == "startup"
        ]
        last_cycle = None
        for row in day_rows:
            if (row.get("event") or "") == "cycle":
                last_cycle = {
                    "timestamp_ct": row.get("timestamp"),
                    "equity": row.get("equity"),
                    "cash": row.get("cash"),
                    "regime": row.get("regime"),
                }
        out["days"][day] = {
            "events": dict(events),
            "cycle_hours_ct": dict(sorted(hours.items())),
            "startups_ct": startups,
            "last_cycle": last_cycle,
            "trade_rows": [
                {
                    "timestamp_ct": row.get("timestamp"),
                    "event": row.get("event"),
                    "symbol": row.get("symbol"),
                    "side": row.get("side"),
                    "qty": row.get("qty"),
                    "price": row.get("price"),
                    "notional": row.get("notional"),
                    "pair_key": row.get("pair_key"),
                    "exit_reason": row.get("exit_reason"),
                    "equity": row.get("equity"),
                    "cash": row.get("cash"),
                    "notes": (row.get("notes") or "")[:160],
                }
                for row in day_rows
                if (row.get("event") or "") in {"fill", "signal", "exit", "trade_closed", "startup"}
            ],
        }
    return out


def bar_inventory() -> dict:
    """Describe current bar tables. These are not a point-in-time snapshot."""
    if not DB.is_file():
        return {"present": False}
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        intraday = [name for name in names if "_5m" not in name and "_daily" not in name]
        sample = {}
        for symbol in TRADED:
            if symbol not in names:
                sample[symbol] = {"table": False}
                continue
            row = conn.execute(f'SELECT MIN(Date), MAX(Date), COUNT(*) FROM "{symbol}"').fetchone()
            sample[symbol] = {"table": True, "min": row[0], "max": row[1], "rows": row[2]}
    finally:
        conn.close()
    return {
        "present": True,
        "path": str(DB),
        "bytes": DB.stat().st_size,
        "tables": len(names),
        "intraday_tables": len(intraday),
        "sample": sample,
        "usable_as_october_input": False,
        "reason": "current database, not a snapshot taken at the decision",
    }


def log_inventory(days: tuple[str, ...]) -> dict:
    path = BOOK / "bot.log"
    counts = {day: Counter() for day in days}
    reasons = {day: Counter() for day in days}
    dated = 0
    reason_re = re.compile(r"reason=([a-z_]+)")
    if not path.is_file():
        return {"present": False}
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if "APCA" in line or "SECRET" in line or "api_key" in line.lower():
                continue
            day = next((item for item in days if item in line), None)
            if day is None:
                continue
            dated += 1
            for needle in LOG_NEEDLES:
                if needle in line:
                    counts[day][needle] += 1
            if "deploy_skip" in line:
                match = reason_re.search(line)
                if match:
                    reasons[day][match.group(1)] += 1
    return {
        "present": True,
        "bytes": path.stat().st_size,
        "dated_lines": dated,
        "symbol_on_order_lines": False,
        "counts": {day: dict(counts[day]) for day in days},
        "deploy_skip_reasons": {day: dict(reasons[day]) for day in days},
    }
