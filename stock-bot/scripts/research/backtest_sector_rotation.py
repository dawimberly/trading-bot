"""Standalone weekly sector-rotation backtest (no live trading / Alpaca / sleeve).

Ranks 11 sector ETFs by 20-day momentum each Monday, holds top 2 with
optional SPY yield-gate sizing and MA200 regime filter. Compares:
  - VTI buy-and-hold
  - Rotation sleeve alone (10% deployed when invested)
  - 80% VTI + 10% sector rotation (+ 10% cash)

Run:
  python scripts/research/backtest_sector_rotation.py
  python scripts/research/backtest_sector_rotation.py --days 730
"""

from __future__ import annotations

import argparse
import pickle
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[2]
CACHE_PATH = ROOT / "data" / "sector_backtest_cache.pkl"
OUT_CSV = Path(__file__).resolve().parent / "sector_rotation_backtest_results.csv"

SECTOR_ETFS: dict[str, str] = {
    "XLK": "Technology",
    "XLF": "Financials",
    "XLV": "Healthcare",
    "XLE": "Energy",
    "XLI": "Industrials",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLB": "Materials",
    "XLU": "Utilities",
    "XLRE": "Real Estate",
    "XLC": "Communication",
}

BENCHMARKS = ("SPY", "VTI")
ALL_TICKERS = tuple(SECTOR_ETFS.keys()) + BENCHMARKS

MOMENTUM_DAYS = 20
SPY_YIELD_LOOKBACK = 10
SPY_YIELD_GATE = -0.03
MA200 = 200
POS_PCT = 0.05
POS_PCT_GATED = 0.025
TRADE_FEE = 0.0005  # 0.05% per entry or exit
WARMUP_CALENDAR = 220
INITIAL = 100_000.0
VTI_WEIGHT = 0.80
SECTOR_BOOK = 0.10  # max sector notional fraction of portfolio


@dataclass
class Position:
    etf: str
    sector: str
    shares: float
    entry_price: float
    entry_week: pd.Timestamp
    momentum_score: float
    weeks_held: int = 1


@dataclass
class TradeRow:
    week_start: str
    sector: str
    etf: str
    action: str
    momentum_score: float
    entry_price: float
    exit_price: float | None
    pnl_pct: float | None


@dataclass
class SimResult:
    equity: pd.Series
    trades: list[TradeRow] = field(default_factory=list)
    picked: Counter = field(default_factory=Counter)
    hold_streaks: list[int] = field(default_factory=list)
    rotation_weeks: int = 0
    current_top2: list[tuple[str, str, float]] = field(default_factory=list)
    weeks: list[pd.Timestamp] = field(default_factory=list)


def _normalize_ohlcv_index(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index)
    if out.index.tz is not None:
        out.index = out.index.tz_localize(None)
    out.index = out.index.normalize()
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


