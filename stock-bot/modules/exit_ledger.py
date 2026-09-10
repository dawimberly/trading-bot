"""Exit-reason dollar ledger (replaces win-rate vanity)."""

from __future__ import annotations

import csv
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _f(val: Any) -> float | None:
    try:
        if val is None or str(val).strip() == "":
            return None
        return float(val)
    except (TypeError, ValueError):
        return None


def _reason(row: dict[str, str]) -> str:
    code = (row.get("exit_reason") or "").strip()
    if code:
        return code
    notes = (row.get("notes") or "").strip()
    low = notes.lower()
    for needle, name in (
        ("smart_atr", "smart_atr_stop"),
        ("concentration", "concentration_guard_trim"),
        ("fat_loser", "nyse_fat_loser_trim"),
        ("smart_size", "smart_size_reduce"),
    ):
        if needle in low:
            return name
    return notes.split()[0] if notes else "(blank)"


def summarize_exit_ledger(
    journal_path: Path | str | None = None,
    *,
    days: int = 20,
) -> dict[str, Any]:
    """Aggregate closed sells by exit_reason over the last N calendar days."""
    if journal_path is None:
        try:
            from modules.portal_paths import resolve_primary_paper_journal

            path = resolve_primary_paper_journal()
        except Exception:
            path = Path("paper_journal.csv")
    else:
        path = Path(journal_path)
    since = (datetime.now() - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d")
    empty = {
        "days": days,
        "since": since,
        "n": 0,
        "wins": 0,
        "losses": 0,
        "net": 0.0,
        "expectancy": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "by_reason": [],
        "journal": str(path),
    }
    if not path.is_file():
        return empty

    by_reason: dict[str, list[float]] = defaultdict(list)
    try:
        with path.open(encoding="utf-8", errors="replace", newline="") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        logger.debug("exit_ledger read failed: %s", path, exc_info=True)
        return empty

    closed = [r for r in rows if (r.get("event") or "") == "trade_closed"]
    if not closed:
        closed = [
            r
            for r in rows
            if (r.get("event") or "") == "fill"
            and str(r.get("side") or "").lower() in ("sell", "sell_short")
            and _f(r.get("realized_pnl")) is not None
        ]

    all_pnls: list[float] = []
    for r in closed:
        ts = str(r.get("timestamp") or "")
        if ts[:10] < since:
            continue
        pnl = _f(r.get("realized_pnl"))
        if pnl is None:
            continue
        by_reason[_reason(r)].append(pnl)
        all_pnls.append(pnl)

    if not all_pnls:
        return empty

    wins = [p for p in all_pnls if p > 0]
    losses = [p for p in all_pnls if p < 0]
    reasons = []
    for name, pnls in sorted(by_reason.items(), key=lambda x: sum(x[1])):
        w = sum(1 for p in pnls if p > 0)
        l = sum(1 for p in pnls if p < 0)
        reasons.append(
            {
                "reason": name,
                "n": len(pnls),
                "wins": w,
                "losses": l,
                "net": round(sum(pnls), 2),
                "avg": round(sum(pnls) / len(pnls), 2),
            }
        )
    return {
        "days": days,
        "since": since,
        "n": len(all_pnls),
        "wins": len(wins),
        "losses": len(losses),
        "net": round(sum(all_pnls), 2),
        "expectancy": round(sum(all_pnls) / len(all_pnls), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "by_reason": reasons,
        "journal": str(path),
    }
