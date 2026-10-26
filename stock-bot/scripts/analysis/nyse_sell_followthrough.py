"""Measure-only: paper NYSE sells vs later daily closes.

Portal journal event=fill, side=sell, NYSE sleeve, since 2026-08-19.
No orders. No .env / ATR / min-hold / sleeve changes.

Usage (from stock-bot/):
  python scripts/analysis/nyse_sell_followthrough.py
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

import config  # noqa: E402
from modules.data_loader import (  # noqa: E402
    _fetch_alpaca_daily_closes,
    _fetch_yfinance_daily_closes,
    _load_table_close,
    _normalize_daily_index,
)
from modules.paper_journal import (  # noqa: E402
    PORTAL_PAPER_JOURNAL,
    display_sleeve_for_fill,
)
from trade_reconciliation import _nonempty_cell, read_journal_csv  # noqa: E402

CT = ZoneInfo("America/Chicago")
SINCE = date(2026, 8, 19)
OUT_MD = Path(__file__).with_name("nyse_sell_followthrough_last.md")
STOP_TOKENS = ("stop", "atr")
_BOOK_JOURNALS = {
    "paper": PORTAL_PAPER_JOURNAL,
    "paper_v2": PORTAL_PAPER_JOURNAL.parent.parent / "alpaca_paper_v2" / "paper_journal.csv",
}


def _as_ct(ts) -> datetime | None:
    if ts is None or (isinstance(ts, float) and pd.isna(ts)):
        return None
    t = pd.Timestamp(ts)
    if pd.isna(t):
        return None
    if t.tzinfo is None:
        return t.to_pydatetime().replace(tzinfo=CT)
    return t.to_pydatetime().astimezone(CT)


def _num(val: Any) -> float | None:
    if not _nonempty_cell(val):
        return None
    try:
        x = float(val)
    except (TypeError, ValueError):
        return None
    if x != x:
        return None
    return x


def _is_stop(reason: str) -> bool:
    r = str(reason or "").strip().lower()
    return any(tok in r for tok in STOP_TOKENS)


def _as_close_series(raw) -> pd.Series:
    if raw is None or getattr(raw, "empty", True):
        return pd.Series(dtype=float)
    s = pd.to_numeric(raw, errors="coerce").dropna()
    if s.empty:
        return pd.Series(dtype=float)
    s.index = _normalize_daily_index(s.index)
    return s[~s.index.duplicated(keep="last")].sort_index()


def _load_daily_closes(symbol: str, start: date, end: date) -> pd.Series:
    """Sqlite daily + 5m last, yfinance fill. Alpaca SIP recent bars 403 on this key."""
    daily = pd.Series(dtype=float)
    live_daily = pd.Series(dtype=float)
    db = config.resolve_db_path()
    if Path(db).is_file():
        conn = sqlite3.connect(str(db), timeout=30)
        try:
            daily = _as_close_series(_load_table_close(conn, f"{symbol}_daily"))
            live = _load_table_close(conn, symbol)
            if live is not None and not getattr(live, "empty", True):
                live = pd.to_numeric(live, errors="coerce").dropna()
                live.index = pd.DatetimeIndex(live.index)
                live_daily = _as_close_series(live.resample("1D").last().dropna())
        except Exception:
            pass
        finally:
            conn.close()
    yf = pd.Series(dtype=float)
    try:
        yf = _as_close_series(_fetch_yfinance_daily_closes(symbol, max_years=1))
    except Exception:
        yf = pd.Series(dtype=float)
    alpaca = pd.Series(dtype=float)
    try:
        need_start = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        need_end = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)
        alpaca = _as_close_series(_fetch_alpaca_daily_closes(symbol, start=need_start, end=need_end))
    except Exception:
        alpaca = pd.Series(dtype=float)
    # Prefer exchange daily / yfinance; 5m last only fills missing dates (e.g. today).
    out = yf
    if not alpaca.empty:
        out = alpaca.combine_first(out) if not out.empty else alpaca
    if not daily.empty:
        out = daily.combine_first(out) if not out.empty else daily
    if not live_daily.empty:
        out = out.combine_first(live_daily) if not out.empty else live_daily
    if out.empty:
        return out
    return out[~out.index.duplicated(keep="last")].sort_index()


def _sessions_after(closes: pd.Series, sell_d: date) -> pd.Series:
    if closes is None or closes.empty:
        return pd.Series(dtype=float)
    s = closes.copy()
    s.index = pd.DatetimeIndex(s.index).normalize()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s[s.index > pd.Timestamp(sell_d)]


def _later_px(closes: pd.Series, sell_d: date, n_sessions: int) -> tuple[float | None, date | None, str]:
    """Nth session close after sell date, or last available in that window."""
    after = _sessions_after(closes, sell_d)
    if after.empty:
        return None, None, "none"
    if len(after) >= n_sessions:
        px = float(after.iloc[n_sessions - 1])
        d = pd.Timestamp(after.index[n_sessions - 1]).date()
        return px, d, "close"
    px = float(after.iloc[-1])
    d = pd.Timestamp(after.index[-1]).date()
    return px, d, "last"


def _max_within_sessions(closes: pd.Series, sell_d: date, n_sessions: int) -> tuple[float | None, date | None]:
    after = _sessions_after(closes, sell_d)
    if after.empty:
        return None, None
    window = after.iloc[:n_sessions]
    idx = window.idxmax()
    return float(window.loc[idx]), pd.Timestamp(idx).date()


def _fifo_hold_minutes(
    lots: list[tuple[datetime, float]],
    sell_ts: datetime,
    sell_qty: float,
) -> tuple[float | None, list[tuple[datetime, float]]]:
    """Consume oldest lots; hold from the first lot used. Returns (minutes, remaining lots)."""
    if sell_qty <= 0 or not lots:
        return None, lots
    remain = sell_qty
    first_ts: datetime | None = None
    new_lots: list[tuple[datetime, float]] = []
    for ts, qty in lots:
        if remain <= 1e-12:
            new_lots.append((ts, qty))
            continue
        take = min(qty, remain)
        if take > 0 and first_ts is None:
            first_ts = ts
        leftover = qty - take
        remain -= take
        if leftover > 1e-12:
            new_lots.append((ts, leftover))
    if first_ts is None:
        return None, new_lots
    delta = sell_ts - first_ts
    return delta.total_seconds() / 60.0, new_lots


def _pct(later: float | None, sell_px: float | None) -> float | None:
    if later is None or sell_px is None or sell_px == 0:
        return None
    return 100.0 * (later / sell_px - 1.0)


def _fmt_px(v: float | None) -> str:
    return "—" if v is None else f"{v:.2f}"


def _fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v:+.2f}%"


def _fmt_hold(m: float | None) -> str:
    if m is None:
        return "—"
    if m >= 1440:
        return f"{m/1440:.1f}d"
    if m >= 60:
        return f"{m/60:.1f}h"
    return f"{m:.0f}m"


def run(journal_path: Path | None = None, out_md: Path | None = None) -> list[dict[str, Any]]:
    path = journal_path or PORTAL_PAPER_JOURNAL
    df, warnings = read_journal_csv(path)
    if df.empty:
        print("no journal rows")
        return []

    ev = df["event"].astype(str).str.strip().str.lower()
    work = df.loc[ev.isin(["fill", "signal", "entry", "buy"])].copy()
    work["event_n"] = work["event"].astype(str).str.strip().str.lower()
    work["ts"] = work["timestamp"].map(_as_ct)
    work["side_n"] = work["side"].astype(str).str.strip().str.lower()
    work["sym"] = work.apply(
        lambda r: str(r.get("symbol") or r.get("ticker") or "").strip().upper(), axis=1
    )
    work["qty_n"] = work["qty"].map(lambda v: abs(_num(v) or 0.0))
    work["px_n"] = work["price"].map(_num)
    work["pnl_n"] = work["realized_pnl"].map(_num) if "realized_pnl" in work.columns else None
    work["sleeve_disp"] = work.apply(
        lambda r: display_sleeve_for_fill(
            sleeve_raw=r.get("sleeve"),
            symbol=r.get("sym"),
            exit_reason=r.get("exit_reason"),
        ),
        axis=1,
    )
    work = work.dropna(subset=["ts"]).sort_values("ts")

    lots: dict[str, list[tuple[datetime, float]]] = {}
    last_buy: dict[str, datetime] = {}
    rows: list[dict[str, Any]] = []
    for rec in work.to_dict(orient="records"):
        sym = rec["sym"]
        ts = rec["ts"]
        side = rec["side_n"]
        qty = float(rec["qty_n"] or 0)
        event_n = rec["event_n"]
        if side in ("buy", "b", "long"):
            last_buy[sym] = ts
            if event_n == "fill" and qty > 0:
                lots.setdefault(sym, []).append((ts, qty))
            continue
        if event_n != "fill" or side not in ("sell", "s", "sell_short"):
            continue
        hold, lots[sym] = _fifo_hold_minutes(lots.get(sym, []), ts, qty)
        if hold is None and last_buy.get(sym) is not None:
            hold = (ts - last_buy[sym]).total_seconds() / 60.0
        if rec.get("sleeve_disp") != "NYSE":
            continue
        sell_d = ts.date()
        if sell_d < SINCE:
            continue
        reason = str(rec.get("exit_reason") or "").strip() or str(rec.get("notes") or "").strip()
        rows.append(
            {
                "symbol": sym,
                "exit_reason": reason,
                "sell_ct": ts,
                "sell_px": rec.get("px_n"),
                "realized_pnl": rec.get("pnl_n"),
                "hold_min": hold,
                "qty": qty,
            }
        )

    symbols = sorted({r["symbol"] for r in rows})
    start = SINCE - timedelta(days=5)
    end = date.today() + timedelta(days=1)
    closes_map: dict[str, pd.Series] = {}
    for sym in symbols:
        closes_map[sym] = _load_daily_closes(sym, start, end)

    flagged: list[str] = []
    for r in rows:
        closes = closes_map.get(r["symbol"], pd.Series(dtype=float))
        sell_d = r["sell_ct"].date()
        sell_px = r["sell_px"]
        for n, key in ((1, "1d"), (5, "5d"), (10, "10d")):
            px, d, how = _later_px(closes, sell_d, n)
            r[f"{key}_px"] = px
            r[f"{key}_date"] = d
            r[f"{key}_how"] = how
            r[f"{key}_pct"] = _pct(px, sell_px)
        mx, mx_d = _max_within_sessions(closes, sell_d, 5)
        r["max_5d_px"] = mx
        r["max_5d_date"] = mx_d
        r["max_5d_pct"] = _pct(mx, sell_px)
        r["stopped"] = _is_stop(r["exit_reason"])
        r["flag_stop_bounce"] = bool(
            r["stopped"] and r["max_5d_pct"] is not None and r["max_5d_pct"] >= 5.0
        )
        if r["flag_stop_bounce"]:
            flagged.append(
                f"{r['symbol']} {r['exit_reason']} sold {r['sell_ct'].strftime('%m-%d %H:%M')} "
                f"then {r['max_5d_pct']:+.1f}% by {mx_d} (within 5d)"
            )

    _print_table(rows, warnings, path, flagged)
    _write_md(rows, warnings, path, flagged, out_md=out_md)
    return rows


def _later_cell(row: dict[str, Any], key: str) -> str:
    px = row.get(f"{key}_px")
    pct = row.get(f"{key}_pct")
    how = row.get(f"{key}_how") or ""
    d = row.get(f"{key}_date")
    if px is None:
        return "—"
    tag = "" if how == "close" else f" {how}"
    ds = d.isoformat()[5:] if d else ""
    return f"{_fmt_px(px)} ({_fmt_pct(pct)}){tag} {ds}".strip()


def _print_table(
    rows: list[dict[str, Any]],
    warnings: list[str],
    path: Path,
    flagged: list[str],
) -> None:
    print(f"SoT: {path}")
    print(f"Paper NYSE sells event=fill side=sell since {SINCE.isoformat()}  n={len(rows)}")
    if warnings:
        print("journal:", "; ".join(warnings))
    hdr = (
        f"{'#':>2} {'symbol':<6} {'exit_reason':<26} {'sell CT':<16} {'px':>8} "
        f"{'pnl':>8} {'hold':>6} {'1d later':<28} {'5d later':<28} {'10d later':<28} flag"
    )
    print(hdr)
    print("-" * len(hdr))
    for i, r in enumerate(rows, 1):
        flag = "STOP+5%+" if r.get("flag_stop_bounce") else ""
        pnl = r.get("realized_pnl")
        pnl_s = "—" if pnl is None else f"{pnl:+.2f}"
        print(
            f"{i:>2} {r['symbol']:<6} {str(r['exit_reason'])[:26]:<26} "
            f"{r['sell_ct'].strftime('%m-%d %H:%M'):<16} {_fmt_px(r.get('sell_px')):>8} "
            f"{pnl_s:>8} {_fmt_hold(r.get('hold_min')):>6} "
            f"{_later_cell(r, '1d'):<28} {_later_cell(r, '5d'):<28} {_later_cell(r, '10d'):<28} {flag}"
        )
    print()
    if flagged:
        print("FLAG — stopped then +5%+ within 5 sessions (or last bar in window):")
        for line in flagged:
            print(" ", line)
    else:
        print("FLAG — none: no stopped name is +5%+ within 5 sessions (available window).")
    print("Note: 5d/10d use last available close after the sell when that many sessions do not exist yet.")


def _write_md(
    rows: list[dict[str, Any]],
    warnings: list[str],
    path: Path,
    flagged: list[str],
    out_md: Path | None = None,
) -> None:
    dest = out_md or OUT_MD
    lines = [
        "# Paper NYSE sell follow-through",
        "",
        f"Generated: {datetime.now(CT).strftime('%Y-%m-%d %H:%M CT')}",
        f"SoT: `{path}`  event=fill side=sell  sleeve=NYSE  since {SINCE.isoformat()}",
        "Measure only. No orders. No ATR / min-hold / .env changes.",
        "",
        "| # | symbol | exit reason | sell CT | sell px | realized_pnl | hold | 1d later | 5d later | 10d later | flag |",
        "|---|--------|-------------|---------|---------|--------------|------|----------|----------|-----------|------|",
    ]
    for i, r in enumerate(rows, 1):
        pnl = r.get("realized_pnl")
        pnl_s = "—" if pnl is None else f"{pnl:+.2f}"
        flag = "STOP+5%+" if r.get("flag_stop_bounce") else ""
        lines.append(
            f"| {i} | {r['symbol']} | {r['exit_reason']} | "
            f"{r['sell_ct'].strftime('%Y-%m-%d %H:%M')} | {_fmt_px(r.get('sell_px'))} | {pnl_s} | "
            f"{_fmt_hold(r.get('hold_min'))} | {_later_cell(r, '1d')} | {_later_cell(r, '5d')} | "
            f"{_later_cell(r, '10d')} | {flag} |"
        )
    lines.extend(["", "## Flags"])
    if flagged:
        for line in flagged:
            lines.append(f"- {line}")
    else:
        lines.append("- none")
    if warnings:
        lines.extend(["", "## Journal notes", *[f"- {w}" for w in warnings]])
    lines.append("")
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {dest}")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--book", choices=sorted(_BOOK_JOURNALS), default="paper_v2")
    args = ap.parse_args()
    journal = _BOOK_JOURNALS[args.book]
    out = Path(__file__).with_name(f"nyse_sell_followthrough_{args.book}_last.md")
    run(journal, out_md=out)
