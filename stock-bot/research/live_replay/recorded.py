"""Read the Live journal. Timestamps in the file are US Central."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")


def parse_ct(text: str) -> datetime | None:
    raw = (text or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw[:19], fmt).replace(tzinfo=CT)
        except ValueError:
            continue
    return None


def load_journal(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def session_rows(rows: list[dict], day: str) -> list[dict]:
    kept = []
    for row in rows:
        stamp = parse_ct(row.get("timestamp") or "")
        if stamp is not None and stamp.date().isoformat() == day:
            kept.append(row)
    return kept


def num(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None
