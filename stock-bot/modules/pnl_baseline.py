"""Display-only P&L baseline since the profitable paper form was locked.

Does **not** change account equity, trading, or all-time \"Since Start\".
Default paper anchor: FORWARD_PAPER_FREEZE start (2026-07-29 / v1.5.4 lock).
Override with env ``PNL_PERFORMANCE_START_DATE=YYYY-MM-DD``.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
# Locked profitable paper form (SPY-off / dyn VTI freeze) — see FORWARD_PAPER_FREEZE.md
DEFAULT_PAPER_FORM_DATE = date(2026, 7, 29)


def performance_start_date(*, paper: bool = True) -> date:
    raw = (os.getenv("PNL_PERFORMANCE_START_DATE") or "").strip()
    if raw:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            logger.warning("Invalid PNL_PERFORMANCE_START_DATE=%r — using default", raw)
    if paper:
        return DEFAULT_PAPER_FORM_DATE
    try:
        from modules.sharpe_history import _load_state

        state = _load_state() or {}
        for key in ("last_major_update_date", "last_version_change_date", "deployment_date"):
            val = state.get(key)
            if val:
                return date.fromisoformat(str(val)[:10])
    except Exception as exc:
        logger.debug("live performance start from sharpe state skipped: %s", exc)
    return DEFAULT_PAPER_FORM_DATE


def performance_label(*, paper: bool = True) -> str:
    custom = (os.getenv("PNL_PERFORMANCE_LABEL") or "").strip()
    if custom:
        return custom
    return "Since form" if paper else "Since update"


def _as_et(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=_ET)
    return ts.astimezone(_ET)


def _equity_rows_from_journal(path: Path) -> list[tuple[datetime, float]]:
    try:
        from modules.status_metrics import _read_equity_journal

        rows = _read_equity_journal(path)
        return list(rows or [])
    except Exception as exc:
        logger.debug("equity journal read failed (%s): %s", path, exc)
        return []


def _candidate_journal_paths(
    *,
    username: str | None = None,
    book_id: str | None = None,
) -> list[Path]:
    paths: list[Path] = []
    try:
        import config

        raw = Path(str(getattr(config, "PAPER_JOURNAL_CSV", None) or "paper_journal.csv"))
        root = Path(__file__).resolve().parents[1]
        active = raw if raw.is_absolute() else (root / raw)
        if active.is_file():
            paths.append(active)
    except Exception:
        pass
    if username and book_id:
        try:
            from modules.portal_paths import book_journal_path

            bp = book_journal_path(username, book_id)
            if bp.is_file() and bp not in paths:
                paths.insert(0, bp)
        except Exception:
            pass
    # De-dupe while preserving order
    out: list[Path] = []
    seen: set[Path] = set()
    for p in paths:
        key = p.resolve()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def resolve_baseline_equity(
    *,
    start: date | None = None,
    paper: bool = True,
    current_equity: float | None = None,
    username: str | None = None,
    book_id: str | None = None,
) -> tuple[float | None, date | None, str]:
    """Return (baseline_equity, baseline_date, note).

    Prefers the first journal equity mark on/after ``start`` that sits in a
    sane band vs current equity (avoids mixed live/small-account stamps).
    """
    start = start or performance_start_date(paper=paper)
    cur = float(current_equity) if current_equity and current_equity > 0 else None
    # Paper ~97k book: ignore tiny/live-scale stamps when resolving the form baseline.
    min_eq = 20_000.0
    if cur and cur >= 50_000:
        min_eq = max(min_eq, cur * 0.35)

    best: tuple[datetime, float] | None = None
    source = ""
    for path in _candidate_journal_paths(username=username, book_id=book_id):
        rows = _equity_rows_from_journal(path)
        for ts, eq in rows:
            try:
                t = _as_et(ts)
                e = float(eq)
            except (TypeError, ValueError):
                continue
            if t.date() < start:
                continue
            if e < min_eq:
                continue
            if best is None or t < best[0]:
                best = (t, e)
                source = path.name
        if best is not None:
            break

    if best is None:
        return None, start, "no journal equity on/after start date"
    return float(best[1]), best[0].date(), f"journal:{source}"


def performance_pnl(
    current_equity: float,
    *,
    paper: bool = True,
    username: str | None = None,
    book_id: str | None = None,
    start: date | None = None,
) -> dict[str, Any]:
    """Equity delta since the profitable-form baseline (display only)."""
    start = start or performance_start_date(paper=paper)
    label = performance_label(paper=paper)
    baseline, baseline_date, note = resolve_baseline_equity(
        start=start,
        paper=paper,
        current_equity=current_equity,
        username=username,
        book_id=book_id,
    )
    out: dict[str, Any] = {
        "label": label,
        "start_date": start.isoformat(),
        "baseline_equity": baseline,
        "baseline_date": baseline_date.isoformat() if baseline_date else None,
        "current_equity": float(current_equity) if current_equity else None,
        "pnl": None,
        "return_pct": None,
        "note": note,
        "ok": False,
    }
    if baseline is None or not current_equity or baseline <= 0:
        return out
    pnl = float(current_equity) - float(baseline)
    ret = 100.0 * (float(current_equity) / float(baseline) - 1.0)
    out.update({"pnl": pnl, "return_pct": ret, "ok": True})
    return out
