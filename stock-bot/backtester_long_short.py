"""Fund backtest: long sleeves + dedicated short sleeve (SPY below MA200).

Capital is split at start between a long book (SPY + crypto + NYSE) and a short
book that mirrors the SPY long rule in reverse. Combined equity is reported.

Allocation model (recommended for live):
  - Fixed split: e.g. 85% long fund / 15% short sleeve (parallel sub-accounts).
  - Long sleeves keep their internal targets (45/20/20 + cash) on the long slice only.
  - Short sleeve only trades SPY short; max exposure ≈ short book equity.

Run:
  python backtester_long_short.py
  python backtester_long_short.py --from 2017 --to 2023
  python backtester_long_short.py --compare-alloc
  python backtester_long_short.py --short-pct 0.15 --wisdom baseline
"""

from __future__ import annotations

import argparse
import warnings

import numpy as np
import pandas as pd

import config
from backtester import (
    BENCHMARK,
    DAILY_COOLDOWN_BARS,
    MIN_HISTORY,
    BacktestExecutor,
    BacktestPortfolio,
    TX_COST,
    _benchmark_return,
    _ensure_daily_data,
)
from modules.backtest_common import add_year_range_args, slice_data_by_year as _slice_data
from backtester_wisdom import run_fund_backtest
from modules.market_context import get_market_regime, get_price_sentiment, get_volatility
from modules.pipeline_strategies import (
    PAUSED_REGIMES,
    run_crypto_strategy,
    run_equity_strategy,
    run_spy_strategy,
    _spy_market_up_signal,
)
from modules.risk_management import RiskManager
from modules.wayback_sentiment import load_monthly_web_sentiment
from modules.wisdom_sentiment import entries_paused, regime_sentiment, PAUSE_REGIME

warnings.filterwarnings("ignore", category=RuntimeWarning)

DEFAULT_SHORT_PCT = 0.15
ALLOC_GRID = (0.0, 0.10, 0.15, 0.20)


class ShortBacktestPortfolio:
    """Negative qty = short; equity = cash + qty * mark."""

    def __init__(self, initial_capital: float):
        self.cash = initial_capital
        self.initial_capital = initial_capital
        self.positions: dict[str, float] = {}

    def equity(self, prices) -> float:
        total = self.cash
        for symbol, qty in self.positions.items():
            p = prices.get(symbol)
            if p is not None and np.isfinite(p):
                total += qty * p
        return total

    def short_qty(self, symbol: str) -> float:
        qty = self.positions.get(symbol, 0.0)
        return abs(qty) if qty < 0 else 0.0

    def _size_notional(self, prices, notional=None) -> float:
        if notional is not None:
            return max(config.MIN_NOTIONAL, round(notional, 2))
        eq = self.equity(prices)
        raw = round(min(eq * config.RISK_PER_TRADE, config.MAX_NOTIONAL_PER_ORDER), 2)
        return max(config.MIN_NOTIONAL, raw)

    def open_short(self, symbol: str, price: float, prices, notional=None):
        notional = self._size_notional(prices, notional)
        if notional < 1:
            return None
        qty = notional / price
        proceeds = notional * (1 - TX_COST)
        self.cash += proceeds
        self.positions[symbol] = self.positions.get(symbol, 0.0) - qty
        return {"symbol": symbol, "side": "sell", "qty": qty, "notional": notional}

    def cover_short(self, symbol: str, price: float, notional=None):
        qty = self.positions.get(symbol, 0.0)
        if qty >= 0:
            return None
        short_qty = abs(qty)
        if notional is None:
            cover_qty = short_qty
        else:
            cover_qty = min(short_qty, notional / price)
        cost = cover_qty * price * (1 + TX_COST)
        if cost > self.cash:
            cover_qty = min(short_qty, self.cash / (price * (1 + TX_COST)))
            cost = cover_qty * price * (1 + TX_COST)
        if cover_qty <= 0:
            return None
        self.cash -= cost
        self.positions[symbol] = qty + cover_qty
        if abs(self.positions[symbol]) < 1e-9:
            del self.positions[symbol]
        return {
            "symbol": symbol,
            "side": "buy",
            "qty": cover_qty,
            "notional": cover_qty * price,
        }


