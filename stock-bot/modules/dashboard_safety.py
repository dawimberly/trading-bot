"""Read-only dashboard safety and Thinking labels.

This module does not write safety state, change the environment, or contact
the network. It does not decide whether the engine may trade.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]
GENERIC_KEYS = frozenset({"paper", "live"})
SHORT_NAMES = {
    "alpaca_live": "Live",
    "alpaca_paper_v2": "Medium",
    "alpaca_paper": "Lab",
}


def safety_state_path() -> Path:
    """Project safety file. Book identity is a key inside it, not the process env."""
    return ROOT / "trading_safety_state.json"


def book_short_name(book_id: str | None) -> str:
    if not book_id:
        return "Unselected book"
    return SHORT_NAMES.get(book_id, book_id)


def _parse_aware(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(ET)


def _parse_day(value) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def session_calendar(bounds: dict | None, now: datetime) -> dict:
    """Use timezone-aware session bounds. Do not invent a session date."""
    if now.tzinfo is None:
        return {"verified": False, "reason": "clock timezone missing", "weekend": False}
    now_et = now.astimezone(ET)
    weekend = now_et.weekday() >= 5
    base = {"verified": False, "weekend": weekend, "clock": now_et}
    if not isinstance(bounds, dict):
        base["reason"] = "no session calendar"
        return base
    opened = _parse_aware(bounds.get("session_open"))
    closed = _parse_aware(bounds.get("session_close"))
    if opened is None or closed is None or closed <= opened:
        base["reason"] = "session calendar incomplete"
        return base
    if now_et > closed:
        base["reason"] = "session calendar stale"
        return base
    in_session = opened <= now_et <= closed
    base.update(
        verified=True,
        reason=None,
        in_session=in_session,
        in_session_date=opened.date() if in_session else None,
        upcoming_date=None if in_session else opened.date(),
    )
    return base


def read_safety_snapshot(
    *,
    book_id: str | None,
    state_path: Path,
    now: datetime,
    equity: float | None,
    session_bounds: dict | None,
    limit_ratio: float | None,
) -> dict:
    """Classify one book's recorded strategy anchor. Never writes."""
    calendar = session_calendar(session_bounds, now)
    snapshot = {
        "book_id": book_id,
        "calendar": calendar,
        "limit_ratio": limit_ratio,
        "equity": equity,
        "anchor": None,
        "anchor_date": None,
        "trip_recorded": False,
        "relation": "unavailable",
        "change_pct": None,
        "calculation": "unavailable",
        "error": None,
    }
    if not book_id or book_id in GENERIC_KEYS:
        snapshot["relation"] = "unavailable"
        snapshot["error"] = "book not selected"
        return snapshot
    if not state_path.is_file():
        snapshot["error"] = "state file unavailable"
        return snapshot
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        snapshot["error"] = "state file malformed"
        return snapshot
    except OSError:
        snapshot["error"] = "state file unreadable"
        return snapshot
    if not isinstance(payload, dict):
        snapshot["error"] = "state file malformed"
        return snapshot
    if book_id not in payload:
        snapshot["error"] = "no state for this book"
        return snapshot
    entry = payload.get(book_id)
    if not isinstance(entry, dict):
        snapshot["error"] = "state file malformed"
        return snapshot
    snapshot["trip_recorded"] = entry.get("circuit_tripped") is True
    raw_date = entry.get("daily_equity_date")
    if raw_date is not None and _parse_day(raw_date) is None:
        snapshot["error"] = "anchor date malformed"
        return snapshot
    anchor_date = _parse_day(raw_date) if raw_date else None
    anchor = entry.get("daily_equity_open")
    if anchor is not None:
        try:
            anchor = float(anchor)
        except (TypeError, ValueError):
            snapshot["error"] = "anchor malformed"
            return snapshot
        if anchor <= 0:
            snapshot["error"] = "anchor malformed"
            return snapshot
    snapshot["anchor"] = anchor
    snapshot["anchor_date"] = anchor_date
    snapshot["relation"] = _relation(anchor_date, calendar)
    snapshot["change_pct"], snapshot["calculation"] = _calculation(
        anchor, equity, limit_ratio, snapshot["relation"]
    )
    return snapshot


