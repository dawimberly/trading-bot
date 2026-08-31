"""Read, normalize, and query paper_chase_journal.csv; build position summaries."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)
from modules.cost_basis import sleeve_for_symbol

ROOT = Path(__file__).resolve().parents[1]
PORTAL_PAPER_JOURNAL = (
    ROOT / "data" / "portal" / "users" / "dawimberly" / "books" / "alpaca_paper" / "paper_journal.csv"
)
FORWARD_TAPE_DATE = date(2026, 8, 21)
FORWARD_MARK_NAME = "sleeve_pnl_mark_2026-08-21.json"
VTI_REALIZED_NA = "n/a until first portal fill"
_CT = ZoneInfo("America/Chicago")
_FILL_PNL_CACHE: dict[str, Any] = {}
_VTI_SLEEVE_TOKENS = frozenset({"vti", "core", "vti_core", "voo", "itot"})
_SPY_SLEEVE_TOKENS = frozenset({"spy"})
_AI_BURST_TOKENS = ("ai-burst", "ai_burst", "aiburst", "ai burst", "aiburst")

JOURNAL_COLUMNS = (
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
    "exit_reason",
    "entry_hour",
    "notes",
)

ENTRY_EVENTS = frozenset({"signal", "fill", "game_plan", "entry", "buy"})
EXIT_EVENTS = frozenset({"exit", "sell", "close"})
TRADE_EVENTS = ENTRY_EVENTS | EXIT_EVENTS | frozenset({"game_plan"})


@dataclass
class PositionRow:
    ticker: str
    sleeve: str
    qty: float
    entry_price: float
    current_price: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    market_value: float
    opened_at: datetime | None
    days_held: int | None
    source: str = "alpaca"


def journal_paths(*, extra: Path | str | None = None) -> list[Path]:
    paths: list[Path] = []
    chase = os.getenv("PAPER_CHASE_JOURNAL", "paper_chase_journal.csv")
    paths.append(ROOT / chase)
    paths.append(ROOT / config.PAPER_JOURNAL_CSV)
    if extra:
        paths.append(Path(extra))
    seen: set[Path] = set()
    out: list[Path] = []
    for p in paths:
        rp = p.resolve()
        if rp not in seen and p.is_file():
            seen.add(rp)
            out.append(p)
    if not out:
        for p in paths:
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                out.append(p)
    return out


def _normalize_ticker(raw: Any) -> str:
    sym = str(raw or "").strip()
    if not sym or sym.lower() in ("nan", "none"):
        return ""
    return config.normalize_symbol(sym).upper()


def normalize_journal_df(df) -> Any:
    """Ensure ticker column and typed fields for reliable symbol queries."""
    import pandas as pd

    if df is None or df.empty:
        return pd.DataFrame(columns=list(JOURNAL_COLUMNS))

    out = df.copy()
    for col in JOURNAL_COLUMNS:
        if col not in out.columns:
            out[col] = ""

    if "symbol" in out.columns:
        sym_ticker = out["symbol"].map(_normalize_ticker)
    else:
        sym_ticker = pd.Series([""] * len(out), index=out.index)

    existing = out["ticker"].astype(str).map(_normalize_ticker) if "ticker" in out.columns else ""
    if isinstance(existing, str):
        out["ticker"] = sym_ticker
    else:
        out["ticker"] = existing.where(existing != "", sym_ticker)

    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    for col in ("notional", "qty", "price", "equity", "cash", "z_score"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["event"] = out["event"].astype(str).str.strip().str.lower()
    out["side"] = out["side"].astype(str).str.strip().str.lower()

    def _row_sleeve(r) -> str:
        existing = r.get("sleeve")
        if existing is not None and str(existing).strip() and str(existing).lower() != "nan":
            return str(existing).strip()
        for key in ("ticker", "symbol"):
            val = r.get(key)
            if val is None or (isinstance(val, float) and pd.isna(val)):
                continue
            sym = str(val).strip()
            if sym and sym.lower() != "nan":
                return sleeve_for_symbol(sym)
        return ""

    out["sleeve"] = out.apply(_row_sleeve, axis=1)
    return out


def read_journal(*, path: Path | str | None = None, tail: int | None = None):
    import pandas as pd

    from modules.csv_utils import read_csv_file, read_csv_tail

    if path is not None:
        paths = [Path(path)]
    else:
        paths = journal_paths()

    parts = []
    for p in paths:
        if not p.is_file():
            continue
        try:
            if tail and tail > 0:
                parts.append(read_csv_tail(p, tail))
            else:
                parts.append(read_csv_file(p))
        except Exception as exc:
            logger.warning("journal CSV read failed for %s: %s", p, exc)
            continue
    if not parts:
        return normalize_journal_df(pd.DataFrame())
    merged = pd.concat(parts, ignore_index=True)
    merged = normalize_journal_df(merged)
    if "timestamp" in merged.columns:
        merged = merged.drop_duplicates(
            subset=[c for c in ("timestamp", "event", "ticker", "side", "notional") if c in merged.columns],
            keep="last",
        ).sort_values("timestamp")
    return merged


def filter_ticker(df, ticker: str):
    t = _normalize_ticker(ticker)
    if not t or df.empty:
        return df.iloc[0:0].copy()
    return df.loc[df["ticker"] == t].copy()


def query_ticker_events(ticker: str, *, limit: int = 50, journal_path: Path | str | None = None) -> list[dict]:
    df = read_journal(path=journal_path)
    df = filter_ticker(df, ticker)
    if df.empty:
        return []
    trade = df.loc[df["event"].isin(TRADE_EVENTS)]
    if not trade.empty:
        df = trade
    df = df.sort_values("timestamp", ascending=False).head(limit)
    return df.to_dict(orient="records")


def _is_buy_side(side: str, event: str) -> bool:
    s = str(side or "").lower()
    if s in ("buy", "long", "b"):
        return True
    if s in ("sell", "short", "s"):
        return False
    if event in ENTRY_EVENTS and event not in EXIT_EVENTS:
        return True
    return event not in EXIT_EVENTS


def _is_sell_side(side: str, event: str) -> bool:
    s = str(side or "").lower()
    if s in ("sell", "short", "s", "sell_short"):
        return True
    ev = str(event or "").lower()
    if ev == "fill":
        return False
    return ev in EXIT_EVENTS


def row_is_entry(event: str, side: str) -> bool:
    """Buy-side trade including event=fill with side=buy."""
    ev = str(event or "").strip().lower()
    if ev in ("cycle", "startup", "error", "exit_error", "skip", "halt"):
        return False
    return _is_buy_side(side, ev) and (
        ev in ENTRY_EVENTS or ev in TRADE_EVENTS or ev == "fill"
    )


def row_is_exit(event: str, side: str) -> bool:
    """Sell-side trade including event=fill with side=sell."""
    ev = str(event or "").strip().lower()
    if ev in ("cycle", "startup", "error", "exit_error", "skip", "halt"):
        return False
    return _is_sell_side(side, ev)


def prefer_fill_rows(df):
    """If any fill rows exist, those are SoT; else fall back to signal/exit."""
    if df is None or getattr(df, "empty", True) or "event" not in df.columns:
        return df
    ev = df["event"].astype(str).str.strip().str.lower()
    fills = df.loc[ev == "fill"]
    if not fills.empty:
        return fills
    return df.loc[ev.isin(TRADE_EVENTS)]


def journal_opened_at(df, ticker: str) -> datetime | None:
    """Replay journal buy/sell events; return open time if still logically open."""
    sub = filter_ticker(df, ticker)
    if sub.empty:
        return None
    sub = sub.sort_values("timestamp")
    open_ts: datetime | None = None
    for row in sub.itertuples(index=False):
        ev = str(getattr(row, "event", "") or "").lower()
        side = str(getattr(row, "side", "") or "").lower()
        ts = getattr(row, "timestamp", None)
        if _is_sell_side(side, ev):
            open_ts = None
        elif _is_buy_side(side, ev) and ev in TRADE_EVENTS:
            if ts is not None and not (isinstance(ts, float) and str(ts) == "nan"):
                open_ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    return open_ts


def _order_fill_time(order) -> datetime | None:
    filled = getattr(order, "filled_at", None) or getattr(order, "submitted_at", None)
    if filled is None:
        return None
    if hasattr(filled, "to_pydatetime"):
        filled = filled.to_pydatetime()
    if isinstance(filled, datetime):
        return filled.replace(tzinfo=None) if filled.tzinfo else filled
    return None


def fetch_closed_orders_paginated(client, *, max_pages: int = 40) -> list[Any]:
    """Newest-first pages of closed orders until exhausted (Alpaca limit 500/page)."""
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
    except ImportError:
        return []

    out: list[Any] = []
    until = None
    seen_ids: set[str] = set()
    for _ in range(max(1, int(max_pages))):
        try:
            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                limit=500,
                nested=True,
                until=until,
            )
            batch = list(client.get_orders(filter=req))
        except Exception as exc:
            logger.debug("Alpaca closed-order page failed: %s", exc)
            break
        if not batch:
            break
        new_count = 0
        for order in batch:
            oid = str(getattr(order, "id", "") or "")
            if oid and oid in seen_ids:
                continue
            if oid:
                seen_ids.add(oid)
            out.append(order)
            new_count += 1
        if new_count == 0 or len(batch) < 500:
            break
        oldest = None
        for order in batch:
            ts = _order_fill_time(order)
            if ts is None:
                continue
            if oldest is None or ts < oldest:
                oldest = ts
        if oldest is None:
            break
        until = oldest
    return out


def opened_times_from_orders(
    orders: list[Any],
    symbols: set[str] | None = None,
) -> dict[str, datetime]:
    """Replay fills chronologically; return current-lot open time per symbol."""
    want = {_normalize_ticker(s) for s in symbols} if symbols else None
    by_sym: dict[str, list[tuple[datetime, str, float]]] = {}
    for order in orders:
        sym = _normalize_ticker(getattr(order, "symbol", "") or "")
        if not sym or (want is not None and sym not in want):
            continue
        side = str(getattr(order, "side", "")).split(".")[-1].lower()
        if side not in ("buy", "sell"):
            continue
        qty = float(getattr(order, "filled_qty", None) or 0)
        if qty <= 0:
            continue
        ts = _order_fill_time(order)
        if ts is None:
            continue
        by_sym.setdefault(sym, []).append((ts, side, qty))

    opened: dict[str, datetime] = {}
    for sym, fills in by_sym.items():
        fills.sort(key=lambda x: x[0])
        pos_qty = 0.0
        open_ts: datetime | None = None
        for ts, side, qty in fills:
            if side == "buy":
                if pos_qty <= 1e-12:
                    open_ts = ts
                pos_qty += qty
            else:
                pos_qty -= qty
                if pos_qty <= 1e-12:
                    pos_qty = 0.0
                    open_ts = None
        if open_ts is not None and pos_qty > 1e-12:
            opened[sym] = open_ts
    return opened


def alpaca_opened_at_map(client, symbols: list[str] | set[str]) -> dict[str, datetime]:
    """Batch open times for held symbols (paginated closed-order history)."""
    want = {_normalize_ticker(s) for s in symbols if s}
    if not want:
        return {}
    orders = fetch_closed_orders_paginated(client)
    return opened_times_from_orders(orders, want)


def _first_alpaca_buy_time(client, ticker: str) -> datetime | None:
    """Legacy single-symbol lookup; prefer alpaca_opened_at_map for dashboards."""
    t = _normalize_ticker(ticker)
    return alpaca_opened_at_map(client, {t}).get(t)


def _days_held(opened: datetime | None) -> int | None:
    if opened is None:
        return None
    now = datetime.now()
    o = opened.replace(tzinfo=None) if opened.tzinfo else opened
    return max(0, (now.date() - o.date()).days)


def fetch_alpaca_positions(*, paper: bool = True) -> tuple[list[PositionRow], str | None]:
    try:
        from modules.alpaca_client import get_trading_client
    except ImportError as exc:
        return [], str(exc)

    try:
        client = get_trading_client(paper=paper)
        positions = list(client.get_all_positions())
    except ValueError as exc:
        return [], str(exc)
    except Exception as exc:
        return [], str(exc)

    journal = read_journal(tail=5000)
    held = [_normalize_ticker(p.symbol) for p in positions]
    try:
        opened_map = alpaca_opened_at_map(client, held)
    except Exception as exc:
        logger.debug("alpaca_opened_at_map failed: %s", exc)
        opened_map = {}
    rows: list[PositionRow] = []
    for pos in positions:
        ticker = _normalize_ticker(pos.symbol)
        qty = float(pos.qty or 0)
        if abs(qty) < 1e-12:
            continue
        entry = float(getattr(pos, "avg_entry_price", None) or 0)
        current = float(getattr(pos, "current_price", None) or 0)
        upl = float(getattr(pos, "unrealized_pl", None) or 0)
        upl_pct = float(getattr(pos, "unrealized_plpc", None) or 0) * 100.0
        mv = abs(float(getattr(pos, "market_value", None) or qty * current))

        opened = journal_opened_at(journal, ticker)
        if opened is None:
            opened = opened_map.get(ticker)

        rows.append(
            PositionRow(
                ticker=ticker,
                sleeve=sleeve_for_symbol(ticker),
                qty=qty,
                entry_price=entry,
                current_price=current,
                unrealized_pnl=upl,
                unrealized_pnl_pct=upl_pct,
                market_value=mv,
                opened_at=opened,
                days_held=_days_held(opened),
                source="alpaca",
            )
        )
    rows.sort(key=lambda r: r.market_value, reverse=True)
    return rows, None


def build_position_summary(
    *,
    paper: bool = True,
    ticker: str | None = None,
) -> tuple[list[PositionRow], str | None]:
    rows, err = fetch_alpaca_positions(paper=paper)
    if ticker:
        t = _normalize_ticker(ticker)
        rows = [r for r in rows if r.ticker == t]
    return rows, err


def _money(val: float | None) -> str:
    if val is None:
        return "n/a"
    return f"${val:,.2f}"


def _pct(val: float | None) -> str:
    if val is None:
        return "n/a"
    return f"{val:+.2f}%"


def format_position_row(row: PositionRow) -> str:
    opened = row.opened_at.strftime("%Y-%m-%d") if row.opened_at else "n/a"
    days = str(row.days_held) if row.days_held is not None else "n/a"
    return (
        f"{row.ticker:<6} {row.sleeve:<6} qty {row.qty:>8.4g}  "
        f"entry {_money(row.entry_price):>10}  now {_money(row.current_price):>10}  "
        f"P/L {_money(row.unrealized_pnl):>10} ({_pct(row.unrealized_pnl_pct)})  "
        f"opened {opened}  ({days}d)"
    )


def format_positions_table(
    rows: list[PositionRow],
    *,
    title: str = "Open positions",
    err: str | None = None,
) -> list[str]:
    lines = [f"=== {title} ==="]
    if err:
        lines.append(f"Note: {err}")
    if not rows:
        lines.append("No open positions (or Alpaca unavailable).")
        return lines
    lines.append(
        f"{'Ticker':<6} {'Sleeve':<6} {'Qty':>8}  {'Entry':>10}  {'Current':>10}  "
        f"{'P/L $':>10}  {'P/L %':>8}  {'Opened':>10}  {'Days':>4}"
    )
    lines.append("-" * 88)
    for row in rows:
        opened = row.opened_at.strftime("%Y-%m-%d") if row.opened_at else "n/a"
        days = str(row.days_held) if row.days_held is not None else "n/a"
        lines.append(
            f"{row.ticker:<6} {row.sleeve:<6} {row.qty:>8.4g}  "
            f"{_money(row.entry_price):>10}  {_money(row.current_price):>10}  "
            f"{_money(row.unrealized_pnl):>10}  {_pct(row.unrealized_pnl_pct):>8}  "
            f"{opened:>10}  {days:>4}"
        )
    total_mv = sum(r.market_value for r in rows)
    total_upl = sum(r.unrealized_pnl for r in rows)
    lines.append("-" * 88)
    lines.append(f"{'TOTAL':<6} {'':6} {'':>8}  {'':>10}  {'':>10}  {_money(total_upl):>10}  "
                 f"({len(rows)} positions, MV {_money(total_mv)})")
    return lines


def format_ticker_history(ticker: str, events: list[dict]) -> list[str]:
    t = _normalize_ticker(ticker)
    lines = [f"=== Journal: {t} ==="]
    if not events:
        lines.append("No journal events for this ticker.")
        return lines
    lines.append(
        f"{'Timestamp':<20} {'Event':<10} {'Side':<5} {'Notional':>10}  {'Regime':<28}  Notes"
    )
    lines.append("-" * 100)
    for ev in events:
        ts = str(ev.get("timestamp", ""))[:19]
        notes = str(ev.get("notes", "") or "")[:40]
        regime = str(ev.get("regime", "") or "")[:28]
        notional = ev.get("notional")
        n_s = f"${float(notional):,.0f}" if notional == notional and notional not in ("", None) else ""
        lines.append(
            f"{ts:<20} {str(ev.get('event','')):<10} {str(ev.get('side','')):<5} {n_s:>10}  "
            f"{regime:<28}  {notes}"
        )
    return lines


def _nonempty_cell(val) -> bool:
    if val is None:
        return False
    try:
        import pandas as pd

        if isinstance(val, float) and pd.isna(val):
            return False
    except Exception:
        pass
    s = str(val).strip()
    return bool(s) and s.lower() not in ("nan", "none", "null", "")


def _read_journal_csv_recon(path: Path):
    """Same extra-column map as scripts/analysis/trade_reconciliation.read_journal_csv."""
    analysis = ROOT / "scripts" / "analysis"
    if str(analysis) not in sys.path:
        sys.path.insert(0, str(analysis))
    from trade_reconciliation import read_journal_csv

    return read_journal_csv(path)


def display_sleeve_for_fill(
    *,
    sleeve_raw: Any = "",
    symbol: str = "",
    exit_reason: Any = "",
) -> str:
    """Map a fill/lot to a display sleeve. Leftover VTI is not VTI core."""
    sym = _normalize_ticker(symbol)
    vanguard_left = {"VTI", "VOO", "VEA", "VWO", "VXUS"}
    metal_left = {"IAU", "GLD", "SLV", "CPER"}
    if sym in vanguard_left:
        return "Vanguard leftover"
    if sym in metal_left:
        return "Metal leftover"
    if sym in {"SPY", "QQQ"}:
        return "SPY leftover"
    inferred = ""
    if sym:
        try:
            inferred = str(sleeve_for_symbol(sym) or "").strip().lower()
        except Exception:
            inferred = ""
    if inferred == "core":
        return "Vanguard leftover"
    if inferred == "metal":
        return "Metal leftover"
    if inferred == "spy":
        return "SPY leftover"
    if inferred == "crypto":
        return "Crypto leftover"

    sleeve = str(sleeve_raw or "").strip().lower()
    if sleeve in ("nan", "none", "null"):
        sleeve = ""
    reason = str(exit_reason or "").strip().lower()
    blob = f"{sleeve} {reason}"
    if sleeve:
        if sleeve in _VTI_SLEEVE_TOKENS or sleeve.startswith("vti"):
            return "Vanguard leftover"
        if sleeve in _SPY_SLEEVE_TOKENS:
            return "SPY leftover"
        if any(tok in sleeve for tok in _AI_BURST_TOKENS):
            return "AI-burst"
        if "atr" in blob or "hygiene" in blob:
            return "NYSE"
        return "NYSE"
    return "NYSE"


def _forward_start_ct() -> datetime:
    return datetime(FORWARD_TAPE_DATE.year, FORWARD_TAPE_DATE.month, FORWARD_TAPE_DATE.day, tzinfo=_CT)


def _as_chicago(ts) -> datetime | None:
    if ts is None:
        return None
    try:
        import pandas as pd

        if isinstance(ts, float) and pd.isna(ts):
            return None
        if isinstance(ts, pd.Timestamp):
            if pd.isna(ts):
                return None
            ts = ts.to_pydatetime()
    except Exception:
        pass
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=_CT)
    return ts.astimezone(_CT)


def portal_paper_journal_path() -> Path:
    if PORTAL_PAPER_JOURNAL.is_file():
        return PORTAL_PAPER_JOURNAL
    return PORTAL_PAPER_JOURNAL


def forward_mark_path(journal_path: Path | str | None = None) -> Path:
    parent = Path(journal_path).parent if journal_path else PORTAL_PAPER_JOURNAL.parent
    return parent / FORWARD_MARK_NAME


def _num(val: Any, default: float = 0.0) -> float:
    try:
        if val is None or val == "":
            return default
        x = float(val)
        if x != x:
            return default
        return x
    except (TypeError, ValueError):
        return default


def positions_from_dashboard_df(df) -> list[dict[str, Any]]:
    """Normalize dashboard Alpaca position rows; re-map sleeve via sleeve_for_symbol."""
    out: list[dict[str, Any]] = []
    if df is None or getattr(df, "empty", True):
        return out
    for data in df.to_dict(orient="records"):
        if not isinstance(data, dict):
            continue
        sym = _normalize_ticker(data.get("Ticker") or data.get("ticker") or data.get("symbol"))
        if not sym:
            continue
        qty = _num(data.get("Qty") or data.get("qty"))
        avg = _num(data.get("Entry") or data.get("avg") or data.get("avg_entry_price"))
        mark = _num(data.get("Current") or data.get("mark") or data.get("current_price"))
        value = _num(data.get("Value $") or data.get("value") or data.get("market_value"))
        unrealized = _num(data.get("P&L $") or data.get("unrealized") or data.get("unrealized_pl"))
        cost = _num(data.get("Cost $") or data.get("cost"))
        if cost <= 0 and value and unrealized == unrealized:
            cost = value - unrealized
        if mark <= 0 and qty:
            mark = abs(value / qty) if value else avg
        sleeve = display_sleeve_for_fill(symbol=sym)
        out.append(
            {
                "symbol": sym,
                "sleeve": sleeve,
                "qty": qty,
                "avg": avg,
                "mark": mark,
                "value": abs(value) if value else abs(qty * mark),
                "unrealized": unrealized,
                "cost": abs(cost) if cost else abs(qty * avg),
            }
        )
    return out


def positions_from_alpaca(raw_positions) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pos in raw_positions or []:
        sym = _normalize_ticker(getattr(pos, "symbol", "") or "")
        if not sym:
            continue
        qty = float(getattr(pos, "qty", 0) or 0)
        if abs(qty) < 1e-12:
            continue
        avg = float(getattr(pos, "avg_entry_price", 0) or 0)
        mark = float(getattr(pos, "current_price", 0) or 0)
        value = float(getattr(pos, "market_value", 0) or 0)
        unrealized = float(getattr(pos, "unrealized_pl", 0) or 0)
        cost = float(getattr(pos, "cost_basis", 0) or 0)
        if cost <= 0:
            cost = abs(qty * avg) if avg else (abs(value) - unrealized)
        if mark <= 0 and qty:
            mark = abs(value / qty) if value else avg
        if not value:
            value = abs(qty * mark)
        out.append(
            {
                "symbol": sym,
                "sleeve": display_sleeve_for_fill(symbol=sym),
                "qty": qty,
                "avg": avg,
                "mark": mark,
                "value": abs(value),
                "unrealized": unrealized,
                "cost": abs(cost),
            }
        )
    return out


def _empty_sleeve_bucket() -> dict[str, Any]:
    return {
        "fill_count": 0,
        "realized": 0.0,
        "realized_ok": False,
        "realized_label": "",
        "unrealized": 0.0,
        "unrealized_pct": None,
        "cost": 0.0,
        "value": 0.0,
        "combined": None,
        "since_fill_count": 0,
        "since_realized": 0.0,
        "since_realized_ok": False,
        "since_realized_label": "",
        "since_unrealized": 0.0,
        "since_combined": None,
        "names": 0,
    }


def _load_fill_realized(path: Path) -> dict[str, Any]:
    """event=fill realized sums all-time and since FORWARD_TAPE_DATE. Cached on mtime."""
    key = str(path.resolve()) if path else ""
    try:
        mtime = path.stat().st_mtime if path.is_file() else None
    except OSError:
        mtime = None
    hit = _FILL_PNL_CACHE.get(key)
    if hit is not None and hit.get("mtime") == mtime:
        return hit

    payload: dict[str, Any] = {
        "mtime": mtime,
        "path": str(path) if path else "",
        "warnings": [],
        "fill_count": 0,
        "vti_fills": 0,
        "by_sleeve": {},
        "since_by_sleeve": {},
    }
    if path is None or not path.is_file():
        payload["warnings"].append("journal missing")
        _FILL_PNL_CACHE[key] = payload
        return payload

    df, warnings = _read_journal_csv_recon(path)
    payload["warnings"] = list(warnings or [])
    if df is None or df.empty or "event" not in df.columns:
        _FILL_PNL_CACHE[key] = payload
        return payload

    ev = df["event"].astype(str).str.strip().str.lower()
    fills = df.loc[ev == "fill"].copy()
    payload["fill_count"] = int(len(fills))
    if fills.empty:
        _FILL_PNL_CACHE[key] = payload
        return payload

    import pandas as pd

    fills["timestamp"] = pd.to_datetime(fills["timestamp"], errors="coerce")
    start = _forward_start_ct()
    by_sleeve: dict[str, dict[str, Any]] = {}
    since_by: dict[str, dict[str, Any]] = {}
    vti_fills = 0
    for rec in fills.to_dict(orient="records"):
        sym = _normalize_ticker(rec.get("symbol") or rec.get("ticker") or "")
        sleeve = display_sleeve_for_fill(
            sleeve_raw=rec.get("sleeve"),
            symbol=sym,
            exit_reason=rec.get("exit_reason"),
        )
        if sleeve == "Vanguard leftover" or sleeve == "VTI":
            vti_fills += 1
        bucket = by_sleeve.setdefault(sleeve, {"fill_count": 0, "realized": 0.0})
        bucket["fill_count"] += 1
        if _nonempty_cell(rec.get("realized_pnl")):
            try:
                bucket["realized"] += float(rec["realized_pnl"])
            except (TypeError, ValueError):
                pass
        ts = _as_chicago(rec.get("timestamp"))
        if ts is not None and ts >= start:
            sb = since_by.setdefault(sleeve, {"fill_count": 0, "realized": 0.0})
            sb["fill_count"] += 1
            if _nonempty_cell(rec.get("realized_pnl")):
                try:
                    sb["realized"] += float(rec["realized_pnl"])
                except (TypeError, ValueError):
                    pass
    payload["vti_fills"] = vti_fills
    payload["by_sleeve"] = by_sleeve
    payload["since_by_sleeve"] = since_by
    _FILL_PNL_CACHE[key] = payload
    return payload


def load_forward_mark_snapshot(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("forward mark snapshot read failed: %s", exc)
        return None
    return data if isinstance(data, dict) else None


def ensure_forward_mark_snapshot(
    path: Path,
    *,
    positions: list[dict[str, Any]],
    equity: float | None,
    cash: float | None,
) -> dict[str, Any] | None:
    """Write qty/avg/mark by sleeve once. Never overwrite; not a trade journal."""
    existing = load_forward_mark_snapshot(path)
    if existing is not None:
        return existing
    sleeves: dict[str, dict[str, Any]] = {}
    for p in positions:
        sl = str(p.get("sleeve") or "NYSE")
        row = sleeves.setdefault(
            sl,
            {"qty": 0.0, "cost": 0.0, "value": 0.0, "unrealized": 0.0, "names": 0},
        )
        row["qty"] += float(p.get("qty") or 0)
        row["cost"] += float(p.get("cost") or 0)
        row["value"] += float(p.get("value") or 0)
        row["unrealized"] += float(p.get("unrealized") or 0)
        row["names"] += 1
    for row in sleeves.values():
        qty = float(row["qty"] or 0)
        cost = float(row["cost"] or 0)
        value = float(row["value"] or 0)
        row["avg"] = (cost / qty) if qty else 0.0
        row["mark"] = (value / qty) if qty else 0.0
    payload = {
        "date": FORWARD_TAPE_DATE.isoformat(),
        "as_of": datetime.now(_CT).isoformat(timespec="seconds"),
        "note": "Forward-tape start mark. Not a trade journal.",
        "equity": equity,
        "cash": cash,
        "sleeves": sleeves,
        "positions": [
            {
                "symbol": p.get("symbol"),
                "sleeve": p.get("sleeve"),
                "qty": p.get("qty"),
                "avg": p.get("avg"),
                "mark": p.get("mark"),
                "value": p.get("value"),
                "unrealized": p.get("unrealized"),
                "cost": p.get("cost"),
            }
            for p in positions
        ],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        from modules.safe_io import write_json_atomic

        write_json_atomic(path, payload, indent=2)
    except Exception as exc:
        logger.warning("forward mark snapshot write failed (%s): %s", path, exc)
        return None
    return payload


def _since_unrealized_from_mark(
    positions: list[dict[str, Any]],
    snapshot: dict[str, Any] | None,
) -> dict[str, float]:
    """MTM vs 2026-08-21 marks on still-open inventory. Closed names live in realized."""
    out: dict[str, float] = {
        "NYSE": 0.0,
        "Vanguard leftover": 0.0,
        "Metal leftover": 0.0,
        "VTI": 0.0,
        "SPY": 0.0,
    }
    if not snapshot:
        return out
    old_rows = snapshot.get("positions") or []
    old_by = {str(r.get("symbol") or "").upper(): r for r in old_rows if r.get("symbol")}
    for p in positions:
        sleeve = str(p.get("sleeve") or "NYSE")
        if sleeve not in out:
            continue
        sym = str(p.get("symbol") or "").upper()
        cq = float(p.get("qty") or 0)
        cm = float(p.get("mark") or 0)
        avg = float(p.get("avg") or 0)
        old = old_by.get(sym)
        if old is None:
            out[sleeve] += float(p.get("unrealized") or 0)
            continue
        sq = float(old.get("qty") or 0)
        sm = float(old.get("mark") or 0)
        sign = 1.0 if cq >= 0 else -1.0
        oqty = min(abs(cq), abs(sq))
        mtm = (cm - sm) * oqty * sign
        extra = abs(cq) - abs(sq)
        if extra > 0:
            mtm += extra * (cm - avg) * sign
        out[sleeve] += mtm
    return out


def _fmt_money(val: float | None, *, signed: bool = True) -> str:
    if val is None:
        return "n/a"
    if not signed:
        return f"${val:,.2f}"
    sign = "-" if val < 0 else "+"
    return f"{sign}${abs(val):,.2f}"


def compute_paper_sleeve_pnl(
    *,
    journal_path: Path | str | None = None,
    positions: list[dict[str, Any]] | None = None,
    equity: float | None = None,
    cash: float | None = None,
    spy_off: bool = True,
    write_snapshot: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """All-time + since-2026-08-21 paper sleeve P&L. Compute-on-read; no extra journal."""
    path = Path(journal_path) if journal_path is not None else portal_paper_journal_path()
    pos = list(positions or [])
    fills = _load_fill_realized(path)
    mark_path = forward_mark_path(path)
    snapshot = load_forward_mark_snapshot(mark_path)
    snapshot_existed = snapshot is not None
    if write_snapshot and snapshot is None and pos:
        snapshot = ensure_forward_mark_snapshot(
            mark_path, positions=pos, equity=equity, cash=cash
        )
        # First mark equals current book → since-unrealized starts at ~0.
    since_u = _since_unrealized_from_mark(pos, snapshot)

    sleeves = {
        name: _empty_sleeve_bucket()
        for name in ("NYSE", "Vanguard leftover", "Metal leftover")
    }
    off_held = {"SPY leftover": 0, "Crypto leftover": 0}
    for p in pos:
        sl = str(p.get("sleeve") or "NYSE")
        if sl in ("SPY", "SPY leftover"):
            off_held["SPY leftover"] += 1
            continue
        if sl in ("Crypto", "Crypto leftover"):
            off_held["Crypto leftover"] += 1
            continue
        if sl == "VTI":
            sl = "Vanguard leftover"
        if sl not in sleeves:
            sl = "NYSE"
        row = sleeves[sl]
        row["unrealized"] += float(p.get("unrealized") or 0)
        row["cost"] += float(p.get("cost") or 0)
        row["value"] += float(p.get("value") or 0)
        row["names"] += 1

    by = fills.get("by_sleeve") or {}
    since_by = fills.get("since_by_sleeve") or {}
    vti_fills = int(fills.get("vti_fills") or 0)

    for name, row in sleeves.items():
        src = by.get(name) or {}
        since_src = since_by.get(name) or {}
        row["fill_count"] = int(src.get("fill_count") or 0)
        row["realized"] = float(src.get("realized") or 0)
        row["since_fill_count"] = int(since_src.get("fill_count") or 0)
        row["since_realized"] = float(since_src.get("realized") or 0)
        if row["cost"] > 0:
            row["unrealized_pct"] = 100.0 * row["unrealized"] / row["cost"]
        row["since_unrealized"] = float(since_u.get(name) or 0.0)

        if name == "Vanguard leftover" and vti_fills <= 0:
            row["realized_ok"] = False
            row["realized_label"] = "leftover (not core)"
            row["since_realized_ok"] = False
            row["since_realized_label"] = "leftover (not core)"
            row["combined"] = row["unrealized"]
            row["since_combined"] = row["since_unrealized"]
        else:
            row["realized_ok"] = True
            row["realized_label"] = _fmt_money(row["realized"])
            row["combined"] = row["realized"] + row["unrealized"]
            row["since_realized_ok"] = True
            row["since_realized_label"] = _fmt_money(row["since_realized"])
            row["since_combined"] = row["since_realized"] + row["since_unrealized"]

    as_of = now or datetime.now(_CT)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=_CT)
    else:
        as_of = as_of.astimezone(_CT)
    eq = float(equity) if equity else 0.0
    ch = float(cash) if cash is not None else None
    cash_pct = (100.0 * ch / eq) if ch is not None and eq > 0 else None

    return {
        "as_of": as_of.strftime("%Y-%m-%d %H:%M CT"),
        "as_of_iso": as_of.isoformat(timespec="seconds"),
        "journal_path": str(path),
        "snapshot_path": str(mark_path),
        "snapshot_existed": snapshot_existed,
        "forward_date": FORWARD_TAPE_DATE.isoformat(),
        "fill_count": int(fills.get("fill_count") or 0),
        "vti_fills": vti_fills,
        "vti_realized_label": (
            "leftover (not core)" if vti_fills <= 0 else sleeves["Vanguard leftover"]["realized_label"]
        ),
        "warnings": list(fills.get("warnings") or []),
        "spy_status": "OFF" if spy_off else "ON",
        "crypto_status": "OFF",
        "ai_burst": "research / 0",
        "equity": eq if eq else None,
        "cash": ch,
        "cash_pct": cash_pct,
        "sleeves": sleeves,
        "off_held": off_held,
    }


def format_paper_sleeve_pnl_table(report: dict[str, Any], *, compact: bool = False) -> str:
    """Paper dashboard text. Leftover VTI is not core. SPY/crypto OFF (not $0)."""
    sleeves = report.get("sleeves") or {}
    nyse = sleeves.get("NYSE") or _empty_sleeve_bucket()
    left = sleeves.get("Vanguard leftover") or sleeves.get("VTI") or _empty_sleeve_bucket()
    metal = sleeves.get("Metal leftover") or _empty_sleeve_bucket()
    cash = report.get("cash")
    cash_pct = report.get("cash_pct")
    if cash is None:
        cash_s = "Cash n/a"
    elif cash_pct is not None:
        cash_s = f"Cash {_fmt_money(cash, signed=False)} ({cash_pct:.1f}% of equity)"
    else:
        cash_s = f"Cash {_fmt_money(cash, signed=False)}"

    def _u(row: dict[str, Any]) -> str:
        pct = row.get("unrealized_pct")
        if pct is None:
            return _fmt_money(row.get("unrealized") or 0.0)
        return f"{_fmt_money(row.get('unrealized') or 0.0)} ({pct:+.2f}%)"

    def _comb(row: dict[str, Any], key: str) -> str:
        val = row.get(key)
        return "n/a" if val is None else _fmt_money(val)

    nyse_real = nyse.get("realized_label") or _fmt_money(nyse.get("realized") or 0.0)
    left_lab = left.get("realized_label") or "leftover (not core)"
    since = report.get("forward_date") or FORWARD_TAPE_DATE.isoformat()
    lines = [
        f"NYSE   realized {nyse_real} · unrealized {_u(nyse)} · combined {_comb(nyse, 'combined')}"
        f"  |  fills {int(nyse.get('fill_count') or 0)} (portal event=fill)",
        f"Vanguard leftover  {left_lab} · unrealized {_u(left)}  (not VTI core)",
        f"Metal leftover  unrealized {_u(metal)}",
        f"SPY {report.get('spy_status') or 'OFF'} · Crypto {report.get('crypto_status') or 'OFF'}"
        f" · {cash_s} · as-of {report.get('as_of') or '—'}",
    ]
    if not compact:
        lines.insert(0, f"Sleeve P&L (paper)  journal={report.get('journal_path')}")
        lines.append(f"since {since}")
    return "\n".join(lines)


def fetch_paper_positions_for_pnl(*, paper: bool = True) -> tuple[list[dict[str, Any]], str | None]:
    """Alpaca paper positions via the existing client. No extra journal I/O."""
    try:
        from modules.alpaca_client import get_trading_client
    except ImportError as exc:
        return [], str(exc)
    try:
        client = get_trading_client(paper=paper)
        raw = list(client.get_all_positions())
    except ValueError as exc:
        return [], str(exc)
    except Exception as exc:
        return [], str(exc)
    return positions_from_alpaca(raw), None


def print_paper_sleeve_pnl(*, paper: bool = True) -> dict[str, Any]:
    """CLI helper: compute + print the paper sleeve P&L table."""
    positions, err = fetch_paper_positions_for_pnl(paper=paper)
    equity = cash = None
    try:
        from modules.alpaca_client import get_trading_client

        acct = get_trading_client(paper=paper).get_account()
        equity = float(getattr(acct, "equity", 0) or 0)
        cash = float(getattr(acct, "cash", 0) or 0)
    except Exception:
        pass
    report = compute_paper_sleeve_pnl(
        journal_path=portal_paper_journal_path(),
        positions=positions,
        equity=equity,
        cash=cash,
        spy_off=True,
        write_snapshot=True,
    )
    if err:
        report.setdefault("warnings", []).append(f"positions: {err}")
    print(format_paper_sleeve_pnl_table(report, compact=False))
    return report
