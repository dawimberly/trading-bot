"""Historical backtest of monthly wisdom rollups (2017–2023 Wayback era).

Simulates what the monthly evaluator would report each month:
per-mode fund returns, best mode, and chained equity paths.

Run:
  python scripts/research/backtest_wisdom_monthly.py
  python scripts/research/backtest_wisdom_monthly.py --from 2018 --to 2022
"""

from __future__ import annotations

import argparse
import json
import sys
from calendar import monthrange
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from backtester import MIN_HISTORY, WARMUP_CALENDAR_BUFFER, _ensure_daily_data
from backtester_wisdom import run_fund_backtest
from modules.wayback_sentiment import load_monthly_web_sentiment
from modules.wisdom_evaluator import _metrics_from_equity, _month_bounds, _recommendation
from modules.wisdom_sentiment import MODES

INITIAL = 10_000.0
OUT_CSV = "wisdom_monthly_backtest.csv"
OUT_SUMMARY = "wisdom_monthly_backtest_summary.json"


def _iter_months(y0: int, m0: int, y1: int, m1: int):
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        yield y, m
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1


def _slice_period_returns(
    equity_index: list[str], equity_values: list[float], y: int, month: int
) -> float | None:
    start, end = _month_bounds(y, month)
    idx = pd.to_datetime(equity_index)
    curve = pd.Series(equity_values, index=idx)
    if curve.index.tz is not None:
        curve.index = curve.index.tz_localize(None)
    sub = curve.loc[
        (curve.index >= pd.Timestamp(start)) & (curve.index <= pd.Timestamp(end))
    ]
    if len(sub) < 2:
        return None
    return (sub.iloc[-1] / sub.iloc[0] - 1) * 100


def _chain(monthly_returns: list[float], initial: float = INITIAL) -> dict:
    eq = initial
    curve = [eq]
    for r in monthly_returns:
        eq *= 1 + r / 100
        curve.append(eq)
    total = (eq / initial - 1) * 100
    rets = pd.Series([r / 100 for r in monthly_returns])
    sharpe = (
        (rets.mean() / rets.std()) * (12**0.5) if rets.std() > 0 else 0.0
    )
    eq_s = pd.Series(curve)
    max_dd = ((eq_s / eq_s.cummax()) - 1).min() * 100
    return {
        "final_equity": round(eq, 2),
        "total_return_pct": round(total, 2),
        "sharpe": round(float(sharpe), 2),
        "max_drawdown_pct": round(float(max_dd), 2),
        "months": len(monthly_returns),
    }