def _relation(anchor_date: date | None, calendar: dict) -> str:
    if anchor_date is None:
        return "missing"
    if not calendar.get("verified"):
        return "unverified"
    in_session = calendar.get("in_session_date")
    upcoming = calendar.get("upcoming_date")
    if in_session and anchor_date == in_session:
        return "current"
    if in_session is None and upcoming and anchor_date == upcoming:
        return "upcoming"
    if upcoming and anchor_date < upcoming:
        return "previous"
    if in_session and anchor_date < in_session:
        return "previous"
    return "unverified"


def _calculation(anchor, equity, limit_ratio, relation) -> tuple[float | None, str]:
    if relation != "current":
        return None, "not_current"
    if limit_ratio is None:
        return None, "limit_unverified"
    if anchor is None or equity is None or equity <= 0:
        return None, "equity_unverified"
    change = (float(equity) / float(anchor) - 1.0) * 100.0
    loss_ratio = (float(anchor) - float(equity)) / float(anchor)
    if loss_ratio >= float(limit_ratio) - 1e-9:
        return change, "at_or_over_limit"
    return change, "under_limit"


def _limit_text(limit_ratio) -> str:
    if limit_ratio is None:
        return "configured limit unavailable"
    pct = float(limit_ratio) * 100.0
    shown = f"{pct:.0f}%" if abs(pct - round(pct)) < 1e-9 else f"{pct:.2f}%"
    return f"configured limit {shown}"


def _anchor_text(snapshot: dict) -> str:
    anchor = snapshot.get("anchor")
    day = snapshot.get("anchor_date")
    if anchor is None or day is None:
        return "no strategy session anchor"
    return (
        f"strategy session anchor ${anchor:,.2f} dated {day.isoformat()} "
        "(not the broker close)"
    )


def format_daily_breaker(snapshot: dict) -> str:
    name = book_short_name(snapshot.get("book_id"))
    error = snapshot.get("error")
    if error == "state file unreadable":
        return f"Daily Breaker: {name} — safety state unreadable"
    if error == "state file malformed" or error in {"anchor date malformed", "anchor malformed"}:
        return f"Daily Breaker: {name} — safety state malformed"
    if error == "state file unavailable":
        return f"Daily Breaker: {name} — safety state unavailable"
    if error == "no state for this book":
        return f"Daily Breaker: {name} — no safety state for this book"
    if error == "book not selected":
        return "Daily Breaker: book not selected — safety state not shown"

    calendar = snapshot.get("calendar") or {}
    when = []
    if calendar.get("weekend"):
        when.append("weekend, no regular session in progress")
    elif calendar.get("verified") and not calendar.get("in_session"):
        when.append("before the regular session")
    elif not calendar.get("verified"):
        when.append(f"session calendar {calendar.get('reason') or 'unavailable'}")

    relation = snapshot.get("relation")
    limit = _limit_text(snapshot.get("limit_ratio"))
    trip = snapshot.get("trip_recorded")
    if relation == "current" and snapshot.get("calculation") == "under_limit":
        trip_s = "trip recorded" if trip else "no trip recorded"
        change = snapshot["change_pct"]
        return (
            f"Daily Breaker: {name} — {trip_s}; current calculation {change:+.2f}% "
            f"vs {_anchor_text(snapshot)}; under {limit}"
        )
    if relation == "current" and snapshot.get("calculation") == "at_or_over_limit":
        trip_s = "trip recorded" if trip else "no trip recorded in the state file"
        change = snapshot["change_pct"]
        return (
            f"Daily Breaker: {name} — {trip_s}; current calculation {change:+.2f}% "
            f"vs {_anchor_text(snapshot)}; at or over {limit}"
        )
    if relation == "current":
        trip_s = "trip recorded" if trip else "no trip recorded"
        why = {
            "limit_unverified": "current calculation unavailable because the limit is unverified",
            "equity_unverified": "current calculation unavailable because equity is unverified",
        }.get(snapshot.get("calculation"), "current calculation unavailable")
        return f"Daily Breaker: {name} — {trip_s}; {why}; {_anchor_text(snapshot)}; {limit}"
    if relation == "previous":
        trip_s = "trip recorded on that anchor" if trip else "no trip recorded on that anchor"
        prefix = "; ".join(when)
        lead = f"{prefix}; " if prefix else ""
        return (
            f"Daily Breaker: {name} — {lead}previous {_anchor_text(snapshot)}; "
            f"{trip_s}; no current-session anchor; {limit}"
        )
    if relation == "upcoming":
        return (
            f"Daily Breaker: {name} — {_anchor_text(snapshot)} is for the upcoming session; "
            f"regular session not in progress; not an in-session calculation; {limit}"
        )
    if relation == "missing":
        prefix = "; ".join(when)
        lead = f"{prefix}; " if prefix else ""
        return (
            f"Daily Breaker: {name} — {lead}current strategy anchor unavailable; "
            f"no trip recorded; not verified; {limit}"
        )
    prefix = "; ".join(when)
    lead = f"{prefix}; " if prefix else ""
    return (
        f"Daily Breaker: {name} — {lead}{_anchor_text(snapshot)}; "
        "session not verified as current; no current calculation; "
        + ("trip recorded on the stored anchor" if trip else "no trip recorded on the stored anchor")
    )


