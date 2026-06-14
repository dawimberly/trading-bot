"""Session safety: daily loss circuit breaker for alerts and watch mode."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

import config
from src.safe_io import read_json_file, write_json_atomic

logger = logging.getLogger(__name__)

_entry_block_reason: str | None = None


def _state_path() -> str:
    return str(config.CIRCUIT_BREAKER_STATE_PATH)


def daily_loss_limit_pct() -> float:
    return config.profile_value("daily_loss_limit_fraction")


def update_session_bankroll_anchor(bankroll: float | None, *, session_key: str | None = None) -> None:
    """Record opening bankroll for the current card session (resets daily)."""
    if bankroll is None or bankroll <= 0:
        return
    today = date.today().isoformat()
    key = session_key or today
    state = read_json_file(_state_path())
    session = state.setdefault(key, {})
    if session.get("session_date") != today:
        session["session_date"] = today
        session["bankroll_open"] = round(float(bankroll), 4)
        session.pop("circuit_tripped", None)
        session.pop("loss_pct", None)
        state[key] = session
        write_json_atomic(_state_path(), state)


def daily_loss_circuit_tripped(
    bankroll: float | None,
    *,
    session_key: str | None = None,
) -> tuple[bool, str, float]:
    """
    True when intraday/session loss exceeds profile limit.
    Returns (tripped, reason, loss_pct).
    """
    if bankroll is None or bankroll <= 0:
        return False, "", 0.0
    if not config.CIRCUIT_BREAKER_ENABLED:
        return False, "", 0.0

    today = date.today().isoformat()
    key = session_key or today
    update_session_bankroll_anchor(bankroll, session_key=key)
    state = read_json_file(_state_path())
    session = state.get(key, {})
    open_br = float(session.get("bankroll_open") or bankroll)
    if open_br <= 0:
        return False, "", 0.0

    loss_pct = (open_br - float(bankroll)) / open_br
    limit = daily_loss_limit_pct()
    if loss_pct < limit - 1e-9:
        return False, "", loss_pct

    reason = (
        f"daily loss circuit breaker ({loss_pct:.2%} >= {limit:.2%} limit, "
        f"profile={config.UFC_PROFILE})"
    )
    session["circuit_tripped"] = True
    session["circuit_tripped_at"] = datetime.now(timezone.utc).isoformat()
    session["loss_pct"] = round(loss_pct, 6)
    state[key] = session
    write_json_atomic(_state_path(), state)
    logger.warning(reason)
    return True, reason, loss_pct


def set_entry_block(reason: str | None) -> None:
    global _entry_block_reason
    _entry_block_reason = reason


def entry_block_active() -> bool:
    return bool(_entry_block_reason)


def entry_block_reason() -> str:
    return _entry_block_reason or ""


def check_alerts_allowed(
    bankroll: float | None = None,
    *,
    drawdown_halted: bool = False,
    drawdown_reason: str = "",
) -> tuple[bool, str]:
    """Combined gate for new alerts: circuit breaker + drawdown halt + manual block."""
    if entry_block_active():
        return False, entry_block_reason()
    if drawdown_halted:
        return False, drawdown_reason or "peak drawdown halt active"
    if bankroll is not None:
        tripped, reason, _ = daily_loss_circuit_tripped(bankroll)
        if tripped:
            set_entry_block(reason)
            return False, reason
    return True, ""


def get_circuit_status() -> dict[str, Any]:
    state = read_json_file(_state_path())
    today = date.today().isoformat()
    session = state.get(today, {})
    limit = daily_loss_limit_pct()
    loss_pct = session.get("loss_pct")
    return {
        "profile": config.UFC_PROFILE,
        "limit_pct": round(limit * 100, 1),
        "bankroll_open": session.get("bankroll_open"),
        "loss_pct": round(float(loss_pct) * 100, 2) if loss_pct is not None else None,
        "tripped": bool(session.get("circuit_tripped")),
        "date": session.get("session_date"),
        "entry_block": entry_block_reason(),
    }
