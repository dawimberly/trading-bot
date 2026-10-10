"""Classify NYSE smart ATR stops as overnight gap-through vs session fade.

Uses alpaca_paper_v2 exit rows, daily OHLC, and the 5-minute print at the sell.
Bot ATR is close-to-close |dClose| (see calculate_atr), not true range.
Does not write .env and does not change the running bot.

Usage (from stock-bot/):
  python scripts/analysis/eval_open_stop_gap.py
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "analysis"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

from friday_run_monday_open import _alpaca_ohlc, _yf_ohlc  # noqa: E402
from modules.cost_basis import sleeve_for_symbol  # noqa: E402

JOURNAL = (
    ROOT
    / "data"
    / "portal"
    / "users"
    / "dawimberly"
    / "books"
    / "alpaca_paper_v2"
    / "paper_journal.csv"
)
OUT_JSON = Path(__file__).with_name("eval_open_stop_gap_last.json")
OUT_MD = Path(__file__).with_name("eval_open_stop_gap_last.md")
ET = ZoneInfo("America/New_York")
ATR_N = 14
ATR_MULT = 2.0
NOTE_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*%")
DISCLAIMER = (
    "Paper journal exits vs the session open and the 5-minute print at the sell. "
    "Not a promote. Do not write .env from this file."
)


def _num(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def _sleeve(row) -> str:
    raw = str(row.get("sleeve") or "").strip()
    if raw and raw.lower() not in ("nan", "none"):
        return raw.lower()
    return str(sleeve_for_symbol(str(row.get("symbol") or "")) or "").lower()


def _note_pct(notes: str) -> float | None:
    match = NOTE_RE.search(str(notes or ""))
    if not match:
        return None
    return float(match.group(1)) / 100.0


def _sale_et(ts) -> datetime | None:
    stamp = pd.Timestamp(ts)
    if pd.isna(stamp):
        return None
    if stamp.tzinfo is None:
        local = datetime.now().astimezone().tzinfo
        stamp = stamp.tz_localize(local, ambiguous=True, nonexistent="shift_forward")
    return stamp.tz_convert(ET).to_pydatetime()


def _load_journal() -> pd.DataFrame:
    df = pd.read_csv(JOURNAL, low_memory=False)
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    return df.dropna(subset=["timestamp"]).sort_values("timestamp")


def _fifo_entry(fills: pd.DataFrame, symbol: str, when) -> float | None:
    lots: deque[dict] = deque()
    cut = pd.Timestamp(when)
    for _, row in fills.iterrows():
        if row["timestamp"] > cut:
            break
        if str(row.get("symbol") or "").upper() != symbol:
            continue
        side = str(row.get("side") or "").lower()
        qty = _num(row.get("qty")) or 0.0
        px = _num(row.get("price"))
        if qty <= 0 or px is None or px <= 0:
            continue
        if side == "buy":
            lots.append({"qty": qty, "px": px})
        elif side == "sell":
            remain = qty
            while remain > 1e-9 and lots:
                lot = lots[0]
                take = min(remain, lot["qty"])
                lot["qty"] -= take
                remain -= take
                if lot["qty"] <= 1e-6:
                    lots.popleft()
    if not lots:
        return None
    notional = sum(lot["qty"] * lot["px"] for lot in lots)
    qty = sum(lot["qty"] for lot in lots)
    if qty <= 0:
        return None
    return notional / qty


def _cc_atr(ohlc: pd.DataFrame, day) -> float | None:
    """Same proxy as modules.risk_management.calculate_atr: mean |dClose|."""
    if ohlc is None or ohlc.empty:
        return None
    hist = ohlc.loc[ohlc.index < pd.Timestamp(day).normalize(), "close"].dropna()
    if len(hist) < ATR_N + 1:
        return None
    atr = float(hist.diff().abs().rolling(ATR_N, min_periods=ATR_N).mean().iloc[-1])
    return atr if atr > 0 else None


def _bar(ohlc: pd.DataFrame, day):
    if ohlc is None or ohlc.empty:
        return None
    key = pd.Timestamp(day).normalize()
    if key not in ohlc.index:
        return None
    return ohlc.loc[key]


def _prior_close(ohlc: pd.DataFrame, day) -> float | None:
    if ohlc is None or ohlc.empty:
        return None
    hist = ohlc.loc[ohlc.index < pd.Timestamp(day).normalize()]
    if hist.empty:
        return None
    return float(hist["close"].iloc[-1])


def _classify(prior: float | None, open_px: float | None, low: float | None, stop: float | None) -> str:
    if stop is None or open_px is None or prior is None:
        return "unknown"
    if prior <= stop and open_px <= stop:
        return "already_through"
    if prior > stop and open_px <= stop:
        return "gap_through"
    if open_px > stop and low is not None and low <= stop:
        return "fade"
    if open_px > stop:
        return "open_alive"
    return "unknown"


def _gap_vs_note(gap: float | None, note: float | None) -> str:
    if gap is None or note is None:
        return "unknown"
    if gap <= note:
        return "gap_covers_loss"
    if gap < 0:
        return "gap_then_fade"
    return "up_open_fade"


def _ohlc_map(symbols: list[str]) -> dict[str, pd.DataFrame]:
    start = datetime(2026, 8, 1)
    found = _alpaca_ohlc(symbols, start)
    missing = [s for s in symbols if s not in found]
    if missing:
        print(f"Alpaca daily miss {missing}; trying yfinance", flush=True)
        found.update(_yf_ohlc(missing, 1))
    return found


def _normalize_intraday(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    if "close" not in out.columns:
        return pd.DataFrame()
    idx = pd.to_datetime(out.index, utc=True, errors="coerce")
    out.index = idx.tz_convert(ET)
    out = out.loc[out.index.notna()].sort_index()
    for col in ("open", "high", "low", "close"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.dropna(subset=["close"])


def _alpaca_5m(symbols: list[str], start: datetime) -> dict[str, pd.DataFrame]:
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    import config

    key, secret = config.get_alpaca_credentials(paper=True)
    client = StockHistoricalDataClient(api_key=key, secret_key=secret)
    end = datetime.now(timezone.utc) + timedelta(days=1)
    tf = TimeFrame(5, TimeFrameUnit.Minute)
    found: dict[str, pd.DataFrame] = {}
    chunk = 20
    for i in range(0, len(symbols), chunk):
        batch = symbols[i : i + chunk]
        df = pd.DataFrame()
        for feed in (DataFeed.IEX, DataFeed.SIP, None):
            kwargs = dict(
                symbol_or_symbols=batch,
                timeframe=tf,
                start=start,
                end=end,
            )
            if feed is not None:
                kwargs["feed"] = feed
            try:
                bars = client.get_stock_bars(StockBarsRequest(**kwargs))
                df = getattr(bars, "df", None)
                if df is not None and not df.empty:
                    break
            except Exception:
                df = pd.DataFrame()
        if df is None or df.empty:
            continue
        if isinstance(df.index, pd.MultiIndex):
            groups = df.groupby(level=0)
        else:
            groups = [(batch[0], df)] if len(batch) == 1 else []
        for sym, g in groups:
            piece = g.droplevel(0) if isinstance(g.index, pd.MultiIndex) else g
            framed = _normalize_intraday(piece)
            if not framed.empty:
                found[str(sym).upper()] = framed
    return found


def _sale_print(intraday: pd.DataFrame | None, sale: datetime) -> float | None:
    if intraday is None or intraday.empty or sale is None:
        return None
    ts = pd.Timestamp(sale)
    if ts.tzinfo is None:
        ts = ts.tz_localize(ET)
    else:
        ts = ts.tz_convert(ET)
    day = intraday.loc[intraday.index.date == ts.date()]
    if day.empty:
        return None
    prior = day.loc[day.index <= ts]
    bar = prior.iloc[-1] if not prior.empty else day.iloc[0]
    px = _num(bar.get("close"))
    return px if px and px > 0 else None


def _paper_positions() -> dict[str, dict]:
    try:
        from alpaca.trading.client import TradingClient

        import config

        key, secret = config.get_alpaca_credentials(paper=True)
        client = TradingClient(key, secret, paper=True)
        out = {}
        for pos in client.get_all_positions():
            qty = _num(pos.qty) or 0.0
            if qty == 0:
                continue
            out[str(pos.symbol).upper()] = {
                "qty": qty,
                "avg_entry": _num(pos.avg_entry_price),
                "unrealized_pct": _num(getattr(pos, "unrealized_plpc", None)),
            }
        return out
    except Exception as exc:
        print(f"Paper positions unavailable: {exc}", flush=True)
        return {}


def _rows_for(journal: pd.DataFrame, start, end=None) -> pd.DataFrame:
    ev = journal["event"].astype(str).str.lower()
    sub = journal[ev.eq("exit")].copy()
    sub = sub[sub["timestamp"] >= start]
    if end is not None:
        sub = sub[sub["timestamp"] < end]
    keep = []
    for _, row in sub.iterrows():
        if _sleeve(row) not in ("nyse", ""):
            continue
        reason = str(row.get("exit_reason") or row.get("notes") or "").lower()
        if "smart_atr_stop" not in reason and "atr_stop" not in reason:
            continue
        keep.append(row)
    return pd.DataFrame(keep)


def _analyze(
    exits: pd.DataFrame,
    fills: pd.DataFrame,
    ohlc_map: dict[str, pd.DataFrame],
    intra_map: dict[str, pd.DataFrame],
    held: dict[str, dict],
) -> list[dict]:
    rows = []
    for _, raw in exits.iterrows():
        sym = str(raw.get("symbol") or "").upper()
        sale = _sale_et(raw["timestamp"])
        if not sym or sale is None:
            continue
        day = sale.date()
        ohlc = ohlc_map.get(sym)
        bar = _bar(ohlc, day)
        prior = _prior_close(ohlc, day)
        open_px = float(bar["open"]) if bar is not None else None
        low = float(bar["low"]) if bar is not None else None
        note = _note_pct(str(raw.get("notes") or ""))
        lot_entry = _fifo_entry(fills, sym, raw["timestamp"])
        print_px = _sale_print(intra_map.get(sym), sale)
        bot_entry = None
        if print_px is not None and note is not None and note > -0.99:
            bot_entry = print_px / (1.0 + note)
        atr = _cc_atr(ohlc, day)
        stop = print_px
        stop_src = "sale_print"
        if stop is None and bot_entry is not None and atr is not None:
            stop = round(max(0.01, bot_entry - ATR_MULT * atr), 4)
            stop_src = "2x_cc_atr"
        if stop is None and lot_entry is not None and atr is not None:
            stop = round(max(0.01, lot_entry - ATR_MULT * atr), 4)
            stop_src = "2x_cc_atr_lot"
        if stop is None and bot_entry is not None and note is not None:
            stop = round(bot_entry * (1.0 + note), 4)
            stop_src = "implied_note"
        label = _classify(prior, open_px, low, stop)
        gap = (open_px / prior - 1.0) if prior and open_px else None
        pos = held.get(sym) or {}
        rows.append(
            {
                "symbol": sym,
                "sold_et": sale.strftime("%Y-%m-%d %H:%M"),
                "hour_et": sale.hour,
                "note_pct": None if note is None else round(note * 100.0, 2),
                "lot_entry": None if lot_entry is None else round(lot_entry, 4),
                "bot_entry": None if bot_entry is None else round(bot_entry, 4),
                "atr": None if atr is None else round(atr, 4),
                "stop": None if stop is None else round(stop, 4),
                "stop_src": stop_src if stop is not None else "",
                "sale_print": None if print_px is None else round(print_px, 4),
                "prior_close": None if prior is None else round(prior, 4),
                "open": None if open_px is None else round(open_px, 4),
                "low": None if low is None else round(low, 4),
                "gap_pct": None if gap is None else round(gap * 100.0, 2),
                "gap_vs_note": _gap_vs_note(gap, note),
                "label": label,
                "still_held_qty": pos.get("qty"),
                "alpaca_avg": pos.get("avg_entry"),
                "first_hour": 9 <= sale.hour < 11,
            }
        )
    return rows


def _counts(rows: list[dict], key: str = "label") -> Counter:
    return Counter(r[key] for r in rows)


def _verdict(week: list[dict]) -> str:
    n = len(week)
    labels = _counts(week)
    gaps = labels.get("gap_through", 0)
    already = labels.get("already_through", 0)
    fades = labels.get("fade", 0)
    open_dead = gaps + already
    first = [r for r in week if r["first_hour"]]
    first_open = sum(1 for r in first if r["label"] in ("gap_through", "already_through"))
    still = [r["symbol"] for r in week if r.get("still_held_qty")]
    held_note = ""
    if still:
        held_note = (
            f" Journal logged an exit and Alpaca still holds {', '.join(still)}. "
            "Those rows are not closed lots."
        )
    if n == 0:
        return "No NYSE ATR exits in the week. Leave the book alone. " + DISCLAIMER
    if open_dead >= max(3, (n + 1) // 2) and first_open >= 3:
        return (
            f"{open_dead} of {n} were already through the stop at the open "
            f"({gaps} gap-through, {already} already dead into the overnight). "
            f"{first_open} of {len(first)} first-two-hour sales were open prints. "
            "That is an open-print cluster, not a midday fade. "
            "Do not change the cap, stall rule, or ranker. "
            "Do not add an open delay from one week of names. "
            + held_note
            + " "
            + DISCLAIMER
        )
    if fades + labels.get("open_alive", 0) >= max(3, (n + 1) // 2) and open_dead < 3:
        return (
            f"{fades} faded after an alive open; {gaps} gapped through; "
            f"{already} were already dead. "
            "This week's stops are session fades (and a few noisy exit rows), "
            "not overnight gaps through a daily ATR stop. Leave the book alone."
            + held_note
            + " "
            + DISCLAIMER
        )
    return (
        f"Mixed: gap_through={gaps} already_through={already} fade={fades} "
        f"open_alive={labels.get('open_alive', 0)} unknown={labels.get('unknown', 0)}."
        + held_note
        + " Leave the book alone. "
        + DISCLAIMER
    )


def _fmt(value, digits=2, pct=False, signed=False) -> str:
    if value is None:
        return "n/a"
    if pct:
        return f"{value:+.{digits}f}%" if signed else f"{value:.{digits}f}%"
    return f"{value:.{digits}f}"


def _table(rows: list[dict]) -> list[str]:
    lines = [
        "| Sold ET | Sym | Note | Bot entry | Stop | Prior | Open | Print | Gap | Class | vs note | Held |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|---:|",
    ]
    for r in rows:
        lines.append(
            "| {sold} | {sym} | {note} | {entry} | {stop} | {prior} | {op} | {prn} | {gap} | {lab} | {gv} | {held} |".format(
                sold=r["sold_et"],
                sym=r["symbol"],
                note=_fmt(r["note_pct"], pct=True),
                entry=_fmt(r["bot_entry"]),
                stop=_fmt(r["stop"]),
                prior=_fmt(r["prior_close"]),
                op=_fmt(r["open"]),
                prn=_fmt(r["sale_print"]),
                gap=_fmt(r["gap_pct"], pct=True, signed=True),
                lab=r["label"],
                gv=r["gap_vs_note"],
                held=_fmt(r["still_held_qty"]),
            )
        )
    return lines


def main() -> int:
    journal = _load_journal()
    now = datetime.now()
    week_start = now - timedelta(days=7)
    ev = journal["event"].astype(str).str.lower()
    fills = journal[ev.eq("fill")].copy()
    week = _rows_for(journal, week_start)
    extra_start = now - timedelta(days=14)
    extra = _rows_for(journal, extra_start, week_start)
    symbols = sorted(
        {
            str(r.get("symbol") or "").upper()
            for _, r in pd.concat([week, extra], ignore_index=True).iterrows()
            if str(r.get("symbol") or "")
        }
    )
    print(f"Exits this week {len(week)}; earlier 14d {len(extra)}; names {len(symbols)}", flush=True)
    ohlc_map = _ohlc_map(symbols)
    print(f"Daily OHLC {len(ohlc_map)}/{len(symbols)}", flush=True)
    intra_map = _alpaca_5m(symbols, datetime(2026, 9, 12, tzinfo=timezone.utc))
    print(f"5m bars {len(intra_map)}/{len(symbols)}", flush=True)
    held = _paper_positions()
    print(f"Paper names now {sorted(held)}", flush=True)
    week_rows = _analyze(week, fills, ohlc_map, intra_map, held)
    extra_rows = _analyze(extra, fills, ohlc_map, intra_map, held)
    verdict = _verdict(week_rows)
    week_counts = dict(_counts(week_rows))
    extra_counts = dict(_counts(extra_rows))
    gap_counts = dict(_counts(week_rows, "gap_vs_note"))
    payload = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "week_start": week_start.isoformat(timespec="seconds"),
        "week": week_rows,
        "week_counts": week_counts,
        "week_gap_vs_note": gap_counts,
        "prior_14d": extra_rows,
        "prior_counts": extra_counts,
        "held_now": held,
        "verdict": verdict,
    }
    held_line = ", ".join(
        f"{s} x{held[s]['qty']:.2f} @{held[s]['avg_entry']}" for s in sorted(held)
    )
    lines = [
        "# Open-print vs fade - NYSE smart ATR stops",
        "",
        DISCLAIMER,
        "",
        f"Generated {payload['generated']}.",
        f"Week window from {week_start:%Y-%m-%d} (the nine Saturday-review exits).",
        "Stop is the 5-minute close at the journal sell. Bot entry is that print divided by (1 + note percent).",
        "The bot ATR used for a sanity check is 14d mean |dClose|, same as calculate_atr.",
        "",
        f"Alpaca paper still open: {held_line or 'unavailable'}",
        "",
        "## This week",
        "",
        f"Open vs stop: {week_counts}",
        f"Overnight gap vs logged loss: {gap_counts}",
        "",
        *(_table(week_rows) if week_rows else ["_No rows._"]),
        "",
        verdict,
        "",
        "## Prior 7-14d (same rule, not in the Saturday table)",
        "",
        f"Counts: {extra_counts}",
        "",
        *(_table(extra_rows) if extra_rows else ["_No rows._"]),
        "",
    ]
    text = "\n".join(lines) + "\n"
    OUT_MD.write_text(text, encoding="utf-8")
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(text)
    print(f"Wrote {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
