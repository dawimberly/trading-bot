"""Wayback Machine sentiment vs price-math simulation (academic arbitrage POC).

Fetches monthly archived finance headlines from the Internet Archive, scores
keyword sentiment, compares to SPY price momentum, and simulates simple
strategies on monthly rebalance.

Run:
  python scripts/research/simulate_wayback_sentiment.py
  python scripts/research/simulate_wayback_sentiment.py --from 2016 --to 2024
  python scripts/research/simulate_wayback_sentiment.py --refresh
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

import config

ROOT = Path(__file__).resolve().parents[2]
CACHE_PATH = ROOT / config.WAYBACK_SENTIMENT_FILE
RESULTS_PATH = ROOT / "wayback_simulation_results.csv"

USER_AGENT = "PythonTradingResearch/1.0 (sentiment backtest; contact: local)"
CDX_URL = "http://web.archive.org/cdx/search/cdx"
ARCHIVE_SOURCES = (
    ("finance.yahoo.com", "http://finance.yahoo.com/"),
    ("money.cnn.com", "http://money.cnn.com/"),
)

BULLISH = (
    "bullish",
    "rally",
    "surge",
    "gains",
    "upbeat",
    "record high",
    "soar",
    "boom",
    "recovery",
    "optimism",
)
BEARISH = (
    "bearish",
    "crash",
    "plunge",
    "decline",
    "fear",
    "recession",
    "selloff",
    "sell-off",
    "panic",
    "worries",
    "slump",
    "tumble",
)


def query_cdx_monthly(url_key: str, start: str, end: str) -> list[tuple[str, str]]:
    """Return (timestamp, original_url) pairs, one per month."""
    # Query year-by-year to avoid CDX gateway timeouts on broad ranges.
    start_year = int(start[:4])
    end_year = int(end[:4])
    seen_months: set[str] = set()
    out: list[tuple[str, str]] = []

    for year in range(start_year, end_year + 1):
        params = {
            "url": url_key,
            "output": "json",
            "filter": "statuscode:200",
            "collapse": "timestamp:6",
            "from": f"{year}0101",
            "to": f"{year}1231",
            "limit": 20,
        }
        for attempt in range(4):
            try:
                resp = requests.get(
                    CDX_URL,
                    params=params,
                    headers={"User-Agent": USER_AGENT},
                    timeout=180,
                )
                resp.raise_for_status()
                rows = resp.json()
                if len(rows) <= 1:
                    break
                for row in rows[1:]:
                    ts, original = row[1], row[2]
                    month_key = ts[:6]
                    if month_key in seen_months:
                        continue
                    seen_months.add(month_key)
                    out.append((ts, original))
                break
            except requests.RequestException as exc:
                wait = 2 ** attempt
                print(f"  CDX retry {url_key} {year} ({attempt + 1}/4): {exc}")
                time.sleep(wait)
        time.sleep(0.5)

    out.sort(key=lambda x: x[0])
    return out


def fetch_archive_text(timestamp: str, original_url: str) -> str:
    archive_url = f"https://web.archive.org/web/{timestamp}/{original_url}"
    last_err: Exception | None = None
    for attempt in range(5):
        try:
            resp = requests.get(
                archive_url, headers={"User-Agent": USER_AGENT}, timeout=120
            )
            resp.raise_for_status()
            html = resp.text
            html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.I | re.S)
            html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.I | re.S)
            html = re.sub(r"<[^>]+>", " ", html)
            return " ".join(html.split()).lower()
        except requests.RequestException as exc:
            last_err = exc
            time.sleep(2 ** attempt)
    raise last_err  # type: ignore[misc]


def score_text_sentiment(text: str) -> float:
    bull = sum(text.count(w) for w in BULLISH)
    bear = sum(text.count(w) for w in BEARISH)
    total = bull + bear
    if total == 0:
        return 0.0
    return round((bull - bear) / total, 4)


def build_wayback_sentiment(
    start_year: int, end_year: int, refresh: bool = False
) -> pd.DataFrame:
    if CACHE_PATH.exists() and not refresh:
        df = pd.read_csv(CACHE_PATH, parse_dates=["month"])
        return df

    existing: pd.DataFrame | None = None
    if CACHE_PATH.exists() and refresh:
        existing = pd.read_csv(CACHE_PATH, parse_dates=["month"])

    start = f"{start_year}0101"
    end = f"{end_year}1231"
    records: list[dict] = []

    for url_key, _original in ARCHIVE_SOURCES:
        snapshots = query_cdx_monthly(url_key, start, end)
        print(f"CDX {url_key}: {len(snapshots)} monthly snapshots")
        for i, (ts, original) in enumerate(snapshots):
            month = pd.Timestamp(ts[:4] + "-" + ts[4:6] + "-01")
            if existing is not None:
                hit = existing[
                    (existing["month"] == month) & (existing["source"] == url_key)
                ]
                if not hit.empty:
                    records.append(hit.iloc[0].to_dict())
                    continue
            try:
                text = fetch_archive_text(ts, original)
                score = score_text_sentiment(text)
                records.append(
                    {
                        "month": month,
                        "source": url_key,
                        "timestamp": ts,
                        "archive_url": f"https://web.archive.org/web/{ts}/{original}",
                        "sentiment": score,
                        "text_chars": len(text),
                    }
                )
                print(
                    f"  [{i + 1}/{len(snapshots)}] {month.date()} "
                    f"sentiment={score:+.3f} chars={len(text)}"
                )
            except Exception as exc:
                print(f"  skip {month.date()} {url_key}: {exc}")
            time.sleep(1.2)

    df = pd.DataFrame(records)
    if df.empty:
        raise RuntimeError("No Wayback sentiment rows fetched.")
    df = df.sort_values(["month", "source"]).reset_index(drop=True)
    df.to_csv(CACHE_PATH, index=False)
    print(f"Cached {len(df)} rows -> {CACHE_PATH}")
    return df


def load_spy_monthly(start_year: int, end_year: int) -> pd.DataFrame:
    start = f"{start_year - 1}-06-01"
    end = f"{end_year + 1}-01-01"
    raw = yf.download("SPY", start=start, end=end, progress=False, auto_adjust=True)
    if raw.empty:
        raise RuntimeError("No SPY data from yfinance.")
    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"]["SPY"]
    else:
        close = raw["Close"]
    close = close.dropna()
    monthly = close.resample("MS").last().dropna()
    ret = monthly.pct_change()
    mom20 = close.pct_change(20).resample("MS").last()
    mom60 = close.pct_change(60).resample("MS").last()
    vol20 = close.pct_change().rolling(20).std().resample("MS").last()

    out = pd.DataFrame(
        {
            "spy_close": monthly,
            "spy_return": ret,
            "price_mom20": mom20.reindex(monthly.index),
            "price_mom60": mom60.reindex(monthly.index),
            "realized_vol20": vol20.reindex(monthly.index),
        }
    )
    out.index.name = "month"
    return out


def aggregate_sentiment(wayback: pd.DataFrame) -> pd.DataFrame:
    """Average archive sources per month."""
    agg = (
        wayback.groupby("month", as_index=True)["sentiment"]
        .agg(["mean", "count"])
        .rename(columns={"mean": "web_sentiment", "count": "source_count"})
    )
    return agg


def run_simulation(
    panel: pd.DataFrame, gap_threshold: float = 0.25
) -> tuple[pd.DataFrame, dict]:
    """
    Monthly rebalance: signal at month-end t, P&L = SPY return in month t+1.
    Academic arbitrage: on sentiment vs price gap, prefer price math when they diverge.
    """
    df = panel.copy()
    df["fwd_return"] = df["spy_return"].shift(-1)
    df = df.dropna(subset=["fwd_return"])
    rows = []

    def record(name: str, position: float, row: pd.Series) -> None:
        pnl = position * row["fwd_return"]
        rows.append(
            {
                "strategy": name,
                "month": row.name,
                "position": position,
                "fwd_return": row["fwd_return"],
                "pnl": pnl,
            }
        )

    for month, row in df.iterrows():
        web = row.get("web_sentiment", np.nan)
        mom = row.get("price_mom20", np.nan)
        gap = row.get("sentiment_gap", np.nan)

        record("buy_hold", 1.0, row)

        if pd.notna(mom):
            record("price_math_only", 1.0 if mom > 0 else 0.0, row)
        if pd.notna(web):
            record("web_sentiment_only", 1.0 if web > 0 else 0.0, row)
        if pd.notna(web) and pd.notna(mom):
            aligned_bull = web > 0 and mom > 0
            aligned_bear = web < 0 and mom < 0
            record(
                "wisdom_aligned",
                1.0 if aligned_bull else (0.0 if aligned_bear else 0.5),
                row,
            )
            if pd.notna(gap) and abs(gap) >= gap_threshold:
                # Crowd vs price disagree: trust price math (Heidegger / authentic chart)
                record("arbitrage_trust_price", 1.0 if mom > 0 else 0.0, row)
            elif pd.notna(gap):
                # Agreement zone: full risk when both bullish, flat when both bearish
                record(
                    "arbitrage_trust_price",
                    1.0 if (web > 0 and mom > 0) else (0.0 if (web < 0 and mom < 0) else 0.5),
                    row,
                )

    trades = pd.DataFrame(rows)
    summary = {}
    for name, grp in trades.groupby("strategy"):
        equity = (1 + grp["pnl"]).cumprod()
        total_ret = (equity.iloc[-1] - 1) * 100 if len(equity) else 0.0
        sharpe = (
            grp["pnl"].mean() / grp["pnl"].std() * np.sqrt(12)
            if grp["pnl"].std() > 0
            else 0.0
        )
        summary[name] = {
            "months": len(grp),
            "total_return_pct": round(total_ret, 2),
            "avg_monthly_pct": round(grp["pnl"].mean() * 100, 3),
            "sharpe": round(sharpe, 2),
            "time_in_market": round(grp["position"].mean(), 3),
        }
    return trades, summary


def predictive_stats(panel: pd.DataFrame) -> dict:
    df = panel.copy()
    df["fwd_return"] = df["spy_return"].shift(-1)
    df = df.dropna(subset=["fwd_return", "web_sentiment", "price_mom20"])
    if len(df) < 6:
        return {}
    gap = df["web_sentiment"] - df["price_mom20"]
    return {
        "web_vs_next_month_return_corr": round(df["web_sentiment"].corr(df["fwd_return"]), 4),
        "price_mom_vs_next_month_return_corr": round(
            df["price_mom20"].corr(df["fwd_return"]), 4
        ),
        "gap_vs_next_month_return_corr": round(gap.corr(df["fwd_return"]), 4),
        "web_price_sentiment_corr": round(df["web_sentiment"].corr(df["price_mom20"]), 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Wayback sentiment simulation")
    parser.add_argument("--from", dest="year_from", type=int, default=2016)
    parser.add_argument("--to", dest="year_to", type=int, default=2024)
    parser.add_argument("--refresh", action="store_true", help="Re-fetch Wayback pages")
    parser.add_argument("--gap", type=float, default=0.25, help="Divergence threshold")
    args = parser.parse_args()

    print("=== Wayback Machine sentiment simulation ===")
    print(f"Window: {args.year_from}-{args.year_to}")

    wayback = build_wayback_sentiment(args.year_from, args.year_to, refresh=args.refresh)
    web = aggregate_sentiment(wayback)
    spy = load_spy_monthly(args.year_from, args.year_to)

    panel = spy.join(web, how="inner")
    panel = panel.loc[
        (panel.index.year >= args.year_from) & (panel.index.year <= args.year_to)
    ]
    panel["sentiment_gap"] = panel["web_sentiment"] - panel["price_mom20"]

    if panel.empty:
        raise RuntimeError("No overlapping Wayback + SPY months.")

    trades, summary = run_simulation(panel, gap_threshold=args.gap)
    trades.to_csv(RESULTS_PATH, index=False)

    stats = predictive_stats(panel)

    print(f"\nPanel: {len(panel)} months ({panel.index.min().date()} -> {panel.index.max().date()})")
    print("\n--- Predictive correlations (same month signal vs next-month SPY return) ---")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    print("\n--- Strategy results (monthly rebalance) ---")
    bench = summary.get("buy_hold", {})
    for name, row in sorted(summary.items(), key=lambda x: -x[1]["total_return_pct"]):
        beat = ""
        if name != "buy_hold" and bench:
            beat = f"  (vs B&H {bench['total_return_pct']:+.1f}%)"
        print(
            f"  {name:28} return {row['total_return_pct']:+7.2f}%  "
            f"Sharpe {row['sharpe']:5.2f}  in-market {row['time_in_market']:.0%}{beat}"
        )

    print(f"\nDetailed trades -> {RESULTS_PATH}")
    print(f"Sentiment cache -> {CACHE_PATH}")


if __name__ == "__main__":
    main()
