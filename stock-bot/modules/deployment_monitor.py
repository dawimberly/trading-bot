"""Paper deployment monitoring — excess-cash warnings and daily cash snapshots."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import config
from modules.safe_io import read_json_file, write_json_atomic

logger = logging.getLogger(__name__)

_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "deployment_cash_state.json"


def _today() -> str:
    return date.today().isoformat()


def record_cash_snapshot(equity: float, cash: float) -> dict[str, Any]:
    """Persist one cash-ratio sample per calendar day (paper research)."""
    eq = float(equity)
    cash_f = float(cash)
    cash_pct = round(cash_f / eq, 4) if eq > 0 else 0.0
    state = read_json_file(_STATE_FILE) or {}
    history: list[dict[str, Any]] = list(state.get("history") or [])
    today = _today()
    entry = {"date": today, "cash_pct": cash_pct, "equity": round(eq, 2), "cash": round(cash_f, 2)}
    if history and history[-1].get("date") == today:
        history[-1] = entry
    else:
        history.append(entry)
    history = history[-14:]
    state = {"history": history, "updated_at": datetime.now(timezone.utc).isoformat()}
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(_STATE_FILE, state)
    except OSError as exc:
        logger.debug("deployment cash state write failed: %s", exc)
    return entry


def excess_cash_warning(
    equity: float | None = None,
    cash: float | None = None,
    *,
    warn_pct: float | None = None,
    warn_days: int | None = None,
) -> str | None:
    """Return a startup/cycle warning when cash exceeded threshold for N consecutive days."""
    if not config.paper_aggressive_context():
        return None
    threshold = float(warn_pct if warn_pct is not None else config.PAPER_EXCESS_CASH_WARN_PCT)
    days_needed = int(warn_days if warn_days is not None else config.PAPER_EXCESS_CASH_WARN_DAYS)
    state = read_json_file(_STATE_FILE) or {}
    history: list[dict[str, Any]] = list(state.get("history") or [])
    if equity is not None and cash is not None and float(equity) > 0:
        record_cash_snapshot(float(equity), float(cash))
        state = read_json_file(_STATE_FILE) or {}
        history = list(state.get("history") or [])
    if len(history) < days_needed:
        return None
    recent = history[-days_needed:]
    if all(float(row.get("cash_pct") or 0) >= threshold for row in recent):
        latest = recent[-1]
        return (
            f"DEPLOYMENT WARNING: cash {float(latest.get('cash_pct', 0)):.0%} of equity "
            f"for {days_needed}+ days (threshold {threshold:.0%}) — active sleeves under-deployed; "
            f"check yield gate, min notional, and sleeve caps"
        )
    return None
