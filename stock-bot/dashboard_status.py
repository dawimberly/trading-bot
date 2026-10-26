"""Display-only labels for the desktop dashboard.

These helpers format status text. They do not read broker accounts, place
orders, or change strategy settings. VTI targets come from the book env the
bot process is given, never from a leftover heartbeat sleeve cap.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_SESSION_CLOSED = (
    "market closed",
    "session closed",
    "closed session",
    "equity session closed",
    "outside market",
    "nyse closed",
)


def _flag(env: Mapping[str, Any], key: str) -> bool | None:
    if key not in env:
        return None
    raw = env.get(key)
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def _frac(env: Mapping[str, Any], *keys: str) -> float | None:
    """Configured fraction. Missing key is None; an explicit 0 stays 0."""
    for key in keys:
        if key not in env:
            continue
        raw = env.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val > 1.0:
            val = val / 100.0
        return min(0.95, max(0.0, val))
    return None


def vti_target_from_env(env: Mapping[str, Any] | None, *, paper: bool) -> dict[str, Any]:
    """Effective VTI target for one book's subprocess env.

    Disabled → target 0. Enabled → the configured percent (or a dynamic
    floor–ceiling when those bounds are set). Heartbeat sleeve caps are not
    an input. A missing flag is left unknown instead of inventing 0% or 33%.
    """
    env = env or {}
    enabled = _flag(env, "VTI_CORE_ENABLED")
    if paper:
        pct = _frac(env, "PAPER_VTI_CORE_PCT", "VTI_CORE_PCT")
        dynamic = _flag(env, "PAPER_DYNAMIC_VTI")
        if dynamic is None:
            dynamic = _flag(env, "PAPER_DYNAMIC_VTI_ENABLED")
    else:
        pct = _frac(env, "VTI_CORE_PCT", "LIVE_VTI_CORE_PCT")
        dynamic = False
    dynamic = bool(dynamic)
    if enabled is False:
        return {
            "enabled": False,
            "target": 0.0,
            "dynamic": False,
            "label": "VTI off · target 0%",
            "source": "book_env",
        }
    if enabled is not True:
        return {
            "enabled": None,
            "target": None,
            "dynamic": dynamic,
            "label": "VTI target not in book env",
            "source": "book_env",
        }
    if dynamic:
        floor = _frac(env, "DYNAMIC_VTI_PAPER_FLOOR")
        ceiling = _frac(env, "DYNAMIC_VTI_PAPER_CEILING")
        if floor is not None and ceiling is not None and (floor > 0 or ceiling > 0):
            target = pct if pct is not None and pct > 0 else None
            return {
                "enabled": True,
                "target": target,
                "dynamic": True,
                "label": f"VTI dynamic {floor:.0%}–{ceiling:.0%}",
                "source": "book_env",
            }
    if pct is None:
        return {
            "enabled": True,
            "target": None,
            "dynamic": dynamic,
            "label": "VTI on · target not in book env",
            "source": "book_env",
        }
    return {
        "enabled": True,
        "target": pct,
        "dynamic": dynamic and pct > 0,
        "label": f"VTI on · target {pct:.0%}",
        "source": "book_env",
    }


def tape_vti_label(vti: Mapping[str, Any] | None) -> str | None:
    """Cyan-bar phrase. Omit the label when the book env did not say."""
    if not vti or vti.get("source") != "book_env":
        return None
    if vti.get("enabled") is None:
        return None
    label = str(vti.get("label") or "").strip()
    return label or None


def account_mode_text(*, paper: bool) -> str:
    """Account venue. This is not the bot process state."""
    return "Account: Paper" if paper else "Account: Live"


def primary_issue(
    *,
    running: bool,
    halted: bool = False,
    heartbeat_stale: bool = False,
    thinking_error: bool = False,
    market_closed: bool = False,
) -> str | None:
    """One headline. A closed session is not an incident."""
    del market_closed
    if not running:
        return "Bot stopped"
    if halted:
        return "Risk halt"
    if thinking_error:
        return "Thinking service error"
    if heartbeat_stale:
        return "Stale heartbeat"
    return None


def health_display_tone(
    *,
    health_color: str,
    running: bool,
    market_closed: bool,
    heartbeat_stale: bool,
    thinking_error: bool,
) -> str:
    """How to color the health chip. Market closed stays calm."""
    if thinking_error or (running and (heartbeat_stale or health_color == "red")):
        return "alert"
    if not running:
        return "muted"
    if market_closed:
        return "calm"
    if health_color == "yellow":
        return "caution"
    if health_color == "green":
        return "ok"
    return "muted"


def gate_is_session_closed(reason: str | None) -> bool:
    text = (reason or "").lower().replace("_", " ")
    return any(bit in text for bit in _SESSION_CLOSED)


def _parse_dt(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _tz_short(ts: datetime | None) -> str | None:
    if ts is None or ts.tzinfo is None:
        return None
    name = ts.tzname() or ""
    zone = str(getattr(ts.tzinfo, "key", "") or "")
    if name in {"EST", "EDT", "ET"} or zone in {"America/New_York", "US/Eastern"}:
        return "ET"
    return name or None


def format_as_of(raw: Any) -> str | None:
    ts = raw if isinstance(raw, datetime) else _parse_dt(raw)
    if ts is None:
        if raw is None or str(raw).strip() == "":
            return None
        return str(raw)[:19]
    clock = ts.strftime("%b %d, %I:%M %p").replace(", 0", ", ")
    tz = _tz_short(ts)
    if tz:
        return f"{clock} {tz}"
    return clock


def _secs_phrase(secs: float) -> str:
    if secs <= 0:
        return "Soon"
    hours = int(secs // 3600)
    mins = int((secs % 3600) // 60)
    if hours >= 24:
        return f"{hours // 24}d {hours % 24}h"
    if hours <= 0:
        return f"{max(1, mins)}m"
    return f"{hours}h {mins}m"


def _clock_now(now: datetime | None, target: datetime) -> datetime:
    if target.tzinfo is not None:
        if now is None:
            return datetime.now(target.tzinfo)
        if now.tzinfo is None:
            return now.replace(tzinfo=target.tzinfo)
        return now.astimezone(target.tzinfo)
    current = now or datetime.now()
    if current.tzinfo is not None:
        return current.replace(tzinfo=None)
    return current


def market_clock(
    heartbeat: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """NYSE session clock. Closed is informational, not a health failure.

    Timezone is included only when the timestamp carries one, or when the
    clock falls back to the NYSE regular-hours helper (defined in ET).
    """
    hb = heartbeat or {}
    scan = hb.get("scan_schedule") if isinstance(hb.get("scan_schedule"), dict) else {}
    is_open = scan.get("market_open")
    if is_open is None and "equity_session_open" in hb:
        is_open = hb.get("equity_session_open")
    if is_open:
        return {
            "is_open": True,
            "title": "NYSE",
            "value": "Open",
            "sentence": "NYSE session open",
            "level": "ok",
            "tz": None,
        }

    phrase: str | None = None
    tz: str | None = None
    raw = scan.get("session_open") or scan.get("orders_start")
    target = _parse_dt(raw) if raw else None
    if target is not None:
        tz = _tz_short(target)
        phrase = _secs_phrase((_clock_now(now, target) - target).total_seconds() * -1)
    else:
        try:
            from modules.market_hours import nyse_rth_status

            status = nyse_rth_status(now)
        except Exception:
            status = None
        if status:
            if status.get("is_open"):
                return {
                    "is_open": True,
                    "title": "NYSE",
                    "value": "Open",
                    "sentence": "NYSE session open",
                    "level": "ok",
                    "tz": "ET",
                }
            nxt = status.get("next_open")
            tz = "ET"
            if nxt is not None:
                phrase = _secs_phrase(
                    (_clock_now(now, nxt) - nxt).total_seconds() * -1
                )

    if is_open is False or phrase:
        if phrase and phrase not in {"Soon"}:
            tz_bit = f" {tz}" if tz else ""
            value = f"Opens in {phrase}{tz_bit}"
            sentence = f"NYSE closed — opens in {phrase}{tz_bit}"
        elif phrase == "Soon":
            value = "Opening"
            sentence = "NYSE opening"
        else:
            tz_bit = f" ({tz})" if tz else ""
            value = f"Closed{tz_bit}"
            sentence = f"NYSE closed{tz_bit}"
        return {
            "is_open": False,
            "title": "NEXT OPEN",
            "value": value,
            "sentence": sentence,
            "level": "info",
            "tz": tz,
        }
    return {
        "is_open": None,
        "title": "NYSE",
        "value": "—",
        "sentence": "",
        "level": "info",
        "tz": None,
    }


def format_source_freshness(
    *,
    heartbeat_raw: Any = None,
    heartbeat_stale: bool = False,
    account_raw: Any = None,
    positions_raw: Any = None,
) -> dict[str, Any]:
    """Heartbeat age and broker fetch time are separate clauses.

    A stale heartbeat must not be worded as stale broker positions.
    """
    heartbeat_at = format_as_of(heartbeat_raw)
    account_at = format_as_of(account_raw)
    positions_at = format_as_of(positions_raw)
    if heartbeat_at:
        heartbeat = f"Heartbeat as of {heartbeat_at}"
        if heartbeat_stale:
            heartbeat += " (stale)"
    else:
        heartbeat = "Heartbeat: no timestamp"
    if account_at and positions_at and account_at != positions_at:
        broker = f"Account as of {account_at} · Positions as of {positions_at}"
    elif account_at or positions_at:
        broker = f"Broker data as of {positions_at or account_at}"
    else:
        broker = "Broker data: not fetched"
    return {
        "heartbeat": heartbeat,
        "broker": broker,
        "heartbeat_stale": bool(heartbeat_stale),
    }


def format_risk_budget(*, equity: float, risk_frac: float, order_cap: float) -> str:
    """Risk budget is the configured fraction of equity, not order notional."""
    return (
        f"Risk budget: ${equity * float(risk_frac):,.2f} "
        f"({float(risk_frac):.1%} of ${equity:,.2f} equity)\n"
        f"Max order notional: ${float(order_cap):,.2f}"
    )


def relabel_equity_change(text: str) -> str:
    """'Since Start' is the equity change from the status start point."""
    return (text or "").replace("Since Start:", "Equity change since start:")