def _flatten_yf(raw: pd.DataFrame, tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Normalize yfinance multi-ticker download into per-symbol OHLCV frames."""
    out: dict[str, pd.DataFrame] = {}
    if raw.empty:
        return out

    if isinstance(raw.columns, pd.MultiIndex):
        level0 = {str(x) for x in raw.columns.get_level_values(0)}
        if "Close" in level0 or "Adj Close" in level0:
            ticker_level = 1
        else:
            ticker_level = 0
        for t in tickers:
            try:
                sub = raw.xs(t, axis=1, level=ticker_level, drop_level=True)
            except KeyError:
                continue
            sub = sub.copy()
            sub.columns = [str(c) for c in sub.columns]
            out[t] = _normalize_ohlcv_index(sub)
    else:
        t = tickers[0]
        sub = raw.copy()
        sub.columns = [str(c) for c in sub.columns]
        out[t] = _normalize_ohlcv_index(sub)
    return out


def fetch_data(days: int, force_refresh: bool = False) -> dict[str, pd.DataFrame]:
    """Load daily OHLCV for sector ETFs + SPY + VTI, with pickle cache."""
    end = pd.Timestamp.today().normalize()
    start = end - pd.Timedelta(days=days + WARMUP_CALENDAR)

    if CACHE_PATH.exists() and not force_refresh:
        try:
            with open(CACHE_PATH, "rb") as f:
                cached: dict[str, Any] = pickle.load(f)
            frames: dict[str, pd.DataFrame] = cached.get("frames", {})
            if set(ALL_TICKERS).issubset(frames.keys()):
                ok = True
                for t in ALL_TICKERS:
                    fr = _normalize_ohlcv_index(frames[t])
                    if fr.empty or fr.index.min() > start + pd.Timedelta(days=5):
                        ok = False
                        break
                    frames[t] = fr
                if ok:
                    lo = min(frames[t].index.min() for t in ALL_TICKERS)
                    hi = max(frames[t].index.max() for t in ALL_TICKERS)
                    print(f"Cache hit: {CACHE_PATH} ({lo.date()} -> {hi.date()})")
                    return frames
        except Exception as exc:  # noqa: BLE001
            print(f"Cache unusable ({exc}); re-downloading...")

    tickers = list(ALL_TICKERS)
    print(
        f"Downloading {len(tickers)} symbols via yfinance "
        f"({start.date()} -> {end.date()})..."
    )
    raw = yf.download(
        tickers,
        start=start.strftime("%Y-%m-%d"),
        end=(end + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=True,
    )
    frames = _flatten_yf(raw, tickers)
    missing = [t for t in tickers if t not in frames or frames[t].empty]
    if missing:
        raise SystemExit(f"Missing yfinance data for: {missing}")

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "wb") as f:
        pickle.dump({"frames": frames, "saved_at": str(pd.Timestamp.now())}, f)
    print(f"Cached -> {CACHE_PATH}")
    return frames


def _price_at(df: pd.DataFrame, ts: pd.Timestamp, prefer_open: bool) -> float:
    row = df.loc[ts]
    if prefer_open:
        if "Open" in df.columns and pd.notna(row.get("Open", np.nan)):
            return float(row["Open"])
        return float(row["Close"])
    return float(row["Close"])


def _aligned_closes(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    closes = {t: frames[t]["Close"].astype(float) for t in frames}
    return pd.DataFrame(closes).dropna(how="any").sort_index()


def _week_rebalance_days(
    index: pd.DatetimeIndex, start: pd.Timestamp, end: pd.Timestamp
) -> list[pd.Timestamp]:
    """First trading day of each ISO week in [start, end] (Monday when available)."""
    days = index[(index >= start) & (index <= end)]
    by_week: dict[tuple[int, int], pd.Timestamp] = {}
    for ts in days:
        iso = ts.isocalendar()
        key = (int(iso.year), int(iso.week))
        if key not in by_week:
            by_week[key] = ts
    return sorted(by_week.values())


def _momentum(closes: pd.DataFrame, ts: pd.Timestamp) -> dict[str, float]:
    hist = closes.loc[:ts]
    if len(hist) < MOMENTUM_DAYS + 1:
        return {}
    latest = hist.iloc[-1]
    past = hist.iloc[-(MOMENTUM_DAYS + 1)]
    scores: dict[str, float] = {}
    for t in SECTOR_ETFS:
        a, b = float(latest[t]), float(past[t])
        if b <= 0 or np.isnan(a) or np.isnan(b):
            continue
        scores[t] = a / b - 1.0
    return scores


def _spy_below_ma200(closes: pd.DataFrame, ts: pd.Timestamp) -> bool:
    hist = closes["SPY"].loc[:ts].dropna()
    if len(hist) < MA200:
        return True
    ma = float(hist.iloc[-MA200:].mean())
    return float(hist.iloc[-1]) < ma


def _spy_10d_return(closes: pd.DataFrame, ts: pd.Timestamp) -> float:
    hist = closes["SPY"].loc[:ts].dropna()
    if len(hist) < SPY_YIELD_LOOKBACK + 1:
        return 0.0
    return float(hist.iloc[-1] / hist.iloc[-(SPY_YIELD_LOOKBACK + 1)] - 1.0)


def _metrics(equity: pd.Series) -> dict[str, float | str]:
    eq = equity.dropna()
    if len(eq) < 2:
        return {
            "total_return_pct": 0.0,
            "sharpe": 0.0,
            "max_drawdown_pct": 0.0,
            "best_month": "n/a",
            "worst_month": "n/a",
        }
    rets = eq.pct_change().dropna()
    total = float(eq.iloc[-1] / eq.iloc[0] - 1.0) * 100.0
    vol = float(rets.std())
    sharpe = float(rets.mean() / vol * np.sqrt(252)) if vol > 0 else 0.0
    dd = float((eq / eq.cummax() - 1.0).min() * 100.0)
    monthly = eq.resample("ME").last().pct_change().dropna()
    if monthly.empty:
        best_m, worst_m = "n/a", "n/a"
    else:
        best_idx = monthly.idxmax()
        worst_idx = monthly.idxmin()
        best_m = f"{best_idx.strftime('%Y-%m')} {monthly.max() * 100:+.2f}%"
        worst_m = f"{worst_idx.strftime('%Y-%m')} {monthly.min() * 100:+.2f}%"
    return {
        "total_return_pct": round(total, 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(dd, 2),
        "best_month": best_m,
        "worst_month": worst_m,
    }


def _avg_hold_weeks(streaks: list[int]) -> float:
    if not streaks:
        return 0.0
    return round(float(np.mean(streaks)), 2)


def _top2_from_scores(scores: dict[str, float], flat: bool) -> list[str]:
    if flat or not scores:
        return []
    return [t for t, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)[:2]]


def simulate_rotation(
    frames: dict[str, pd.DataFrame],
    closes: pd.DataFrame,
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
) -> SimResult:
    """Sector sleeve in isolation: up to 10% equity in top-2, rest cash at 0%."""
    rebalance_days = _week_rebalance_days(closes.index, window_start, window_end)
    daily_idx = closes.index[(closes.index >= window_start) & (closes.index <= window_end)]
    rebalance_set = set(rebalance_days)

    cash = INITIAL
    positions: dict[str, Position] = {}
    equity_points: dict[pd.Timestamp, float] = {}
    trades: list[TradeRow] = []
    picked: Counter = Counter()
    hold_streaks: list[int] = []
    rotation_weeks = 0
    current_top2: list[tuple[str, str, float]] = []

    def mark_equity(ts: pd.Timestamp) -> float:
        total = cash
        for etf, pos in positions.items():
            total += pos.shares * float(closes.loc[ts, etf])
        return total

    def close_position(etf: str, ts: pd.Timestamp, px: float) -> None:
        nonlocal cash
        pos = positions.pop(etf)
        notional = pos.shares * px
        cash += notional * (1.0 - TRADE_FEE)
        pnl_pct = (px * (1.0 - TRADE_FEE) / (pos.entry_price * (1.0 + TRADE_FEE)) - 1.0) * 100.0
        trades.append(
            TradeRow(
                week_start=ts.strftime("%Y-%m-%d"),
                sector=pos.sector,
                etf=etf,
                action="sell",
                momentum_score=pos.momentum_score,
                entry_price=round(pos.entry_price, 4),
                exit_price=round(px, 4),
                pnl_pct=round(pnl_pct, 4),
            )
        )
        hold_streaks.append(pos.weeks_held)

    for ts in daily_idx:
        if ts in rebalance_set:
            scores = _momentum(closes, ts)
            flat = _spy_below_ma200(closes, ts)
            gated = _spy_10d_return(closes, ts) < SPY_YIELD_GATE
            size_pct = POS_PCT_GATED if gated else POS_PCT
            top2 = _top2_from_scores(scores, flat)
            current_top2 = [
                (SECTOR_ETFS[t], t, round(scores.get(t, 0.0), 6)) for t in top2
            ]

            for etf in list(positions.keys()):
                if etf not in top2:
                    close_position(etf, ts, _price_at(frames[etf], ts, prefer_open=True))
                else:
                    positions[etf].weeks_held += 1

            bought = False
            eq_pre = mark_equity(ts)
            for etf in top2:
                if etf in positions:
                    picked[etf] += 1
                    continue
                px = _price_at(frames[etf], ts, prefer_open=True)
                target_notional = eq_pre * size_pct
                cost = target_notional * (1.0 + TRADE_FEE)
                if cost > cash:
                    cost = max(0.0, cash)
                    target_notional = cost / (1.0 + TRADE_FEE)
                if target_notional <= 0 or px <= 0:
                    continue
                shares = target_notional / px
                cash -= cost
                mom = float(scores.get(etf, 0.0))
                positions[etf] = Position(
                    etf=etf,
                    sector=SECTOR_ETFS[etf],
                    shares=shares,
                    entry_price=px,
                    entry_week=ts,
                    momentum_score=mom,
                    weeks_held=1,
                )
                picked[etf] += 1
                bought = True
                trades.append(
                    TradeRow(
                        week_start=ts.strftime("%Y-%m-%d"),
                        sector=SECTOR_ETFS[etf],
                        etf=etf,
                        action="buy",
                        momentum_score=round(mom, 6),
                        entry_price=round(px, 4),
                        exit_price=None,
                        pnl_pct=None,
                    )
                )
            if bought:
                rotation_weeks += 1

        equity_points[ts] = mark_equity(ts)

    for pos in positions.values():
        hold_streaks.append(pos.weeks_held)

    return SimResult(
        equity=pd.Series(equity_points).sort_index(),
        trades=trades,
        picked=picked,
        hold_streaks=hold_streaks,
        rotation_weeks=rotation_weeks,
        current_top2=current_top2,
        weeks=rebalance_days,
    )


def simulate_vti_hold(
    closes: pd.DataFrame,
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
) -> pd.Series:
    sub = closes["VTI"].loc[(closes.index >= window_start) & (closes.index <= window_end)].dropna()
    if sub.empty:
        return pd.Series(dtype=float)
    return INITIAL * (sub / float(sub.iloc[0]))


def simulate_combined(
    frames: dict[str, pd.DataFrame],
    closes: pd.DataFrame,
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
) -> pd.Series:
    """80% VTI buy-and-hold + sector sleeve sized at 5%/2.5% of total equity + cash."""
    rebalance_days = _week_rebalance_days(closes.index, window_start, window_end)
    daily_idx = closes.index[(closes.index >= window_start) & (closes.index <= window_end)]
    rebalance_set = set(rebalance_days)
    if daily_idx.empty:
        return pd.Series(dtype=float)

    first = daily_idx[0]
    vti0 = float(closes.loc[first, "VTI"])
    vti_shares = (INITIAL * VTI_WEIGHT) / vti0
    cash = INITIAL * (1.0 - VTI_WEIGHT)  # 20%: sector budget + idle cash
    positions: dict[str, Position] = {}
    equity_points: dict[pd.Timestamp, float] = {}

    def mark_equity(ts: pd.Timestamp) -> float:
        total = cash + vti_shares * float(closes.loc[ts, "VTI"])
        for etf, pos in positions.items():
            total += pos.shares * float(closes.loc[ts, etf])
        return total

    def close_position(etf: str, ts: pd.Timestamp, px: float) -> None:
        nonlocal cash
        pos = positions.pop(etf)
        cash += pos.shares * px * (1.0 - TRADE_FEE)

    for ts in daily_idx:
        if ts in rebalance_set:
            scores = _momentum(closes, ts)
            flat = _spy_below_ma200(closes, ts)
            gated = _spy_10d_return(closes, ts) < SPY_YIELD_GATE
            size_pct = POS_PCT_GATED if gated else POS_PCT
            top2 = _top2_from_scores(scores, flat)

            for etf in list(positions.keys()):
                if etf not in top2:
                    close_position(etf, ts, _price_at(frames[etf], ts, prefer_open=True))

            eq_pre = mark_equity(ts)
            for etf in top2:
                if etf in positions:
                    continue
                px = _price_at(frames[etf], ts, prefer_open=True)
                target_notional = eq_pre * size_pct  # 5% or 2.5%; top-2 ≈ SECTOR_BOOK
                cost = target_notional * (1.0 + TRADE_FEE)
                if cost > cash:
                    cost = max(0.0, cash)
                    target_notional = cost / (1.0 + TRADE_FEE)
                if target_notional <= 0 or px <= 0:
                    continue
                shares = target_notional / px
                cash -= cost
                positions[etf] = Position(
                    etf=etf,
                    sector=SECTOR_ETFS[etf],
                    shares=shares,
                    entry_price=px,
                    entry_week=ts,
                    momentum_score=float(scores.get(etf, 0.0)),
                    weeks_held=1,
                )

        equity_points[ts] = mark_equity(ts)

    return pd.Series(equity_points).sort_index()


def _print_table(
    m_vti: dict[str, float | str],
    m_rot: dict[str, float | str],
    m_comb: dict[str, float | str],
    rot: SimResult,
) -> None:
    rows = [
        ("Total return %", m_vti["total_return_pct"], m_rot["total_return_pct"], m_comb["total_return_pct"]),
        ("Sharpe", m_vti["sharpe"], m_rot["sharpe"], m_comb["sharpe"]),
        ("Max drawdown %", m_vti["max_drawdown_pct"], m_rot["max_drawdown_pct"], m_comb["max_drawdown_pct"]),
        ("Best month", m_vti["best_month"], m_rot["best_month"], m_comb["best_month"]),
        ("Worst month", m_vti["worst_month"], m_rot["worst_month"], m_comb["worst_month"]),
        ("Total rotations", "-", rot.rotation_weeks, "-"),
        ("Avg hold weeks", "-", _avg_hold_weeks(rot.hold_streaks), "-"),
    ]
    headers = ("Metric", "VTI Hold", "Rotation Only", "VTI + Rotation")
    w0 = max(len(headers[0]), max(len(str(r[0])) for r in rows))
    w1 = max(len(headers[1]), max(len(str(r[1])) for r in rows))
    w2 = max(len(headers[2]), max(len(str(r[2])) for r in rows))
    w3 = max(len(headers[3]), max(len(str(r[3])) for r in rows))
    sep = f"+-{'-' * w0}-+-{'-' * w1}-+-{'-' * w2}-+-{'-' * w3}-+"
    print(sep)
    print(f"| {headers[0]:<{w0}} | {headers[1]:>{w1}} | {headers[2]:>{w2}} | {headers[3]:>{w3}} |")
    print(sep)
    for r in rows:
        print(
            f"| {str(r[0]):<{w0}} | {str(r[1]):>{w1}} | {str(r[2]):>{w2}} | {str(r[3]):>{w3}} |"
        )
    print(sep)


def _print_pick_stats(rot: SimResult) -> None:
    counts = rot.picked
    most = counts.most_common()
    least_map = {t: counts.get(t, 0) for t in SECTOR_ETFS}
    least = sorted(least_map.items(), key=lambda x: (x[1], x[0]))

    print("\nSectors picked most often:")
    if not most:
        print("  (none)")
    else:
        for etf, n in most[:5]:
            print(f"  {etf:5s} {SECTOR_ETFS[etf]:<24s} {n} weeks")
    print("Sectors avoided most often:")
    for etf, n in least[:5]:
        print(f"  {etf:5s} {SECTOR_ETFS[etf]:<24s} {n} weeks")
    print("Current week top 2:")
    if not rot.current_top2:
        print("  (flat / no score — regime filter or insufficient data)")
    else:
        for sector, etf, mom in rot.current_top2:
            print(f"  {etf:5s} {sector:<24s} momentum={mom:+.4%}")


def save_trades_csv(trades: list[TradeRow], path: Path) -> None:
    rows = [
        {
            "week_start": t.week_start,
            "sector": t.sector,
            "etf": t.etf,
            "action": t.action,
            "momentum_score": t.momentum_score,
            "entry_price": t.entry_price,
            "exit_price": "" if t.exit_price is None else t.exit_price,
            "pnl_pct": "" if t.pnl_pct is None else t.pnl_pct,
        }
        for t in trades
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)
    print(f"\nWrote {path} ({len(rows)} rows)")


def run(days: int, force_refresh: bool = False) -> None:
    frames = fetch_data(days, force_refresh=force_refresh)
    closes = _aligned_closes(frames)
    if closes.empty:
        raise SystemExit("No aligned close data.")

    window_end = closes.index.max()
    target_start = window_end - pd.Timedelta(days=days)
    need_start = target_start - pd.Timedelta(days=WARMUP_CALENDAR)
    if closes.index.min() > need_start + pd.Timedelta(days=10):
        print(
            f"Warning: history starts {closes.index.min().date()}, "
            f"wanted ~{need_start.date()} for MA200 warmup."
        )

    sessions = closes.index[closes.index >= target_start]
    if sessions.empty:
        raise SystemExit("No sessions in requested window.")
    window_start = sessions[0]

    print(
        f"\nBacktest window: {window_start.date()} -> {window_end.date()} "
        f"(~{days} calendar days, {len(sessions)} sessions)"
    )
    print(
        f"Rules: top-2 by {MOMENTUM_DAYS}d momentum | "
        f"size {POS_PCT:.1%}/{POS_PCT_GATED:.1%} | "
        f"SPY 10d gate {SPY_YIELD_GATE:.0%} | MA{MA200} regime | "
        f"fee {TRADE_FEE:.2%}/side"
    )

    vti_eq = simulate_vti_hold(closes, window_start, window_end)
    rot = simulate_rotation(frames, closes, window_start, window_end)
    comb_eq = simulate_combined(frames, closes, window_start, window_end)

    m_vti = _metrics(vti_eq)
    m_rot = _metrics(rot.equity)
    m_comb = _metrics(comb_eq)

    print()
    _print_table(m_vti, m_rot, m_comb, rot)
    _print_pick_stats(rot)
    save_trades_csv(rot.trades, OUT_CSV)


def main() -> None:
    parser = argparse.ArgumentParser(description="Weekly sector rotation backtest")
    parser.add_argument(
        "--days",
        type=int,
        default=365,
        help="Backtest calendar-day window (default 365; try 730)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force re-download and overwrite cache",
    )
    args = parser.parse_args()
    run(days=args.days, force_refresh=args.refresh)


if __name__ == "__main__":
    main()