def _tilt_text(tilt) -> str:
    if isinstance(tilt, str) and tilt.strip():
        return tilt.strip()
    if not isinstance(tilt, dict) or not tilt:
        return ""
    try:
        items = sorted(tilt.items(), key=lambda kv: float(kv[1]), reverse=True)
        return " | ".join(f"{key} {float(value):.0%}" for key, value in items[:6])
    except (TypeError, ValueError):
        return ""


def thinking_rotation_text(
    heartbeat: dict | None,
    *,
    book_id: str | None,
    now: datetime | None = None,
    session_bounds: dict | None = None,
) -> str:
    """Book heartbeat only. A global cached recommendation is never consulted."""
    if not book_id:
        return "no book selected"
    payload = (heartbeat or {}).get("thinking_engine") if isinstance(heartbeat, dict) else None
    if not isinstance(payload, dict) or not payload:
        return "no current recommendation for this book"
    payload_book = payload.get("book_id")
    if payload_book and str(payload_book) != book_id:
        return "no current recommendation for this book"
    if payload.get("enabled") is False:
        return "disabled for this book"
    stamp = payload.get("timestamp") or payload.get("as_of")
    parsed = _parse_aware(stamp) if isinstance(stamp, str) else None
    if not stamp:
        return "recommendation has no timestamp; not shown as current"
    if parsed is None:
        return "recommendation timestamp is not timezone-aware; not shown as current"
    applied = payload.get("applied")
    if applied is True:
        application = "heartbeat marks it applied; not a sleeve setting"
    elif applied is False:
        application = "not applied"
    else:
        application = "application unknown"
    weights = _tilt_text(payload.get("suggested_tilt") or payload.get("recommended_tilt"))
    detail = weights or str(payload.get("narrative") or payload.get("regime_narrative") or "").strip()
    calendar = session_calendar(session_bounds, now or datetime.now(ET))
    current = (
        calendar.get("in_session")
        and calendar.get("in_session_date") == parsed.astimezone(ET).date()
    )
    body = f"{detail}" if detail else "no weights stored"
    if current:
        return (
            f"recommendation {parsed.isoformat()} (not a holding; {application}): {body}"
        )
    return (
        f"historical recommendation {parsed.isoformat()} "
        f"(not a holding; {application}): {body}"
    )


def thinking_pill_snapshot(
    heartbeat: dict | None,
    book_id: str | None,
    now: datetime | None = None,
) -> dict:
    bounds = None
    if isinstance(heartbeat, dict) and isinstance(heartbeat.get("scan_schedule"), dict):
        bounds = heartbeat.get("scan_schedule")
    text = thinking_rotation_text(
        heartbeat, book_id=book_id, now=now, session_bounds=bounds
    )
    if text.endswith("no current recommendation for this book") or text == "no book selected":
        status, label = "NONE", "no current rec"
    elif text.startswith("disabled"):
        status, label = "OFF", "disabled"
    elif "historical recommendation" in text:
        status, label = "HIST", "historical"
    elif "not shown as current" in text:
        status, label = "NONE", "timestamp unverified"
    else:
        status, label = "REC", "recommendation"
    return {"status": status, "detail": label, "label": label, "text": text}
