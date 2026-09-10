"""Structured CSV journal for paper-trading data collection."""

from __future__ import annotations

import csv
import json
import logging
import os
import uuid
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

_OPEN_TRADES_PATH = Path(__file__).resolve().parents[1] / "data" / "open_trade_ids.json"

# Map free-text / notes fragments → stable exit_reason codes.
_EXIT_REASON_ALIASES: tuple[tuple[str, str], ...] = (
    ("smart_atr_stop", "smart_atr_stop"),
    ("atr_stop", "atr_stop"),
    ("smart_size_reduce", "smart_size_reduce"),
    ("nyse_fat_loser", "nyse_fat_loser_trim"),
    ("fat_loser", "nyse_fat_loser_trim"),
    ("concentration_guard", "concentration_guard_trim"),
    ("take_profit", "take_profit"),
    ("partial_1r", "partial_1r"),
    ("stop_loss", "stop_loss"),
    ("time_exit", "time_exit"),
    ("max_hold", "max_hold"),
    ("rebalance", "rebalance"),
    ("vti_", "vti_rebalance"),
)


def normalize_exit_reason(reason: str, *, fallback: str = "") -> str:
    """Collapse free-text exit notes into a stable exit_reason code."""
    text = " ".join(str(reason or "").lower().split())
    if not text:
        return str(fallback or "")
    # Already a short code?
    if " " not in text and len(text) <= 40 and text.replace("_", "").isalnum():
        return text
    for needle, code in _EXIT_REASON_ALIASES:
        if needle in text:
            return code
    return str(fallback or text[:48])


def _one_line(s) -> str:
    """Collapse free text so CSV rows cannot pick up multiline Alpaca JSON."""
    return " ".join(str(s or "").split())


