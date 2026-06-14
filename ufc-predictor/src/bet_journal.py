"""Structured CSV journal for UFC bet signals and alert dispatches."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

JOURNAL_FIELDS = [
    "timestamp",
    "event",
    "event_type",
    "fight",
    "pick",
    "edge_pct",
    "model_prob",
    "stake",
    "bankroll",
    "profile",
    "notes",
]


def _journal_path(path: Path | str | None = None) -> Path:
    return Path(path) if path else config.BET_JOURNAL_CSV


def _ensure_header(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.stat().st_size > 0:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=JOURNAL_FIELDS).writeheader()


def log_journal_row(
    event_type: str,
    *,
    event: str = "",
    fight: str = "",
    pick: str = "",
    edge_pct: float | str = "",
    model_prob: float | str = "",
    stake: float | str = "",
    bankroll: float | str = "",
    profile: str = "",
    notes: str = "",
    journal_path: Path | str | None = None,
) -> None:
    path = _journal_path(journal_path)
    _ensure_header(path)
    row = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "event": event,
        "event_type": event_type,
        "fight": fight,
        "pick": pick,
        "edge_pct": edge_pct,
        "model_prob": model_prob,
        "stake": stake,
        "bankroll": bankroll,
        "profile": profile or config.UFC_PROFILE,
        "notes": notes,
    }
    with path.open("a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=JOURNAL_FIELDS).writerow(row)


def log_signal(
    fight: str,
    pick: str,
    *,
    event: str = "",
    edge_pct: float = 0.0,
    model_prob: float | None = None,
    stake: float = 0.0,
    bankroll: float | None = None,
    notes: str = "",
) -> None:
    log_journal_row(
        "signal",
        event=event,
        fight=fight,
        pick=pick,
        edge_pct=f"{edge_pct:+.1f}" if edge_pct else "",
        model_prob=f"{model_prob:.1%}" if model_prob is not None else "",
        stake=f"{stake:.2f}" if stake else "",
        bankroll=f"{bankroll:.2f}" if bankroll is not None else "",
        notes=notes,
    )


def log_alert_dispatch(
    alert_data: dict[str, Any],
    *,
    status: dict[str, Any] | None = None,
) -> None:
    status = status or {}
    notes = (
        f"singles={alert_data.get('singles_count', 0)} "
        f"parlays={alert_data.get('parlays_count', 0)} "
        f"sent={status.get('sent', False)} "
        f"skip={status.get('skip_reason', '')}"
    )
    log_journal_row(
        "alert_dispatch",
        event=str(alert_data.get("event_name", "")),
        bankroll=alert_data.get("bankroll", ""),
        notes=notes[:500],
    )
    for s in alert_data.get("singles", []):
        log_signal(
            s.get("fight", ""),
            s.get("pick", ""),
            event=str(alert_data.get("event_name", "")),
            edge_pct=float(s.get("edge_pct", 0)),
            model_prob=s.get("prob"),
            stake=float(s.get("suggested_stake", 0)),
            bankroll=alert_data.get("bankroll"),
            notes=s.get("brief") or s.get("reasoning", "")[:200],
        )


def log_watch_tick(
    *,
    iteration: int,
    event_name: str,
    singles: int,
    parlays: int,
    notified: bool,
    block_reason: str = "",
) -> None:
    log_journal_row(
        "watch_tick",
        event=event_name,
        notes=f"iter={iteration} singles={singles} parlays={parlays} notify={notified} {block_reason}",
    )
