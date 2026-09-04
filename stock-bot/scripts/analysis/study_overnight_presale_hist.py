#!/usr/bin/env python3
"""Historical overnight pre-sale study: sell@open vs sell@10:00 ET.

Builds a large sample from yfinance 5m bars (last ~55 trading days):

1) Momentum-like entry at day close when close > SMA50 and 20d return > 0
2) Hold up to --max-hold trading days
3) If close vs entry <= --open-pct (default -1%), mark overnight pre-sale
4) Next session: compare sell at RTH open vs sell at 10:00 ET
5) Also report hold-to-close of that sell day (context)

Usage (from stock-bot/):
  python scripts/analysis/study_overnight_presale_hist.py
  python scripts/analysis/study_overnight_presale_hist.py --max-tickers 80 --open-pct -0.01
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402

ET = ZoneInfo("America/New_York")

SKIP = frozenset(
    {
        "VTI",
        "SPY",
        "QQQ",
        "IWM",
        "VOO",
        "IVV",
        "VXUS",
        "VEA",
        "VWO",
        "VNQ",
        "VGT",
        "VIG",
        "VYM",
        "VO",
        "VB",
        "VUG",
        "VTV",
        "VT",
        "BND",
        "BNDX",
        "GLD",
        "SLV",
        "CPER",
        "URA",
        "PPLT",
        "DBB",
        "GDX",
        "USO",
        "GOVT",
        "VIX",
        "JETS",
        "FAS",
        "EWT",
        "CGXU",
        "CGGO",
        "CWB",
        "EMXC",
        "NAN",
        "",
    }
)


def _ticker_list(max_tickers: int) -> list[str]:
    # Prefer static equity UNIVERSE + recent paper names, then DB fill.
    paper_syms: list[str] = []
    jp = ROOT / "data/portal/users/dawimberly/books/alpaca_paper_v2/paper_journal.csv"
    if jp.is_file():
        try:
            j = pd.read_csv(jp, usecols=lambda c: c.lower() in ("ticker", "symbol"), nrows=5000)
            col = "ticker" if "ticker" in j.columns else "symbol"
            paper_syms = (
                j[col].astype(str).str.upper().str.strip().dropna().unique().tolist()
            )
        except Exception:
            paper_syms = []

    base: list[str] = []
    for s in list(config.UNIVERSE) + paper_syms:
        s = str(s).upper().strip()
        if not s or s in SKIP or "-USD" in s:
            continue
        if not config._nyse_eligible_symbol(s):
            continue
        if s not in base:
            base.append(s)

    # Fill from DB daily tables if short
    if len(base) < max_tickers:
        import sqlite3

        conn = sqlite3.connect(str(config.resolve_db_path()))
        try:
            tabs = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
        finally:
            conn.close()
        db_syms = [t[:-6] for t in tabs if t.endswith("_daily")]
        for s in config.nyse_momentum_universe(db_syms):
            s = str(s).upper().strip()
            if not s or s in SKIP or s in base or "-USD" in s:
                continue
            if not config._nyse_eligible_symbol(s):
                continue
            base.append(s)
            if len(base) >= max_tickers:
                break

    return base[:max_tickers]


def _yf_download_5m(tickers: list[str], *, batch: int = 15) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    out: dict[str, pd.DataFrame] = {}
    # yfinance 5m history ~60 calendar days
    period = "59d"
    for i in range(0, len(tickers), batch):
        chunk = tickers[i : i + batch]
        yf_syms = [config.yf_symbol(t) for t in chunk]
        rev = {config.yf_symbol(t): t for t in chunk}
        try:
            raw = yf.download(
                yf_syms,
                period=period,
                interval="5m",
                group_by="ticker",
                auto_adjust=True,
                threads=True,
                progress=False,
            )
        except Exception as exc:
            print(f"  batch download failed ({chunk[0]}...): {exc}")
            time.sleep(1.0)
            continue
        if raw is None or raw.empty:
            continue
        # Single ticker -> flat columns; multi -> MultiIndex
        if len(chunk) == 1:
            sym = chunk[0]
            df = raw.reset_index()
            tcol = "Datetime" if "Datetime" in df.columns else df.columns[0]
            df["ts"] = pd.to_datetime(df[tcol], utc=True).dt.tz_convert(ET)
            out[sym] = df
        else:
            # MultiIndex columns: (ticker, OHLCV)
            level0 = raw.columns.get_level_values(0).unique()
            for yf_sym in level0:
                try:
                    part = raw[yf_sym].dropna(how="all")
                except Exception:
                    continue
                if part.empty:
                    continue
                sym = rev.get(str(yf_sym), str(yf_sym).replace("-", "."))
                df = part.reset_index()
                tcol = "Datetime" if "Datetime" in df.columns else df.columns[0]
                df["ts"] = pd.to_datetime(df[tcol], utc=True).dt.tz_convert(ET)
                out[sym] = df
        print(f"  downloaded 5m batch {i // batch + 1}: {len(chunk)} tickers")
        time.sleep(0.4)
    return out


def _session_frames(bars: pd.DataFrame) -> pd.DataFrame:
    """One row per session: open, ten_et, close."""
    if bars.empty:
        return pd.DataFrame()
    df = bars.copy()
    df["day"] = df["ts"].dt.date
    # RTH only 09:30-16:00 ET
    mins = df["ts"].dt.hour * 60 + df["ts"].dt.minute
    rth = df[(mins >= 9 * 60 + 30) & (mins < 16 * 60)].copy()
    if rth.empty:
        return pd.DataFrame()

    rows = []
    for day, g in rth.groupby("day", sort=True):
        g = g.sort_values("ts")
        open_px = float(g.iloc[0]["Open"])
        close_px = float(g.iloc[-1]["Close"])
        ten = g[
            (g["ts"].dt.hour > 10)
            | ((g["ts"].dt.hour == 10) & (g["ts"].dt.minute >= 0))
        ]
        ten_px = float(ten.iloc[0]["Open"]) if not ten.empty else None
        # Prefer bar at/after 10:00; if first RTH bar is already after 10 (holiday), skip
        if ten_px is None:
            continue
        rows.append(
            {
                "day": day,
                "open": open_px,
                "ten": ten_px,
                "close": close_px,
            }
        )
    return pd.DataFrame(rows)


def _events_for_symbol(
    sym: str,
    session: pd.DataFrame,
    *,
    open_pct: float,
    max_hold: int,
    require_momentum: bool,
) -> list[dict]:
    if session is None or len(session) < 55:
        return []
    s = session.reset_index(drop=True)
    closes = s["close"].astype(float)
    sma50 = closes.rolling(50, min_periods=50).mean()
    ret20 = closes / closes.shift(20) - 1.0

    events: list[dict] = []
    i = 50
    while i < len(s) - 2:
        if require_momentum:
            if not (closes.iloc[i] > sma50.iloc[i] and float(ret20.iloc[i] or 0) > 0):
                i += 1
                continue
        entry = float(closes.iloc[i])
        if entry <= 0:
            i += 1
            continue
        fired = False
        for j in range(i + 1, min(i + max_hold, len(s) - 1)):
            mark = float(closes.iloc[j]) / entry - 1.0
            if mark > open_pct:
                continue
            # Pre-sale after close j -> sell session j+1
            sell = s.iloc[j + 1]
            open_px = float(sell["open"])
            ten_px = float(sell["ten"])
            close_px = float(sell["close"])
            sell_open = open_px / entry - 1.0
            sell_ten = ten_px / entry - 1.0
            sell_close = close_px / entry - 1.0
            open_to_ten = ten_px / open_px - 1.0 if open_px else None
            events.append(
                {
                    "symbol": sym,
                    "entry_day": s.iloc[i]["day"],
                    "signal_day": s.iloc[j]["day"],
                    "sell_day": sell["day"],
                    "hold_days": j - i,
                    "overnight_pnl_pct": round(mark, 4),
                    "sell_open_pnl_pct": round(sell_open, 4),
                    "sell_10et_pnl_pct": round(sell_ten, 4),
                    "sell_close_pnl_pct": round(sell_close, 4),
                    "open_vs_ten_pp": round((sell_open - sell_ten) * 100, 3),
                    "ten_vs_open_pp": round(open_to_ten * 100, 3) if open_to_ten is not None else None,
                    "open_vs_close_pp": round((sell_open - sell_close) * 100, 3),
                    "momentum_entry": require_momentum,
                }
            )
            fired = True
            # Skip ahead past this entry's sell day to avoid overlapping events
            i = j + 2
            break
        if not fired:
            i += 1
    return events


def _summarize(df: pd.DataFrame, label: str) -> None:
    print(f"\n=== {label} (n={len(df)}) ===")
    if df.empty:
        print("  (no rows)")
        return
    for col in ("overnight_pnl_pct", "sell_open_pnl_pct", "sell_10et_pnl_pct", "sell_close_pnl_pct"):
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        if s.empty:
            continue
        print(
            f"  {col}: mean={s.mean()*100:+.2f}%  median={s.median()*100:+.2f}%  "
            f"win={(s > 0).mean()*100:.0f}%"
        )
    edge = pd.to_numeric(df["open_vs_ten_pp"], errors="coerce").dropna()
    if len(edge):
        print(
            f"  sell@open vs sell@10:00: mean={edge.mean():+.3f} pp  "
            f"median={edge.median():+.3f} pp  "
            f"open_better={(edge > 0).mean()*100:.0f}% of events"
        )
    drift = pd.to_numeric(df["ten_vs_open_pp"], errors="coerce").dropna()
    if len(drift):
        print(
            f"  price drift open->10:00: mean={drift.mean():+.3f} pp  "
            f"median={drift.median():+.3f} pp"
        )
        print(
            "  (negative drift => selling at open beat waiting 30m to sell)"
        )
    vs_close = pd.to_numeric(df["open_vs_close_pp"], errors="coerce").dropna()
    if len(vs_close):
        print(
            f"  sell@open vs hold-to-close same day: mean={vs_close.mean():+.3f} pp  "
            f"open_better={(vs_close > 0).mean()*100:.0f}%"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-tickers", type=int, default=80)
    ap.add_argument("--open-pct", type=float, default=-0.01)
    ap.add_argument("--max-hold", type=int, default=8, help="Max hold days after entry")
    ap.add_argument(
        "--broad",
        action="store_true",
        help="Also run without momentum filter (any entry day close)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=ROOT / "reports" / "overnight_presale_hist_study.csv",
    )
    args = ap.parse_args()

    tickers = _ticker_list(args.max_tickers)
    print(f"Universe: {len(tickers)} tickers (5m ~59d)")
    print("Downloading 5m bars...")
    bars_map = _yf_download_5m(tickers)
    print(f"Got 5m data for {len(bars_map)} / {len(tickers)} tickers")

    sessions: dict[str, pd.DataFrame] = {}
    for sym, bars in bars_map.items():
        sess = _session_frames(bars)
        if len(sess) >= 55:
            sessions[sym] = sess
    print(f"Usable session frames: {len(sessions)}")

    rows: list[dict] = []
    for sym, sess in sessions.items():
        rows.extend(
            _events_for_symbol(
                sym,
                sess,
                open_pct=args.open_pct,
                max_hold=args.max_hold,
                require_momentum=True,
            )
        )
    mom = pd.DataFrame(rows)
    print(f"Momentum-entry pre-sale events: {len(mom)}")

    broad = pd.DataFrame()
    if args.broad:
        brows: list[dict] = []
        for sym, sess in sessions.items():
            brows.extend(
                _events_for_symbol(
                    sym,
                    sess,
                    open_pct=args.open_pct,
                    max_hold=args.max_hold,
                    require_momentum=False,
                )
            )
        broad = pd.DataFrame(brows)
        print(f"Broad (any-entry) pre-sale events: {len(broad)}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    out_df = mom if not mom.empty else broad
    if out_df.empty:
        print("No events found.")
        return 1
    # Prefer writing momentum; append broad with tag if both
    if not mom.empty and not broad.empty:
        mom = mom.copy()
        mom["cohort"] = "momentum"
        broad = broad.copy()
        broad["cohort"] = "broad"
        out_df = pd.concat([mom, broad], ignore_index=True)
    elif not mom.empty:
        mom = mom.copy()
        mom["cohort"] = "momentum"
        out_df = mom
    else:
        broad = broad.copy()
        broad["cohort"] = "broad"
        out_df = broad

    out_df.to_csv(args.out, index=False)
    print(f"Wrote {args.out}")

    _summarize(
        out_df[out_df["cohort"] == "momentum"] if "cohort" in out_df else out_df,
        f"MOMENTUM entries, pre-sale at close <= {args.open_pct:.0%} vs entry",
    )
    if args.broad and not broad.empty:
        _summarize(
            out_df[out_df["cohort"] == "broad"],
            f"BROAD any-entry, pre-sale at close <= {args.open_pct:.0%} vs entry",
        )

    print("\n=== Verdict ===")
    print(
        "If open_better is high and open->10:00 drift is negative:\n"
        "  dump overnight losers at the open; keep 9:30-10:00 buy cooldown.\n"
        "If open_better is low / drift positive:\n"
        "  waiting past the open (or not pre-selling) may be better."
    )

    show = out_df
    if "cohort" in out_df.columns and (out_df["cohort"] == "momentum").any():
        show = out_df[out_df["cohort"] == "momentum"]
    print("\n=== Worst overnight marks (sample) ===")
    cols = [
        "symbol",
        "entry_day",
        "sell_day",
        "hold_days",
        "overnight_pnl_pct",
        "sell_open_pnl_pct",
        "sell_10et_pnl_pct",
        "open_vs_ten_pp",
        "ten_vs_open_pp",
    ]
    print(show.sort_values("overnight_pnl_pct").head(20)[cols].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
