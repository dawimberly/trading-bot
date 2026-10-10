"""Structured CSV journal for paper-trading data collection."""

import csv
import json
import logging
import os
from collections import deque
from datetime import datetime
from pathlib import Path

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
    "qty",
    "price",
    "sleeve",
    "ticker",
    "order_id",
    "book",
    "exit_reason",
    "entry_hour",
    "realized_pnl",
    "realized_pnl_pct",
    "is_partial",
    "notes",
    "trade_id",
    "entry_price",
    "hold_minutes",
]

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_OPEN_TRADES = _ROOT / "data" / "open_trade_ids.json"
_OPEN_TRADES_PATH = _DEFAULT_OPEN_TRADES


def _one_line(text) -> str:
    return " ".join(str(text or "").split())


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _blank(value) -> bool:
    text = str(value or "").strip()
    return text == "" or text.lower() in ("nan", "none")


def compute_realized_pnl(qty, price, entry_price, *, is_sell: bool):
    """Dollar P&L for a sell versus its entry. Buys and missing entries are None."""
    if not is_sell or entry_price is None:
        return None
    try:
        q = float(qty)
        px = float(price)
        en = float(entry_price)
    except (TypeError, ValueError):
        return None
    if q == 0 or en == 0:
        return None
    return (px - en) * q


def _ensure_header(path):
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
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


def _open_trades_path(journal_path: str) -> Path:
    if Path(_OPEN_TRADES_PATH) != _DEFAULT_OPEN_TRADES:
        return Path(_OPEN_TRADES_PATH)
    return Path(journal_path).with_name("open_trade_ids.json")


def _load_lots(path: Path) -> dict[str, deque]:
    lots: dict[str, deque] = {}
    try:
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            for sym, rows in (payload.get("lots") or {}).items():
                lots[str(sym).upper()] = deque(rows)
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        logger.debug("open trade ledger read failed: %s", exc)
    return lots


