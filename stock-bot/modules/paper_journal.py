"""Read, normalize, and query paper_chase_journal.csv; build position summaries."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger(__name__)
from modules.cost_basis import sleeve_for_symbol

ROOT = Path(__file__).resolve().parents[1]

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
    if s in ("sell", "short", "s"):
        return True
    return event in EXIT_EVENTS


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


def _first_alpaca_buy_time(client, ticker: str) -> datetime | None:
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
    except ImportError:
        return None

    t = _normalize_ticker(ticker)
    try:
        req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=500, nested=True)
        orders = list(client.get_orders(filter=req))
    except Exception as exc:
        logger.debug("Alpaca closed-order lookup failed for %s: %s", t, exc)
        return None

    times: list[datetime] = []
    for order in orders:
        if _normalize_ticker(getattr(order, "symbol", "")) != t:
            continue
        side = str(getattr(order, "side", "")).split(".")[-1].lower()
        if side != "buy":
            continue
        qty = float(getattr(order, "filled_qty", None) or 0)
        if qty <= 0:
            continue
        filled = getattr(order, "filled_at", None) or getattr(order, "submitted_at", None)
        if filled is None:
            continue
        if hasattr(filled, "to_pydatetime"):
            filled = filled.to_pydatetime()
        if isinstance(filled, datetime):
            times.append(filled.replace(tzinfo=None) if filled.tzinfo else filled)
    return min(times) if times else None


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
            opened = _first_alpaca_buy_time(client, ticker)

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
