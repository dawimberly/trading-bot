"""Heartbeat file for watch-mode ops monitoring."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import config
from src.safe_io import read_json_file, write_json_atomic


def write_heartbeat(
    *,
    status: str = "ok",
    event_name: str = "",
    iteration: int = 0,
    singles_count: int = 0,
    parlays_count: int = 0,
    last_alert_sent: bool = False,
    block_reason: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "profile": config.UFC_PROFILE,
        "event_name": event_name,
        "iteration": iteration,
        "singles_count": singles_count,
        "parlays_count": parlays_count,
        "last_alert_sent": last_alert_sent,
        "block_reason": block_reason,
        **(extra or {}),
    }
    write_json_atomic(config.HEARTBEAT_PATH, payload)


def read_heartbeat() -> dict[str, Any]:
    return read_json_file(config.HEARTBEAT_PATH)


def heartbeat_age_minutes() -> float | None:
    hb = read_heartbeat()
    ts = hb.get("updated_at")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except (ValueError, TypeError):
        return None
