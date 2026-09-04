#!/usr/bin/env python3
"""Study: overnight pre-sale list → sell at open → wait 30m before buys.

Read-only research. Does not place orders or change live defaults.

Idea under test
---------------
1) After the close / overnight: mark NYSE names that should be sold next open
   (underwater vs entry by OPEN_PCT, default -1% like old fat-loser).
2) At the open: sell those names at the session open (not mid-morning chase).
3) Keep the existing 9:30–10:00 ET buy cooldown (wait 30m before redeploy).

Compares, for completed journal round-trips that qualify as overnight candidates:
  A) Actual exit fill (what paper did)
  B) Counterfactual sell at next session open after the signal close
  C) Counterfactual sell at 10:00 ET same day (open + 30m)

Usage (from stock-bot/):
  python scripts/analysis/study_overnight_presale_open.py
  python scripts/analysis/study_overnight_presale_open.py --days 60
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")

JOURNAL_PATHS = [
    ROOT / "data/portal/users/dawimberly/books/alpaca_paper_v2/paper_journal.csv",
    ROOT / "data/portal/users/dawimberly/books/alpaca_paper/paper_journal.csv",
    ROOT / "paper_journal.csv",
    ROOT / "paper_chase_journal.csv",
]


@dataclass
class RoundTrip:
    symbol: str
    entry_ts: pd.Timestamp
    entry_px: float
    exit_ts: pd.Timestamp
    exit_px: float
    notional: float
    exit_reason: str
    src: str


def _load_sells_and_buys(paths: list[Path]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for p in paths:
        if not p.is_file():
            continue
        df = pd.read_csv(p, on_bad_lines="skip", low_memory=False)
        df["_src"] = str(p.relative_to(ROOT) if p.is_relative_to(ROOT) else p)
        frames.append(df)
    if not frames:
        raise SystemExit("No journal files found.")
    df = pd.concat(frames, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    # Journals on this box are naive local CT; tag as CT then convert to ET.
    ts = df["timestamp"]
    if getattr(ts.dt, "tz", None) is None:
        ts = ts.dt.tz_localize(CT, ambiguous="infer", nonexistent="shift_forward")
    else:
        ts = ts.dt.tz_convert(CT)
    df["ts_et"] = ts.dt.tz_convert(ET)
    df["side"] = df.get("side", "").astype(str).str.lower()
    df["event"] = df.get("event", "").astype(str).str.lower()
    df["ticker"] = (
        df.get("ticker", df.get("symbol", ""))
        .astype(str)
        .str.upper()
        .str.strip()
    )
    df["price"] = pd.to_numeric(df.get("price"), errors="coerce")
    df["notional"] = pd.to_numeric(df.get("notional"), errors="coerce")
    reason = df.get("exit_reason")
    if reason is None:
        reason = df.get("pair_key")
    df["exit_reason"] = reason.astype(str) if reason is not None else ""
    # Keep NYSE-ish fills only (skip VTI/SPY/core ETFs when obvious).
    skip = {"VTI", "SPY", "QQQ", "VOO", "IVV", "NAN", ""}
    df = df[~df["ticker"].isin(skip)]
    fills = df[df["event"].eq("fill") & df["side"].isin(["buy", "sell"])].copy()
    fills = fills.dropna(subset=["ts_et", "ticker"])
    return fills.sort_values("ts_et")


def _round_trips(fills: pd.DataFrame) -> list[RoundTrip]:
    """Match FIFO buy→sell per ticker (simple; good enough for study)."""
    trips: list[RoundTrip] = []
    open_lots: dict[str, list[tuple[pd.Timestamp, float, float, str]]] = {}
    for _, row in fills.iterrows():
        sym = row["ticker"]
        px = float(row["price"]) if pd.notna(row["price"]) and row["price"] > 0 else None
        if px is None and pd.notna(row["notional"]) and pd.notna(row.get("qty")):
            qty = float(row["qty"])
            if qty:
                px = abs(float(row["notional"]) / qty)
        if px is None or px <= 0:
            continue
        notion = float(row["notional"]) if pd.notna(row["notional"]) else 0.0
        if row["side"] == "buy":
            open_lots.setdefault(sym, []).append(
                (row["ts_et"], px, notion, row["_src"])
            )
            continue
        # sell
        lots = open_lots.get(sym) or []
        if not lots:
            continue
        entry_ts, entry_px, entry_n, src = lots.pop(0)
        trips.append(
            RoundTrip(
                symbol=sym,
                entry_ts=entry_ts,
                entry_px=entry_px,
                exit_ts=row["ts_et"],
                exit_px=px,
                notional=notion or entry_n,
                exit_reason=str(row.get("exit_reason") or ""),
                src=src,
            )
        )
    return trips


def _fetch_intraday(symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    import yfinance as yf

    # Pad a day each side for open bars.
    start_d = (start.tz_convert(ET) - timedelta(days=2)).strftime("%Y-%m-%d")
    end_d = (end.tz_convert(ET) + timedelta(days=2)).strftime("%Y-%m-%d")
    hist = yf.Ticker(symbol).history(
        start=start_d, end=end_d, interval="5m", auto_adjust=True
    )
    if hist is None or hist.empty:
        return pd.DataFrame()
    out = hist.reset_index()
    # yfinance column may be Datetime or Date
    tcol = "Datetime" if "Datetime" in out.columns else out.columns[0]
    out["ts"] = pd.to_datetime(out[tcol], utc=True).dt.tz_convert(ET)
    return out


def _session_open_px(bars: pd.DataFrame, day: datetime.date) -> float | None:
    day_bars = bars[bars["ts"].dt.date == day]
    if day_bars.empty:
        return None
    # First bar of RTH ~09:30
    rth = day_bars[
        (day_bars["ts"].dt.hour > 9)
        | ((day_bars["ts"].dt.hour == 9) & (day_bars["ts"].dt.minute >= 30))
    ]
    if rth.empty:
        return None
    return float(rth.iloc[0]["Open"])


def _px_at_or_after(bars: pd.DataFrame, when: pd.Timestamp) -> float | None:
    later = bars[bars["ts"] >= when]
    if later.empty:
        return None
    # Prefer Open of that bar; else Close
    row = later.iloc[0]
    px = row.get("Open", np.nan)
    if pd.isna(px):
        px = row.get("Close", np.nan)
    return float(px) if pd.notna(px) else None


def _prior_close_px(bars: pd.DataFrame, day: datetime.date) -> float | None:
    prior = bars[bars["ts"].dt.date < day]
    if prior.empty:
        return None
    last_day = prior["ts"].dt.date.max()
    day_bars = prior[prior["ts"].dt.date == last_day]
    return float(day_bars.iloc[-1]["Close"])


def analyze(
    trips: list[RoundTrip],
    *,
    open_pct: float,
    min_days: int | None,
) -> pd.DataFrame:
    rows = []
    cutoff = None
    if min_days:
        cutoff = datetime.now(tz=ET) - timedelta(days=min_days)

    for t in trips:
        if cutoff is not None and t.exit_ts < cutoff:
            continue
        # True overnight hold only (entry session before exit session).
        entry_day = t.entry_ts.tz_convert(ET).date()
        exit_day = t.exit_ts.tz_convert(ET).date()
        if entry_day >= exit_day:
            continue

        try:
            bars = _fetch_intraday(t.symbol, t.entry_ts, t.exit_ts)
        except Exception as exc:
            rows.append(
                {
                    "symbol": t.symbol,
                    "error": str(exc),
                    "exit_reason": t.exit_reason,
                }
            )
            continue
        if bars.empty:
            continue

        # Signal: last close before exit session (overnight mark).
        # Counterfactual sell: open of exit session (pre-sale at open).
        prior_close = _prior_close_px(bars, exit_day)
        if prior_close is None or t.entry_px <= 0:
            continue
        overnight_pnl_pct = prior_close / t.entry_px - 1.0
        is_candidate = overnight_pnl_pct <= open_pct

        open_px = _session_open_px(bars, exit_day)
        ten_et = datetime(
            exit_day.year, exit_day.month, exit_day.day, 10, 0, tzinfo=ET
        )
        ten_px = _px_at_or_after(bars, pd.Timestamp(ten_et))

        actual_pct = t.exit_px / t.entry_px - 1.0
        open_sell_pct = (open_px / t.entry_px - 1.0) if open_px else None
        ten_sell_pct = (ten_px / t.entry_px - 1.0) if ten_px else None

        # Price drift open -> 10:00 (sell timing only; buys already wait 30m)
        open_to_ten = None
        if open_px and ten_px:
            open_to_ten = ten_px / open_px - 1.0

        rows.append(
            {
                "symbol": t.symbol,
                "entry_et": t.entry_ts.isoformat(),
                "exit_et": t.exit_ts.isoformat(),
                "exit_reason": t.exit_reason[:48],
                "src": t.src,
                "held_overnight": True,
                "overnight_pnl_pct": round(overnight_pnl_pct, 4),
                "candidate": is_candidate,
                "actual_pnl_pct": round(actual_pct, 4),
                "sell_open_pnl_pct": round(open_sell_pct, 4) if open_sell_pct is not None else None,
                "sell_10et_pnl_pct": round(ten_sell_pct, 4) if ten_sell_pct is not None else None,
                "open_vs_actual_pp": (
                    round((open_sell_pct - actual_pct) * 100, 3)
                    if open_sell_pct is not None
                    else None
                ),
                "ten_vs_open_pp": (
                    round(open_to_ten * 100, 3) if open_to_ten is not None else None
                ),
                "notional": round(t.notional, 2),
            }
        )
    return pd.DataFrame(rows)


def _summarize(df: pd.DataFrame, label: str) -> None:
    print(f"\n=== {label} (n={len(df)}) ===")
    if df.empty:
        print("  (no rows)")
        return
    for col in ("actual_pnl_pct", "sell_open_pnl_pct", "sell_10et_pnl_pct"):
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        if s.empty:
            print(f"  {col}: n/a")
            continue
        print(
            f"  {col}: mean={s.mean()*100:+.2f}%  median={s.median()*100:+.2f}%  "
            f"win={(s > 0).mean()*100:.0f}%"
        )
    edge = pd.to_numeric(df["open_vs_actual_pp"], errors="coerce").dropna()
    if len(edge):
        print(
            f"  sell@open vs actual: mean={edge.mean():+.3f} pp  "
            f"median={edge.median():+.3f} pp  "
            f"open_better={(edge > 0).mean()*100:.0f}% of trips"
        )
    drift = pd.to_numeric(df["ten_vs_open_pp"], errors="coerce").dropna()
    if len(drift):
        print(
            f"  10:00 vs open (same-day price drift after open sell): "
            f"mean={drift.mean():+.3f} pp  median={drift.median():+.3f} pp"
        )
        print(
            "  (negative = price fell after open -> selling at open was better "
            "than waiting to 10:00 to sell)"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=45, help="Lookback days on exits")
    ap.add_argument(
        "--open-pct",
        type=float,
        default=-0.01,
        help="Overnight candidate threshold vs entry (default -1%%)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=ROOT / "reports" / "overnight_presale_open_study.csv",
    )
    args = ap.parse_args()

    fills = _load_sells_and_buys(JOURNAL_PATHS)
    trips = _round_trips(fills)
    print(f"Loaded {len(fills)} fills -> {len(trips)} round-trips")

    df = analyze(trips, open_pct=args.open_pct, min_days=args.days)
    if df.empty:
        print("No analyzable trips (need yfinance 5m bars).")
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.out, index=False)
    print(f"Wrote {args.out}")

    overnight = df[df.get("held_overnight", True) == True]  # noqa: E712
    cand = overnight[overnight["candidate"] == True]  # noqa: E712
    other = overnight[overnight["candidate"] != True]
    _summarize(overnight, "ALL overnight holds (sell@open vs actual)")
    _summarize(
        cand,
        f"PRE-SALE CANDIDATES (prior close <= {args.open_pct:.0%} vs entry)",
    )
    _summarize(other, "Overnight holds that were NOT pre-sale candidates")

    print("\n=== Verdict guide ===")
    print(
        "If candidates' sell@open mean pnl% >> actual: open dump beats mid-session trims.\n"
        "If 10:00 vs open is mostly negative: after you sell at open, waiting 30m to "
        "BUY (existing cooldown) is fine; don't wait 30m to SELL.\n"
        "Buy cooldown 9:30-10:00 ET is already live; this study only tests sell timing."
    )

    if not cand.empty:
        print("\n=== Candidate sample (worst overnight first) ===")
        cols = [
            "symbol",
            "exit_et",
            "exit_reason",
            "overnight_pnl_pct",
            "actual_pnl_pct",
            "sell_open_pnl_pct",
            "sell_10et_pnl_pct",
            "open_vs_actual_pp",
            "ten_vs_open_pp",
        ]
        show = cand.sort_values("overnight_pnl_pct").head(15)
        print(show[cols].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