def run_backtest(year_from: int, year_to: int) -> None:
    monthly_web = load_monthly_web_sentiment()
    if monthly_web.empty:
        raise SystemExit("Missing wayback_sentiment.csv — run simulate_wayback_sentiment.py first.")

    web_start = monthly_web.index.min()
    start = date(max(year_from, web_start.year), max(1, web_start.month if web_start.year == year_from else 1), 1)
    end = date(year_to, 12, monthrange(year_to, 12)[1])

    data = _ensure_daily_data(0, refresh=False, use_max=True)
    if data.index.tz is not None:
        w = pd.Timestamp(start).tz_localize(data.index.tz) - pd.Timedelta(days=MIN_HISTORY + WARMUP_CALENDAR_BUFFER)
        e = pd.Timestamp(end).tz_localize(data.index.tz)
    else:
        w = pd.Timestamp(start) - pd.Timedelta(days=MIN_HISTORY + WARMUP_CALENDAR_BUFFER)
        e = pd.Timestamp(end)
    data = data.loc[(data.index >= w) & (data.index <= e)]
    print(f"Data: {len(data)} daily bars ({data.index.min().date()} -> {data.index.max().date()})")

    mode_curves: dict[str, dict] = {}
    for mode in MODES:
        print(f"Running full-period fund backtest: {mode} ...")
        row = run_fund_backtest(
            data, monthly_web, mode, gap_threshold=config.WISDOM_GAP_THRESHOLD
        )
        mode_curves[mode] = row

    months = list(_iter_months(start.year, start.month, end.year, end.month))
    rows = []
    mode_monthly: dict[str, list[float]] = {m: [] for m in MODES}
    oracle_returns: list[float] = []

    for y, m in months:
        month_key = f"{y:04d}-{m:02d}"
        sim = {}
        for mode in MODES:
            row = mode_curves[mode]
            ret = _slice_period_returns(row["equity_index"], row["equity_values"], y, m)
            if ret is not None:
                sim[mode] = {"return_pct": round(ret, 2)}
                mode_monthly[mode].append(ret)
            else:
                sim[mode] = {"return_pct": None}

        valid = {k: v for k, v in sim.items() if v.get("return_pct") is not None}
        best = (
            max(valid.items(), key=lambda x: x[1]["return_pct"])[0] if valid else None
        )
        if best:
            oracle_returns.append(valid[best]["return_pct"])

        live_stub = {"mode": "arbitrage", "return_pct": sim.get("arbitrage", {}).get("return_pct")}
        rec = _recommendation(live_stub if live_stub.get("return_pct") is not None else None, sim)

        row_out = {
            "month": month_key,
            "best_sim_mode": best,
            "recommendation": rec,
        }
        for mode in MODES:
            row_out[f"{mode}_return_pct"] = sim.get(mode, {}).get("return_pct")
        rows.append(row_out)

    df = pd.DataFrame(rows)
    df.to_csv(OUT_CSV, index=False)

    summary = {
        "period": f"{months[0][0]:04d}-{months[0][1]:02d} to {months[-1][0]:04d}-{months[-1][1]:02d}",
        "months": len(months),
        "gap_threshold": config.WISDOM_GAP_THRESHOLD,
        "strategies": {},
        "best_sim_mode_wins": df["best_sim_mode"].value_counts().to_dict(),
        "oracle_note": "Oracle = best sim mode each month (not achievable in advance).",
    }
    for mode in MODES:
        if mode_monthly[mode]:
            summary["strategies"][f"always_{mode}"] = _chain(mode_monthly[mode])
    if oracle_returns:
        summary["strategies"]["oracle_monthly_best"] = _chain(oracle_returns)

    # VTI benchmark by month from data
    if "VTI" in data.columns:
        vti = data["VTI"].dropna()
        vti_rets = []
        for y, m in months:
            pstart, pend = _month_bounds(y, m)
            if vti.index.tz is not None:
                a = pd.Timestamp(pstart).tz_localize(vti.index.tz)
                b = pd.Timestamp(pend).tz_localize(vti.index.tz)
            else:
                a, b = pd.Timestamp(pstart), pd.Timestamp(pend)
            sub = vti.loc[(vti.index >= a) & (vti.index <= b)]
            if len(sub) >= 2:
                vti_rets.append((sub.iloc[-1] / sub.iloc[0] - 1) * 100)
        if vti_rets:
            summary["strategies"]["vti_buy_hold"] = _chain(vti_rets)

    import json

    with open(OUT_SUMMARY, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("\n=== MONTHLY WISDOM BACKTEST ===")
    print(f"Months: {len(months)} | {summary['period']}")
    print(f"\nBest-mode wins per month: {summary['best_sim_mode_wins']}")
    print("\nChained $10k start (monthly returns compounded):")
    ranked = sorted(
        summary["strategies"].items(),
        key=lambda x: -x[1]["total_return_pct"],
    )
    for name, s in ranked:
        print(
            f"  {name:22} ${s['final_equity']:>12,.0f}  "
            f"{s['total_return_pct']:+8.1f}%  Sharpe {s['sharpe']:.2f}  "
            f"maxDD {s['max_drawdown_pct']:.1f}%"
        )
    print(f"\nMonthly detail -> {OUT_CSV}")
    print(f"Summary JSON  -> {OUT_SUMMARY}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="y0", type=int, default=2017)
    parser.add_argument("--to", dest="y1", type=int, default=2023)
    args = parser.parse_args()
    run_backtest(args.y0, args.y1)


if __name__ == "__main__":
    main()
