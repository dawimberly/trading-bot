"""Structured CSV journal for paper-trading data collection."""

import csv
import os
from datetime import datetime

import config

JOURNAL_FIELDS = [
    "timestamp",
    "event",
    "symbol",
    "ticker",
    "side",
    "regime",
    "pair_key",
    "z_score",
    "equity",
    "cash",
    "notional",
    "qty",
    "price",
    "sleeve",
    "notes",
]


def _ensure_header(path):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=JOURNAL_FIELDS).writeheader()
        return
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
    if header and set(JOURNAL_FIELDS).issubset(set(header)):
        return
    try:
        import pandas as pd

        df = pd.read_csv(path, low_memory=False)
        for col in JOURNAL_FIELDS:
            if col not in df.columns:
                df[col] = ""
        df = df[list(JOURNAL_FIELDS)]
        df.to_csv(path, index=False)
    except Exception:
        pass


def _sleeve_label(symbol: str) -> str:
    if not symbol:
        return ""
    try:
        from modules.cost_basis import sleeve_for_symbol

        return sleeve_for_symbol(symbol)
    except Exception:
        return ""


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
    qty="",
    price="",
    sleeve="",
    notes="",
    journal_path=None,
):
    path = journal_path or config.PAPER_JOURNAL_CSV
    _ensure_header(path)
    sym = str(symbol or "").strip()
    ticker = config.normalize_symbol(sym).upper() if sym else ""
    row = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event": event,
        "symbol": symbol,
        "ticker": ticker,
        "side": side,
        "regime": regime,
        "pair_key": pair_key,
        "z_score": z_score,
        "equity": equity,
        "cash": cash,
        "notional": notional,
        "qty": qty,
        "price": price,
        "sleeve": sleeve or _sleeve_label(sym),
        "notes": notes,
    }
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=JOURNAL_FIELDS).writerow(row)


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


def log_exit(symbol, side, reason, equity, journal_path=None):
    log_event(
        "exit",
        symbol=symbol,
        side=side or "sell",
        equity=round(equity, 2),
        notes=reason,
        journal_path=journal_path,
    )


def log_fill(
    symbol,
    side,
    *,
    qty,
    price,
    regime="",
    equity="",
    notional="",
    notes="",
    journal_path=None,
):
    """Record an Alpaca fill with queryable ticker/qty/price columns."""
    n = notional
    if n in ("", None) and qty not in ("", None) and price not in ("", None):
        try:
            n = round(float(qty) * float(price), 2)
        except (TypeError, ValueError):
            n = ""
    log_event(
        "fill",
        symbol=symbol,
        ticker=config.normalize_symbol(symbol).upper() if symbol else "",
        side=side,
        regime=regime,
        equity=equity,
        notional=n,
        qty=qty,
        price=price,
        notes=notes,
        journal_path=journal_path,
    )
