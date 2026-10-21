"""Synthetic plumbing run. Returns are not market evidence."""
from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .calendar import ET, bar_starts, is_session
from .__main__ import connect, ingest, report

ROOT = Path(__file__).resolve().parents[1]


def build_feed(days: int = 6) -> Path:
    out = ROOT / "runtime" / "smoke" / "feed.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    cursor = date(2026, 9, 1)
    written = 0
    index = 0
    with out.open("w", encoding="utf-8") as handle:
        while written < days:
            day = cursor.isoformat()
            if is_session(day):
                symbols = ["SYNTH_A", "SYNTH_B", "SYNTH_C"]
                for clock in bar_starts(day):
                    hour, minute = clock.split(":")
                    moment = datetime.combine(cursor, time(int(hour), int(minute)), tzinfo=ET)
                    bars = {}
                    for offset, symbol in enumerate(symbols):
                        close = 100 + index * 0.035 + 3 * math.sin(index * 0.16 + offset)
                        previous = 100 + max(0, index - 1) * 0.035 + 3 * math.sin(max(0, index - 1) * 0.16 + offset)
                        bars[symbol] = {
                            "open": previous,
                            "high": max(previous, close) + 0.05,
                            "low": min(previous, close) - 0.05,
                            "close": close,
                            "eligible": True,
                        }
                    frame = {
                        "timestamp": moment.isoformat(),
                        "complete": True,
                        "price_basis": "raw",
                        "feed_mode": "synthetic",
                        "as_of": (moment + timedelta(minutes=5)).isoformat(),
                        "action_audit": {
                            "source": "synthetic_declared",
                            "independent": False,
                            "coverage_from": day,
                            "coverage_through": day,
                            "symbols": symbols,
                            "events": [],
                        },
                        "bars": bars,
                    }
                    handle.write(json.dumps(frame) + "\n")
                    index += 1
                written += 1
            cursor += timedelta(days=1)
    return out


def run() -> dict:
    feed = build_feed()
    run_dir = ROOT / "runtime" / "smoke"
    database = run_dir / "paper.sqlite"
    if database.exists():
        database.unlink()
    connection, path = connect("smoke")
    first = 0
    try:
        with feed.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    first += int(ingest(connection, json.loads(line)))
        report(connection, path)
    finally:
        connection.close()
    connection, path = connect("smoke")
    second = 0
    try:
        with feed.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    second += int(ingest(connection, json.loads(line)))
        payload = report(connection, path)
    finally:
        connection.close()
    result = {
        "feed": str(feed),
        "first_processed": first,
        "second_processed": second,
        "evidence": "synthetic",
        "market_claim": False,
        "running": False,
        "complete_sessions": payload["complete_sessions"],
        "learner_minus_baseline": payload["learner_minus_baseline"],
    }
    (run_dir / "smoke_result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    run()
