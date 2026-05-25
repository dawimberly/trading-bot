"""Structured CSV journal for paper-trading data collection."""

import csv
import os
from datetime import datetime

import config

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
    "notes",
]


def _ensure_header(path):
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=JOURNAL_FIELDS).writeheader()


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
    notes="",
):
    path = config.PAPER_JOURNAL_CSV
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
        "notes": notes,
    }
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=JOURNAL_FIELDS).writerow(row)


def log_cycle(regime, equity, cash, crypto_trades, equity_trades, notes=""):
    log_event(
        "cycle",
        regime=regime,
        equity=round(equity, 2),
        cash=round(cash, 2),
        notes=f"crypto={crypto_trades} equity={equity_trades}; {notes}",
    )


def log_signal(symbol, side, regime, pair_key, z_score, equity, notional):
    log_event(
        "signal",
        symbol=symbol,
        side=side,
        regime=regime,
        pair_key=pair_key,
        z_score=round(z_score, 4) if z_score != "" else "",
        equity=round(equity, 2),
        notional=notional,
    )


def log_exit(symbol, side, reason, equity):
    log_event(
        "exit",
        symbol=symbol,
        side=side,
        equity=round(equity, 2),
        notes=reason,
    )