def _save_lots(path: Path, lots: dict[str, deque]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "lots": {sym: list(rows) for sym, rows in lots.items() if rows}
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.debug("open trade ledger write failed: %s", exc)


def _hour_bucket(when: datetime | None) -> str:
    if when is None:
        return ""
    return f"{when.hour:02d}:00"


def _parse_ts(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


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
    **extra,
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
        "notes": _one_line(notes),
    }
    for key, value in extra.items():
        if key in JOURNAL_FIELDS and key not in row:
            row[key] = _cell(value)
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


def log_exit(
    symbol,
    side,
    reason,
    equity,
    journal_path=None,
    *,
    exit_reason="",
    entry_hour="",
    entry_price="",
    hold_minutes="",
    trade_id="",
):
    path = journal_path or config.PAPER_JOURNAL_CSV
    if _blank(trade_id) or _blank(entry_price):
        peeked = _peek_lot(symbol, path)
        if peeked:
            trade_id = trade_id or peeked.get("trade_id") or ""
            entry_price = entry_price or peeked.get("entry_price") or ""
            entry_hour = entry_hour or peeked.get("entry_hour") or ""
            if _blank(hold_minutes) and peeked.get("opened_at"):
                opened = _parse_ts(peeked["opened_at"])
                if opened is not None:
                    hold_minutes = int((datetime.now() - opened).total_seconds() // 60)
    log_event(
        "exit",
        symbol=symbol,
        side=side,
        equity=round(equity, 2),
        exit_reason=exit_reason or "",
        entry_hour=entry_hour or "",
        entry_price=_cell(entry_price),
        hold_minutes=_cell(hold_minutes),
        trade_id=_cell(trade_id),
        notes=reason,
        journal_path=journal_path,
    )


def _peek_lot(symbol: str, journal_path: str) -> dict | None:
    lots = _load_lots(_open_trades_path(journal_path))
    rows = lots.get(str(symbol or "").upper())
    if not rows:
        return None
    return dict(rows[0])


def _full_exit(is_partial) -> bool:
    text = str(is_partial).strip().lower()
    return text in ("", "0", "false", "no")


def log_fill(
    symbol,
    side,
    *,
    qty="",
    price="",
    notional="",
    sleeve="",
    reason="",
    order_id="",
    equity="",
    cash="",
    book="",
    realized_pnl="",
    realized_pnl_pct="",
    is_partial="",
    journal_path=None,
    regime="",
    pair_key="",
    z_score="",
    entry_price="",
    entry_hour="",
    hold_minutes="",
    trade_id="",
    ticker="",
):
    """Append one observational fill. A full sell also appends trade_closed."""
    path = journal_path or config.PAPER_JOURNAL_CSV
    sym = str(symbol or "").strip().upper()
    side_l = str(side or "").strip().lower()
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    ledger = _open_trades_path(path)
    lots = _load_lots(ledger)
    matched: dict | None = None

    if side_l == "buy" and sym:
        tid = _cell(trade_id) or _cell(order_id) or f"{sym}-{stamp}"
        px = entry_price if not _blank(entry_price) else price
        hour = _cell(entry_hour) or _hour_bucket(now)
        lots.setdefault(sym, deque()).append(
            {
                "trade_id": tid,
                "symbol": sym,
                "qty": _cell(qty),
                "entry_price": _cell(px),
                "entry_hour": hour,
                "opened_at": stamp,
                "order_id": _cell(order_id),
            }
        )
        trade_id = tid
        entry_price = px
        entry_hour = hour
        _save_lots(ledger, lots)
    elif side_l == "sell" and sym:
        matched = _consume_lot(lots, sym, qty)
        if matched:
            trade_id = trade_id or matched.get("trade_id") or ""
            entry_price = entry_price or matched.get("entry_price") or ""
            entry_hour = entry_hour or matched.get("entry_hour") or ""
            if _blank(hold_minutes) and matched.get("opened_at"):
                opened = _parse_ts(matched["opened_at"])
                if opened is not None:
                    hold_minutes = int((now - opened).total_seconds() // 60)
            if _blank(realized_pnl) and not _blank(entry_price):
                pnl = compute_realized_pnl(qty, price, entry_price, is_sell=True)
                if pnl is not None:
                    realized_pnl = round(pnl, 2)
            if _blank(realized_pnl_pct) and not _blank(entry_price):
                try:
                    realized_pnl_pct = round(
                        (float(price) / float(entry_price) - 1.0) * 100.0, 4
                    )
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
        _save_lots(ledger, lots)

    note = _one_line(reason)
    common = dict(
        symbol=sym or symbol,
        side=side_l or side,
        regime=regime,
        pair_key=pair_key,
        z_score=z_score,
        equity=equity,
        cash=cash,
        notional=notional,
        qty=qty,
        price=price,
        sleeve=sleeve,
        ticker=ticker or sym or symbol,
        order_id=order_id,
        book=book,
        exit_reason=reason if side_l == "sell" else "",
        entry_hour=entry_hour,
        realized_pnl=realized_pnl,
        realized_pnl_pct=realized_pnl_pct,
        is_partial=is_partial,
        notes=note,
        trade_id=trade_id,
        entry_price=entry_price,
        hold_minutes=hold_minutes,
        journal_path=path,
    )
    log_event("fill", **common)
    if side_l == "sell" and _full_exit(is_partial):
        log_event("trade_closed", **common)


def _consume_lot(lots: dict[str, deque], symbol: str, qty) -> dict | None:
    rows = lots.get(symbol)
    if not rows:
        return None
    try:
        remaining = float(qty)
    except (TypeError, ValueError):
        remaining = 0.0
    if remaining <= 0:
        remaining = float(rows[0].get("qty") or 0)
    first = dict(rows[0])
    while remaining > 1e-9 and rows:
        lot = rows[0]
        try:
            have = float(lot.get("qty") or 0)
        except (TypeError, ValueError):
            have = 0.0
        if have <= remaining + 1e-9:
            remaining -= have
            rows.popleft()
        else:
            lot["qty"] = str(have - remaining)
            remaining = 0.0
    if not rows:
        lots.pop(symbol, None)
    return first


def backfill_hold_fields(path: str | Path) -> dict:
    """Fill blank trade_id, entry_price, hold_minutes, and entry_hour from fills.

    Does not change prices, quantities, or realized dollars. Sells match buys
    FIFO. Exit rows copy the nearest sell fill of the same symbol.
    """
    path = Path(path)
    original = path.read_text(encoding="utf-8")
    rows, fieldnames = _read_journal_rows(original)
    for col in ("trade_id", "entry_price", "hold_minutes", "entry_hour"):
        if col not in fieldnames:
            fieldnames.append(col)

    stats = {"buys": 0, "sells_matched": 0, "exits_matched": 0, "cells": 0}
    lots: dict[str, deque] = {}
    sell_marks: list[tuple[datetime, str, dict]] = []

    def stamp(row) -> datetime | None:
        return _parse_ts(row.get("timestamp") or "")

    order = sorted(
        range(len(rows)),
        key=lambda i: stamp(rows[i]) or datetime.min,
    )
    for i in order:
        row = rows[i]
        event = str(row.get("event") or "").strip().lower()
        side = str(row.get("side") or "").strip().lower()
        sym = str(row.get("symbol") or row.get("ticker") or "").strip().upper()
        when = stamp(row)
        if event != "fill" or not sym:
            continue
        if side == "buy":
            _backfill_buy(row, sym, when, lots, stats)
        elif side == "sell":
            matched = _backfill_sell(row, sym, when, lots, stats)
            if matched:
                sell_marks.append((when or datetime.min, sym, dict(row)))

    for row in rows:
        event = str(row.get("event") or "").strip().lower()
        if event != "exit":
            continue
        sym = str(row.get("symbol") or row.get("ticker") or "").strip().upper()
        when = stamp(row)
        if not sym or when is None:
            continue
        nearest = _nearest_sell(sell_marks, sym, when)
        if not nearest:
            continue
        wrote = False
        for key in ("trade_id", "entry_price", "hold_minutes", "entry_hour"):
            wrote = _set_blank(row, key, nearest.get(key) or "", stats) or wrote
        if wrote:
            stats["exits_matched"] += 1

    if stats["cells"] == 0:
        stats["written"] = False
        return stats

    updated = _render_journal(fieldnames, rows)
    latest = path.read_text(encoding="utf-8")
    if latest != original:
        if latest.startswith(original):
            extra = latest[len(original) :]
            if extra and not extra.startswith("\n"):
                stats["written"] = False
                stats["skipped"] = "journal grew mid-line"
                return stats
            updated = updated + extra
        else:
            stats["written"] = False
            stats["skipped"] = "journal changed while reading"
            return stats
    tmp = path.with_suffix(".csv.tmp")
    tmp.write_text(updated, encoding="utf-8")
    tmp.replace(path)
    stats["written"] = True
    return stats


def _read_journal_rows(text: str) -> tuple[list[dict], list[str]]:
    reader = csv.DictReader(text.splitlines())
    fieldnames = list(reader.fieldnames or [])
    return list(reader), fieldnames


def _render_journal(fieldnames: list[str], rows: list[dict]) -> str:
    from io import StringIO

    buf = StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=fieldnames,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in fieldnames})
    return buf.getvalue()


def _set_blank(row: dict, key: str, value, stats: dict) -> bool:
    if _blank(value) or not _blank(row.get(key)):
        return False
    row[key] = _cell(value)
    stats["cells"] += 1
    return True


def _fmt_px(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _backfill_buy(row, sym, when, lots, stats) -> None:
    try:
        qty = float(row.get("qty") or 0)
        px = float(row.get("price") or 0)
    except (TypeError, ValueError):
        return
    if qty <= 0 or px <= 0:
        return
    tid = str(row.get("order_id") or "").strip() or f"{sym}-{row.get('timestamp')}"
    hour = _hour_bucket(when)
    _set_blank(row, "trade_id", tid, stats)
    _set_blank(row, "entry_price", _fmt_px(px), stats)
    _set_blank(row, "entry_hour", hour, stats)
    lots.setdefault(sym, deque()).append(
        {
            "trade_id": str(row.get("trade_id") or tid),
            "qty": qty,
            "entry_price": str(row.get("entry_price") or _fmt_px(px)),
            "entry_hour": str(row.get("entry_hour") or hour),
            "opened_at": str(row.get("timestamp") or ""),
        }
    )
    stats["buys"] += 1


def _backfill_sell(row, sym, when, lots, stats) -> bool:
    try:
        qty = float(row.get("qty") or 0)
        px = float(row.get("price") or 0)
    except (TypeError, ValueError):
        qty, px = 0.0, 0.0
    matched = _consume_lot(lots, sym, qty)
    if not matched:
        return False
    _set_blank(row, "trade_id", matched.get("trade_id") or "", stats)
    _set_blank(row, "entry_price", matched.get("entry_price") or "", stats)
    _set_blank(row, "entry_hour", matched.get("entry_hour") or "", stats)
    opened = _parse_ts(matched.get("opened_at") or "")
    if opened is not None and when is not None:
        minutes = int((when - opened).total_seconds() // 60)
        _set_blank(row, "hold_minutes", str(max(0, minutes)), stats)
    stats["sells_matched"] += 1
    _ = px
    return True


def _nearest_sell(marks, symbol: str, when: datetime) -> dict | None:
    best = None
    best_gap = None
    for sold_at, sym, row in marks:
        if sym != symbol:
            continue
        gap = abs((sold_at - when).total_seconds())
        if gap > 15 * 60:
            continue
        if best_gap is None or gap < best_gap:
            best = row
            best_gap = gap
    return best