class ShortBacktestExecutor:
    def __init__(self, portfolio: ShortBacktestPortfolio, prices):
        self.portfolio = portfolio
        self.prices = prices
        self.orders = []

    @property
    def client(self):
        return self

    def get_all_positions(self):
        positions = []
        for symbol, qty in self.portfolio.positions.items():
            if qty >= 0:
                continue
            p = self.prices.get(symbol)
            positions.append(
                type(
                    "Pos",
                    (),
                    {"symbol": symbol, "qty": qty, "avg_entry_price": p, "current_price": p},
                )()
            )
        return positions

    def execute_order(self, symbol, side, notional=None, reduce_only=False):
        price = self.prices.get(symbol)
        if price is None or not np.isfinite(price) or price <= 0:
            return None
        side = side.lower()
        if side == "sell" and not reduce_only:
            order = self.portfolio.open_short(symbol, price, self.prices, notional=notional)
        elif side == "buy":
            order = self.portfolio.cover_short(symbol, price, notional=notional)
        else:
            order = None
        if order:
            self.orders.append(order)
        return order


def _on_short_cooldown(pair_cooldown, key, now, cooldown_bars):
    last = pair_cooldown.get(key)
    if last is None:
        return False
    return (now - last) < cooldown_bars


def run_spy_short_strategy(
    data,
    executor: ShortBacktestExecutor,
    regime,
    now,
    pair_cooldown,
    *,
    symbol=None,
    ma_window=None,
    cooldown_bars=DAILY_COOLDOWN_BARS,
    max_short_pct=0.95,
):
    """Short SPY when price is below MA (inverse of long SPY sleeve)."""
    symbol = symbol or config.SPY_BOT_SYMBOL
    ma_window = ma_window or config.SPY_MA_WINDOW
    if regime in PAUSED_REGIMES:
        return 0
    bullish, momentum = _spy_market_up_signal(data, symbol, ma_window)
    if bullish:
        return 0

    eq = executor.portfolio.equity(executor.prices)
    if eq <= 0:
        return 0
    current_short = executor.portfolio.short_qty(symbol) * executor.prices.get(symbol, 0)
    cap = eq * max_short_pct
    if current_short >= cap * 0.98:
        return 0

    pair_key = f"{symbol}/SHORT/MA{ma_window}"
    if _on_short_cooldown(pair_cooldown, pair_key, now, cooldown_bars):
        return 0

    room = max(0.0, cap - current_short)
    notional = min(room, eq * config.RISK_PER_TRADE, config.MAX_NOTIONAL_PER_ORDER)
    notional = max(config.MIN_NOTIONAL, round(notional, 2))
    order = executor.execute_order(symbol, "sell", notional=notional)
    if order:
        pair_cooldown[pair_key] = now
        return 1
    return 0


def run_spy_short_exits(data, executor: ShortBacktestExecutor, *, symbol=None, ma_window=None):
    """Cover short when SPY closes back above MA."""
    symbol = symbol or config.SPY_BOT_SYMBOL
    ma_window = ma_window or config.SPY_MA_WINDOW
    if executor.portfolio.short_qty(symbol) <= 0:
        return 0
    bullish, _ = _spy_market_up_signal(data, symbol, ma_window)
    if not bullish:
        return 0
    order = executor.execute_order(symbol, "buy")
    return 1 if order else 0


def _run_long_book_day(
    data,
    i,
    long_portfolio,
    pair_cooldown,
    *,
    wisdom_mode=None,
    monthly_web=None,
    gap_threshold=0.25,
):
    window = data.iloc[: i + 1]
    prices = window.iloc[-1]
    ts = data.index[i]

    if wisdom_mode and monthly_web is not None:
        vol = get_volatility(window)
        sent, web, gap = regime_sentiment(
            window, ts, monthly_web, mode=wisdom_mode, gap_threshold=gap_threshold
        )
        regime = get_market_regime(sent, vol)
        if entries_paused(wisdom_mode, web, gap, gap_threshold, data=window, vol=vol):
            regime = PAUSE_REGIME
    else:
        sentiment = get_price_sentiment(window)
        vol = get_volatility(window)
        regime = get_market_regime(sentiment, vol)

    executor = BacktestExecutor(long_portfolio, prices)
    spy_n = run_spy_strategy(
        window, executor, regime, i, pair_cooldown, cooldown_bars=DAILY_COOLDOWN_BARS
    )
    crypto_n = run_crypto_strategy(
        window,
        executor,
        regime,
        i,
        pair_cooldown,
        cooldown_bars=DAILY_COOLDOWN_BARS,
        volatility=vol,
    )
    equity_n = run_equity_strategy(
        window, executor, regime, i, pair_cooldown, cooldown_bars=DAILY_COOLDOWN_BARS
    )
    return regime, spy_n + crypto_n + equity_n


