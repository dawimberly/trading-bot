"""Structured CSV journal for paper-trading data collection."""

import csv
import logging
import os
from datetime import datetime

import config

logger = logging.getLogger(__name__)

JOURNAL_FIELDS = [
    "timestamp",
    "event",
    "symbol",
    "side",
    "regime",
    "pair_key",
    "z_score",
    "equity",
    "cash",
    "notional",
    "exit_reason",
    "entry_hour",
    "notes",
]


def _ensure_header(path):
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=JOURNAL_FIELDS).writeheader()


def _header_fieldnames(path):
    """Use existing CSV header so rows stay aligned with paper_chase_journal columns."""
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            first = f.readline().strip()
        if first:
            names = [c.strip() for c in first.split(",")]
            for col in JOURNAL_FIELDS:
                if col not in names:
                    names.append(col)
            return names
    except Exception as exc:
        logger.debug("journal header read failed for %s: %s", path, exc)
    return list(JOURNAL_FIELDS)


def log_event(
    event,
    *,
    symbol="",
    side="",
    regime="",
    pair_key="",
    z_score="",
    equity="",
    cash="",
    notional="",
    exit_reason="",
    entry_hour="",
    notes="",
    journal_path=None,
):
    path = journal_path or config.PAPER_JOURNAL_CSV
    _ensure_header(path)
    row = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event": event,
        "symbol": symbol,
        "side": side,
        "regime": regime,
        "pair_key": pair_key,
        "z_score": z_score,
        "equity": equity,
        "cash": cash,
        "notional": notional,
        "exit_reason": exit_reason,
        "entry_hour": entry_hour,
        "notes": notes,
    }
    fieldnames = _header_fieldnames(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore").writerow(row)


def log_cycle(regime, equity, cash, crypto_trades, equity_trades, notes="", journal_path=None):
    log_event(
        "cycle",
        regime=regime,
        equity=round(equity, 2),
        cash=round(cash, 2),
        notes=f"crypto={crypto_trades} equity={equity_trades}; {notes}",
        journal_path=journal_path,
    )


def log_signal(symbol, side, regime, pair_key, z_score, equity, notional, journal_path=None):
    log_event(
        "signal",
        symbol=symbol,
        side=side,
        regime=regime,
        pair_key=pair_key,
        z_score=round(z_score, 4) if z_score != "" else "",
        equity=round(equity, 2),
        notional=notional,
        journal_path=journal_path,
    )


def log_exit(symbol, side, reason, equity, journal_path=None, *, exit_reason="", entry_hour=""):
    log_event(
        "exit",
        symbol=symbol,
        side=side,
        equity=round(equity, 2),
        exit_reason=exit_reason or "",
        entry_hour=entry_hour or "",
        notes=reason,
        journal_path=journal_path,
    )
