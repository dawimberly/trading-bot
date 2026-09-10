#!/usr/bin/env python3
"""Backtest Felix title/transcript directional calls vs GLD/SLV/SPY/UUP."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "sentiment/sources/youtube/felix_and_friends/manifest.jsonl"
OUT_CSV = ROOT / "data/_felix_prediction_backtest.csv"

GOLD_BEAR = re.compile(
    r"never buying.?gold|crushed gold|gold.?s bloodbath|sold gold|gold bloodbath",
    re.I,
)
STOCK_BEAR = re.compile(
    r"stock market crash|stocks? (crash|fall|falling)|exact date.*crash|"
    r"unthinkable.*stocks|dire warning|sold everything|before 2008|ai bubble|"
    r"refuses to crash",
    re.I,
)
STOCK_BULL = re.compile(
    r"wish you bought|get in now|10.?bagger|stocks worth buying|buy these|"
    r"wealth opportunity|tech stocks.*dip|opened up a \$",
    re.I,
)
DOLLAR_BEAR = re.compile(
    r"petrodollar|dollar (collapse|reset|crash)|happen to the dollar|"
    r"currency reset|global monetary reset",
    re.I,
)


def load_rows() -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        vid = r.get("video_id")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        rows.append(r)
    return rows


def classify(r: dict) -> list[tuple[str, int, str]]:
    title = r.get("title") or ""
    calls: list[tuple[str, int, str]] = []

    if GOLD_BEAR.search(title):
        calls.append(("GLD", -1, "gold_bear"))
    elif re.search(r"\bgold\b", title, re.I):
        calls.append(("GLD", 1, "gold_bull"))

    if re.search(r"\bsilver\b", title, re.I) and not GOLD_BEAR.search(title):
        calls.append(("SLV", 1, "silver_bull"))

    if DOLLAR_BEAR.search(title):
        calls.append(("UUP", -1, "dollar_bear"))

    if STOCK_BEAR.search(title):
        calls.append(("SPY", -1, "stock_bear"))
    elif STOCK_BULL.search(title):
        calls.append(("SPY", 1, "stock_bull"))

    if not calls and (r.get("sentiment") or 0) <= -0.7:
        calls.append(("SPY", -1, "sentiment_bear"))
    return calls


def fwd_ret(series: pd.Series, dt, days: int) -> float | None:
    series = series.dropna()
    ts = pd.Timestamp(dt)
    loc = series.index.searchsorted(ts)
    if loc >= len(series.index):
        return None
    dt0 = series.index[loc]
    future = series.index[series.index > dt0]
    cand = future[future >= (dt0 + pd.Timedelta(days=days))]
    if len(cand) == 0:
        return None
    dt1 = cand[0]
    p0 = float(series.loc[dt0])
    p1 = float(series.loc[dt1])
    if p0 <= 0:
        return None
    return (p1 / p0 - 1.0) * 100.0


def main() -> None:
    rows = load_rows()
    tickers = ["GLD", "SLV", "SPY", "UUP"]
    start = "2026-04-01"
    end = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
    print(f"Downloading {tickers} {start} -> {end}")
    data = yf.download(tickers, start=start, end=end, progress=False, auto_adjust=True)
    px = data["Close"] if isinstance(data.columns, pd.MultiIndex) else data
    px = px.dropna(how="all")
    print(f"price rows={len(px)}")

    horizons = [5, 20, 60]
    results: list[dict] = []
    for r in rows:
        pub = r.get("published")
        if not pub or len(str(pub)) != 8:
            continue
        dt = datetime.strptime(str(pub), "%Y%m%d").date()
        for asset, direction, label in classify(r):
            if asset not in px.columns:
                continue
            out = {
                "published": pub,
                "video_id": r.get("video_id"),
                "title": (r.get("title") or "")[:80],
                "label": label,
                "asset": asset,
                "dir": direction,
                "sentiment": r.get("sentiment"),
            }
            for h in horizons:
                ret = fwd_ret(px[asset], dt, h)
                out[f"ret_{h}d"] = None if ret is None else round(ret, 2)
                if ret is not None:
                    signed = direction * ret
                    out[f"pnl_{h}d"] = round(signed, 2)
                    out[f"hit_{h}d"] = bool(signed > 0)
            results.append(out)

    df = pd.DataFrame(results)
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_CSV, index=False)
    print(f"wrote {len(df)} call rows -> {OUT_CSV}")

    print("\n=== HIT RATES (direction * return > 0) ===")
    for h in horizons:
        col = f"hit_{h}d"
        sub = df.dropna(subset=[col])
        if sub.empty:
            continue
        print(
            f"{h}d: {sub[col].mean()*100:.1f}% hit "
            f"({int(sub[col].sum())}/{len(sub)}) "
            f"avg_signed={sub[f'pnl_{h}d'].mean():+.2f}%"
        )

    print("\n=== BY THEME (20d) ===")
    for label, g in df.groupby("label"):
        sub = g.dropna(subset=["hit_20d"])
        if len(sub) < 3:
            continue
        print(
            f"{label:16s} n={len(sub):2d} "
            f"hit20={sub['hit_20d'].mean()*100:5.1f}% "
            f"avg={sub['pnl_20d'].mean():+.2f}% "
            f"med={sub['pnl_20d'].median():+.2f}%"
        )

    print("\n=== BY ASSET (20d) ===")
    for asset, g in df.groupby("asset"):
        sub = g.dropna(subset=["hit_20d"])
        if len(sub) < 3:
            continue
        print(
            f"{asset:4s} n={len(sub):2d} "
            f"hit20={sub['hit_20d'].mean()*100:5.1f}% "
            f"avg={sub['pnl_20d'].mean():+.2f}%"
        )

    print("\n=== BASELINE always-long from same publish dates (20d) ===")
    for asset in tickers:
        rets = []
        for r in rows:
            pub = r.get("published")
            if not pub or len(str(pub)) != 8:
                continue
            dt = datetime.strptime(str(pub), "%Y%m%d").date()
            ret = fwd_ret(px[asset], dt, 20)
            if ret is not None:
                rets.append(ret)
        if rets:
            arr = np.array(rets)
            print(
                f"{asset}: avg20={arr.mean():+.2f}% "
                f"pct_up={(arr > 0).mean()*100:.1f}% n={len(arr)}"
            )

    print("\n=== BIGGEST |pnl_20d| ===")
    sub = df.dropna(subset=["pnl_20d"]).copy()
    sub["abs"] = sub["pnl_20d"].abs()
    for _, row in sub.nlargest(10, "abs").iterrows():
        print(
            f"{row['published']} {row['label']:14s} {row['asset']} "
            f"dir={int(row['dir']):+d} ret20={row['ret_20d']:+.1f}% "
            f"pnl={row['pnl_20d']:+.1f}% | {row['title'][:55]}"
        )


if __name__ == "__main__":
    main()