def run_combined_backtest(
    data: pd.DataFrame,
    *,
    short_pct: float = DEFAULT_SHORT_PCT,
    initial_capital: float = 10_000.0,
    wisdom_mode: str | None = None,
    monthly_web: pd.Series | None = None,
    gap_threshold: float = 0.25,
) -> dict:
    if len(data) < MIN_HISTORY:
        raise ValueError(f"Need {MIN_HISTORY}+ rows; got {len(data)}")

    short_pct = max(0.0, min(0.5, short_pct))
    long_pct = 1.0 - short_pct
    long_cap = initial_capital * long_pct
    short_cap = initial_capital * short_pct

    long_portfolio = BacktestPortfolio(long_cap)
    short_portfolio = ShortBacktestPortfolio(short_cap) if short_pct > 0 else None
    long_cooldown = {}
    short_cooldown = {}
    risk_manager = RiskManager(max_drawdown_pct=config.MAX_DRAWDOWN_PCT)

    equity_curve = []
    long_curve = []
    short_curve = []
    regime_counts = {}
    total_long_orders = 0
    total_short_signals = 0
    total_short_covers = 0
    halted = False
    start_i = MIN_HISTORY

    for i in range(start_i, len(data)):
        window = data.iloc[: i + 1]
        prices = window.iloc[-1]

        long_eq = long_portfolio.equity(prices)
        short_eq = short_portfolio.equity(prices) if short_portfolio else 0.0
        combined = long_eq + short_eq
        equity_curve.append(combined)
        long_curve.append(long_eq)
        short_curve.append(short_eq)

        if halted or not risk_manager.check_drawdown(combined):
            if not halted:
                halted = True
            continue

        regime, long_orders = _run_long_book_day(
            data,
            i,
            long_portfolio,
            long_cooldown,
            wisdom_mode=wisdom_mode,
            monthly_web=monthly_web,
            gap_threshold=gap_threshold,
        )
        total_long_orders += long_orders
        regime_counts[regime] = regime_counts.get(regime, 0) + 1

        if short_portfolio is not None:
            short_exec = ShortBacktestExecutor(short_portfolio, prices)
            total_short_covers += run_spy_short_exits(window, short_exec)
            total_short_signals += run_spy_short_strategy(
                window,
                short_exec,
                regime,
                i,
                short_cooldown,
                cooldown_bars=DAILY_COOLDOWN_BARS,
            )
            total_long_orders += len(short_exec.orders)

    curve = pd.Series(equity_curve, index=data.index[start_i:])
    long_s = pd.Series(long_curve, index=data.index[start_i:])
    short_s = pd.Series(short_curve, index=data.index[start_i:])
    returns = curve.pct_change().dropna()
    total_ret = (curve.iloc[-1] / initial_capital - 1) * 100
    sharpe = (
        (returns.mean() / returns.std()) * np.sqrt(252) if returns.std() != 0 else 0.0
    )
    max_dd = ((curve / curve.cummax()) - 1).min() * 100
    bench = _benchmark_return(data, start_i)

    return {
        "short_pct": short_pct,
        "long_pct": long_pct,
        "wisdom_mode": wisdom_mode or "price_only",
        "final_equity": round(curve.iloc[-1], 2),
        "long_final": round(long_s.iloc[-1], 2),
        "short_final": round(short_s.iloc[-1], 2),
        "total_return_pct": round(total_ret, 2),
        "long_return_pct": round((long_s.iloc[-1] / long_cap - 1) * 100, 2) if long_cap else 0.0,
        "short_return_pct": round((short_s.iloc[-1] / short_cap - 1) * 100, 2)
        if short_cap
        else 0.0,
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "benchmark_pct": round(bench, 2) if bench is not None else None,
        "long_orders": total_long_orders,
        "short_entries": total_short_signals,
        "short_covers": total_short_covers,
        "halted": halted,
        "start": data.index[start_i].date(),
        "end": data.index[-1].date(),
    }


def _print_row(row: dict) -> None:
    label = f"{row['long_pct']:.0%} long / {row['short_pct']:.0%} short"
    if row.get("wisdom_mode") and row["wisdom_mode"] != "price_only":
        label += f" ({row['wisdom_mode']})"
    print(
        f"  {label:<28} return {row['total_return_pct']:+7.2f}%  "
        f"Sharpe {row['sharpe']:5.2f}  max DD {row['max_drawdown_pct']:6.2f}%  "
        f"equity ${row['final_equity']:,.0f}  "
        f"(long ${row['long_final']:,.0f} + short ${row['short_final']:,.0f})"
    )