def _ensure_header(path):
    """Create journal or upgrade header so new JOURNAL_FIELDS actually persist."""
    path = str(path)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=JOURNAL_FIELDS).writeheader()
        return
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            first = f.readline().strip()
        if not first:
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=JOURNAL_FIELDS).writeheader()
            return
        names = [c.strip() for c in first.split(",")]
        missing = [c for c in JOURNAL_FIELDS if c not in names]
        if not missing:
            return
        # Rewrite once so trade_id / entry_price / hold_minutes land in the header.
        with open(path, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        fieldnames = list(names)
        for col in JOURNAL_FIELDS:
            if col not in fieldnames:
                fieldnames.append(col)
        tmp = path + ".schema_tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            for row in rows:
                w.writerow(row)
        os.replace(tmp, path)
        logger.info("journal schema upgraded %s (+%s)", path, ",".join(missing))
    except Exception:
        logger.debug("journal schema upgrade skipped for %s", path, exc_info=True)


def _header_fieldnames(path):
    """Use existing CSV header so rows stay aligned; ensure JOURNAL_FIELDS present."""
    _ensure_header(path)
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


def _load_open_trades() -> dict:
    try:
        if _OPEN_TRADES_PATH.is_file():
            data = json.loads(_OPEN_TRADES_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        logger.debug("open_trade_ids read failed", exc_info=True)
    return {}


def _save_open_trades(data: dict) -> None:
    try:
        _OPEN_TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)
        from modules.safe_io import write_json_atomic

        write_json_atomic(_OPEN_TRADES_PATH, data)
    except Exception:
        logger.debug("open_trade_ids write failed", exc_info=True)


def register_open_trade(
    symbol: str,
    *,
    entry_price: float | str,
    qty: float | str = "",
    sleeve: str = "",
    trade_id: str = "",
) -> str:
    """Record / refresh open lot for round-trip tracking. Returns trade_id."""
    sym = str(symbol or "").upper().strip()
    if not sym:
        return trade_id or ""
    tid = (trade_id or "").strip() or uuid.uuid4().hex[:12]
    store = _load_open_trades()
    prev = store.get(sym) if isinstance(store.get(sym), dict) else {}
    if prev.get("trade_id") and not trade_id:
        tid = str(prev["trade_id"])
    try:
        px = float(entry_price)
    except (TypeError, ValueError):
        px = float(prev.get("entry_price") or 0) if prev else 0.0
    store[sym] = {
        "trade_id": tid,
        "entry_ts": prev.get("entry_ts") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "entry_price": px,
        "qty": qty,
        "sleeve": sleeve or prev.get("sleeve") or "",
    }
    _save_open_trades(store)
    return tid


def pop_open_trade(symbol: str) -> dict:
    """Remove open lot (full exit). Returns prior meta or {}."""
    sym = str(symbol or "").upper().strip()
    store = _load_open_trades()
    meta = store.pop(sym, None)
    _save_open_trades(store)
    return meta if isinstance(meta, dict) else {}


def peek_open_trade(symbol: str) -> dict:
    sym = str(symbol or "").upper().strip()
    meta = _load_open_trades().get(sym)
    return meta if isinstance(meta, dict) else {}


def _hold_minutes_since(entry_ts: str) -> str:
    if not entry_ts:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            start = datetime.strptime(str(entry_ts)[:19], fmt)
            return str(max(0, int((datetime.now() - start).total_seconds() // 60)))
        except ValueError:
            continue
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
    ticker="",
    order_id="",
    book="",
    exit_reason="",
    entry_hour="",
    realized_pnl="",
    realized_pnl_pct="",
    is_partial="",
    notes="",
    trade_id="",
    entry_price="",
    hold_minutes="",
    journal_path=None,
):
    path = journal_path or config.PAPER_JOURNAL_CSV
    _ensure_header(path)
    ticker = ticker or symbol
    row = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event": event,
        "symbol": symbol,
        "side": side,
        "regime": _one_line(regime),
        "pair_key": _one_line(pair_key),
        "z_score": z_score,
        "equity": equity,
        "cash": cash,
        "notional": notional,
        "qty": qty,
        "price": price,
        "sleeve": sleeve,
        "ticker": ticker,
        "order_id": order_id,
        "book": book,
        "exit_reason": _one_line(exit_reason),
        "entry_hour": entry_hour,
        "realized_pnl": realized_pnl,
        "realized_pnl_pct": realized_pnl_pct,
        "is_partial": is_partial,
        "notes": _one_line(notes),
        "trade_id": trade_id,
        "entry_price": entry_price,
        "hold_minutes": hold_minutes,
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
    code = normalize_exit_reason(exit_reason or reason)
    open_meta = peek_open_trade(symbol)
    log_event(
        "exit",
        symbol=symbol,
        side=side,
        equity=round(equity, 2),
        exit_reason=code,
        entry_hour=entry_hour or "",
        notes=reason,
        trade_id=open_meta.get("trade_id", ""),
        entry_price=open_meta.get("entry_price", ""),
        hold_minutes=_hold_minutes_since(str(open_meta.get("entry_ts") or "")),
        journal_path=journal_path,
    )


def compute_realized_pnl(qty, fill_price, avg_entry, *, is_sell: bool):
    """Dollar PnL on a sell vs avg entry. None if not a sell or inputs missing."""
    if not is_sell:
        return None
    try:
        q = float(qty)
        px = float(fill_price)
        entry = float(avg_entry)
    except (TypeError, ValueError):
        return None
    if q == 0 or px <= 0 or entry <= 0:
        return None
    return round(q * (px - entry), 2)


def log_fill(
    symbol,
    side,
    *,
    qty="",
    price="",
    notional="",
    sleeve="",
    reason="",
    pair_key="",
    order_id="",
    equity="",
    cash="",
    regime="",
    book="",
    exit_reason="",
    entry_hour="",
    realized_pnl="",
    realized_pnl_pct="",
    is_partial="",
    notes="",
    journal_path=None,
    trade_id="",
    entry_price="",
    hold_minutes="",
):
    """Ground-truth Alpaca fill row. Observational — callers must try/except."""
    side_l = str(side or "").lower()
    is_sell = side_l in ("sell", "sell_short")
    note_text = notes or reason
    exit_code = ""
    if is_sell:
        exit_code = normalize_exit_reason(
            exit_reason or reason, fallback=normalize_exit_reason(note_text)
        )

    tid = trade_id
    entry_px = entry_price
    hold = hold_minutes
    open_meta: dict = {}

    if not is_sell:
        try:
            px = float(price) if price not in ("", None) else 0.0
        except (TypeError, ValueError):
            px = 0.0
        tid = register_open_trade(
            symbol, entry_price=px, qty=qty, sleeve=sleeve, trade_id=tid
        )
        entry_px = px if px else entry_px
    else:
        open_meta = peek_open_trade(symbol)
        if open_meta:
            tid = tid or open_meta.get("trade_id", "")
            entry_px = entry_px or open_meta.get("entry_price", "")
            hold = hold or _hold_minutes_since(str(open_meta.get("entry_ts") or ""))
        partial = str(is_partial).strip() in ("1", "true", "True", "yes")
        if not partial and open_meta:
            pop_open_trade(symbol)

    log_event(
        "fill",
        symbol=symbol,
        side=side_l,
        regime=regime,
        pair_key=pair_key or reason,
        equity=equity,
        cash=cash,
        notional=notional,
        qty=qty,
        price=price,
        sleeve=sleeve,
        ticker=symbol,
        order_id=order_id,
        book=book,
        exit_reason=exit_code,
        entry_hour=entry_hour,
        realized_pnl=realized_pnl,
        realized_pnl_pct=realized_pnl_pct,
        is_partial=is_partial,
        notes=note_text,
        trade_id=tid,
        entry_price=entry_px,
        hold_minutes=hold,
        journal_path=journal_path,
    )

    # One clean round-trip row for win/loss analysis (full exits with PnL).
    partial_flag = str(is_partial).strip().lower() in ("1", "true", "yes")
    if is_sell and not partial_flag:
        try:
            pnl_f = float(realized_pnl) if realized_pnl not in ("", None) else None
        except (TypeError, ValueError):
            pnl_f = None
        if pnl_f is not None:
            log_event(
                "trade_closed",
                symbol=symbol,
                side=side_l,
                regime=regime,
                equity=equity,
                cash=cash,
                notional=notional,
                qty=qty,
                price=price,
                sleeve=sleeve,
                ticker=symbol,
                order_id=order_id,
                book=book,
                exit_reason=exit_code,
                entry_hour=entry_hour,
                realized_pnl=realized_pnl,
                realized_pnl_pct=realized_pnl_pct,
                is_partial="0",
                notes=note_text,
                trade_id=tid,
                entry_price=entry_px,
                hold_minutes=hold,
                journal_path=journal_path,
            )


def log_ops_event(kind: str, *, notes: str = "", **fields) -> None:
    """Lightweight ops row (network/sleep/gap) into the paper journal + events.log."""
    extra = " ".join(f"{k}={v}" for k, v in sorted(fields.items()) if v not in ("", None))
    note = _one_line(f"{notes} {extra}".strip())
    try:
        log_event("ops", notes=f"{kind}:{note}" if note else kind)
    except Exception:
        logger.debug("log_ops_event journal failed", exc_info=True)
    try:
        from modules.logging_utils import log_event as _ev

        _ev(kind, notes=note, **fields)
    except Exception:
        logger.debug("log_ops_event events.log failed", exc_info=True)