def main() -> None:
    from modules.logging_utils import setup_project_logging

    setup_project_logging()
    parser = argparse.ArgumentParser(description="Long fund + SPY short sleeve backtest")
    add_year_range_args(parser)
    parser.add_argument("--short-pct", type=float, default=DEFAULT_SHORT_PCT)
    parser.add_argument(
        "--compare-alloc",
        action="store_true",
        help="Grid: 0/10/15/20%% short vs long-only baseline",
    )
    parser.add_argument(
        "--wisdom",
        choices=("none", "baseline", "wisdom_log", "wisdom_pause"),
        default="none",
        help="Use wisdom sentiment on long book (same as backtester_wisdom.py)",
    )
    parser.add_argument("--gap", type=float, default=0.25)
    parser.add_argument("--capital", type=float, default=10_000.0)
    args = parser.parse_args()

    wisdom_mode = None if args.wisdom == "none" else args.wisdom
    monthly_web = None
    if wisdom_mode:
        monthly_web = load_monthly_web_sentiment()
        if monthly_web.empty:
            print("Missing wayback_sentiment.csv — run simulate_wayback_sentiment.py first.")
            return

    print("=== LONG + SHORT FUND BACKTEST ===")
    print(f"Window:         {args.year_from} -> {args.year_to}")
    print(f"Short rule:     SPY below MA{config.SPY_MA_WINDOW} (cover above MA)")
    print(f"Long sleeves:   SPY / crypto / NYSE (same as backtester.py)")
    print(
        "Allocation:     fixed split at start — long book + short book "
        "(recommended live: 85/15 or 90/10)"
    )
    if wisdom_mode:
        print(f"Long wisdom:    {wisdom_mode} (gap {args.gap})")

    data = _ensure_daily_data(0, refresh=args.refresh, use_max=True)
    data = _slice_data(data, args.year_from, args.year_to)
    print(
        f"Daily bars:     {len(data)} "
        f"({data.index.min().date()} -> {data.index.max().date()})"
    )

    results = []
    grid = ALLOC_GRID if args.compare_alloc else (args.short_pct,)

    for short_pct in grid:
        print(f"\n--- short sleeve {short_pct:.0%} ---")
        row = run_combined_backtest(
            data,
            short_pct=short_pct,
            initial_capital=args.capital,
            wisdom_mode=wisdom_mode,
            monthly_web=monthly_web,
            gap_threshold=args.gap,
        )
        results.append(row)
        _print_row(row)
        print(
            f"    short book: {row['short_return_pct']:+.2f}% on short slice | "
            f"entries {row['short_entries']} covers {row['short_covers']}"
        )

    if args.compare_alloc and wisdom_mode is None:
        print("\n--- Long-only reference (backtester_wisdom run_fund_backtest) ---")
        baseline = run_fund_backtest(
            data,
            pd.Series(dtype=float),
            "baseline",
            gap_threshold=args.gap,
        )
        print(
            f"  100% long (legacy fn)       return {baseline['total_return_pct']:+7.2f}%  "
            f"Sharpe {baseline['sharpe']:5.2f}  max DD {baseline['max_drawdown_pct']:6.2f}%"
        )

    print("\n=== COMPARISON (combined equity) ===")
    header = f"{'Long/Short':<14} {'Return':>9} {'Sharpe':>7} {'MaxDD':>8} {'ShortPnL':>10}"
    print(header)
    print("-" * len(header))
    for row in sorted(results, key=lambda r: -r["total_return_pct"]):
        alloc = f"{row['long_pct']:.0%}/{row['short_pct']:.0%}"
        print(
            f"{alloc:<14} {row['total_return_pct']:+8.2f}% "
            f"{row['sharpe']:7.2f} {row['max_drawdown_pct']:7.2f}% "
            f"{row['short_return_pct']:+9.2f}%"
        )
    bench = results[0].get("benchmark_pct")
    if bench is not None:
        print(f"\n{BENCHMARK} buy & hold (same window): {bench:+.2f}%")

    best = max(results, key=lambda r: r["sharpe"])
    print(
        f"\nBest Sharpe in grid: {best['long_pct']:.0%}/{best['short_pct']:.0%} short "
        f"({best['sharpe']:.2f})"
    )
    print(
        "\nLive allocation note: use two notional buckets on one account — "
        "long fund targets apply only to the long bucket; short sleeve never "
        "dips into long sleeve cash except via combined drawdown halt."
    )

    out_path = "fund_long_short_backtest_results.csv"
    pd.DataFrame(results).to_csv(out_path, index=False)
    print(f"\nSaved -> {out_path}")


if __name__ == "__main__":
    main()
