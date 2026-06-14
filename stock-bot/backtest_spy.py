"""Backtest for run_spy.py (SPY above MA, MA-break exit, stop-loss, regime gate).

Run:  python backtest_spy.py
       python backtest_spy.py --compare
       python backtest_spy.py --all
       python backtest_spy.py --ma 50 --allocation 0.25
       python backtest_spy.py --days 180 --refresh
"""

import argparse
import warnings

import numpy as np
import pandas as pd

import config
from fetch_data import fetch_daily_history
from modules.data_loader import load_close_matrix
from modules.market_context import (
    get_market_regime,
    get_price_sentiment,
    get_volatility,
)
from modules.pipeline_strategies import (
    COOLDOWN_SECONDS,
    run_spy_exits,
    run_spy_strategy,
)
from modules.risk_management import RiskManager

warnings.filterwarnings("ignore", category=RuntimeWarning)

TX_COST = 0.001
BENCHMARK = config.SPY_BOT_SYMBOL
DAILY_COOLDOWN_BARS = 1


class BacktestExecutor:
    def __init__(self, portfolio, prices):
        self.portfolio = portfolio
        self.prices = prices
        self.orders = []

    def execute_order(self, symbol, side, notional=None, reduce_only=False):
        price = self.prices.get(symbol)
        if price is None or not np.isfinite(price) or price <= 0:
            return None
        if reduce_only and side.lower() == "sell":
            order = self.portfolio.trade_full_exit(symbol, price, tx_cost=TX_COST)
        else:
            order = self.portfolio.trade(
                symbol, side.lower(), price, tx_cost=TX_COST, notional=notional
            )
        if order:
            self.orders.append(order)
        return order

    @property
    def client(self):
        return self.portfolio


class BacktestPortfolio:
    def __init__(self, initial_capital=10000.0, risk_pct=None):
        self.cash = initial_capital
        self.initial_capital = initial_capital
        self.positions = {}
        self.entry_prices = {}
        self.risk_pct = risk_pct if risk_pct is not None else config.SPY_RISK_PER_TRADE
        self._last_prices = {}

    def equity(self, prices):
        total = self.cash
        for symbol, qty in self.positions.items():
            p = prices.get(symbol)
            if p is not None and np.isfinite(p):
                total += qty * p
        return total

    def get_all_positions(self):
        positions = []
        for symbol, qty in self.positions.items():
            if qty <= 0:
                continue
            entry = self.entry_prices.get(symbol, 0)
            current = self._last_prices.get(symbol, entry)
            positions.append(
                type(
                    "Pos",
                    (),
                    {
                        "symbol": symbol,
                        "qty": qty,
                        "avg_entry_price": entry,
                        "current_price": current,
                    },
                )()
            )
        return positions

    def set_prices(self, prices):
        self._last_prices = dict(prices)

    def _size_notional(self, notional=None):
        if notional is not None:
            return max(config.MIN_NOTIONAL, round(notional, 2))
        eq = self.equity(self._last_prices)
        raw = round(eq * self.risk_pct, 2)
        return max(
            config.MIN_NOTIONAL,
            min(raw, config.MAX_NOTIONAL_PER_ORDER, round(self.cash * 0.95, 2)),
        )

    def trade(self, symbol, side, price, tx_cost=TX_COST, notional=None):
        notional = self._size_notional(notional)
        if side == "buy":
            if notional < 1 or self.cash < notional:
                return None
            cost = notional * (1 + tx_cost)
            if cost > self.cash:
                return None
            qty = notional / price
            prev_qty = self.positions.get(symbol, 0)
            prev_entry = self.entry_prices.get(symbol, price)
            new_qty = prev_qty + qty
            self.entry_prices[symbol] = (
                (prev_qty * prev_entry + qty * price) / new_qty if new_qty else price
            )
            self.cash -= cost
            self.positions[symbol] = new_qty
            return {"symbol": symbol, "side": "buy", "qty": qty, "notional": notional}
        if side == "sell":
            qty = self.positions.get(symbol, 0)
            if qty <= 0:
                return None
            sell_notional = min(notional, qty * price)
            sell_qty = sell_notional / price
            proceeds = sell_notional * (1 - tx_cost)
            self.cash += proceeds
            self.positions[symbol] = qty - sell_qty
            if self.positions[symbol] < 1e-9:
                del self.positions[symbol]
                self.entry_prices.pop(symbol, None)
            return {"symbol": symbol, "side": "sell", "qty": sell_qty, "notional": sell_notional}
        return None

    def trade_full_exit(self, symbol, price, tx_cost=TX_COST):
        qty = self.positions.get(symbol, 0)
        if qty <= 0:
            return None
        sell_notional = qty * price
        return self.trade(symbol, "sell", price, tx_cost=tx_cost, notional=sell_notional)

    def run_stop_losses(self, prices, executor):
        exits = 0
        for symbol in list(self.positions.keys()):
            qty = self.positions.get(symbol, 0)
            entry = self.entry_prices.get(symbol, 0)
            current = prices.get(symbol)
            if qty <= 0 or entry <= 0 or current is None or current <= 0:
                continue
            pnl_pct = (current - entry) / entry
            if pnl_pct > -config.STOP_LOSS_PCT:
                continue
            if executor.execute_order(symbol, "sell", reduce_only=True):
                exits += 1
        return exits


def _benchmark_return(data, start_idx):
    if BENCHMARK not in data.columns:
        return None
    col = data[BENCHMARK].iloc[start_idx:].dropna()
    if len(col) < 2 or col.iloc[0] <= 0:
        return None
    return (col.iloc[-1] / col.iloc[0] - 1) * 100


def _ensure_daily_data(days, refresh=False, min_history=50):
    min_rows = max(min_history + 10, int(days * 0.85))
    if not refresh:
        data = load_close_matrix(interval="1d", days=days)
        if len(data) >= min_rows and config.SPY_BOT_SYMBOL in data.columns:
            return data
    print(f"--- Downloading {days} days of daily history ---")
    fetch_daily_history(days)
    return load_close_matrix(interval="1d", days=days)


def simulate_spy_strategy(
    data,
    *,
    ma_window,
    risk_pct,
    exit_on_ma_break,
    days,
    verbose=False,
    start_idx=None,
):
    min_history = max(50, ma_window)
    start_idx = start_idx if start_idx is not None else min_history
    if len(data) < start_idx + 2:
        return None

    portfolio = BacktestPortfolio(risk_pct=risk_pct)
    pair_cooldown = {}
    risk_manager = RiskManager(max_drawdown_pct=config.MAX_DRAWDOWN_PCT)
    equity_curve = []
    total_buys = 0
    total_ma_exits = 0
    total_stop_exits = 0
    halted = False

    for i in range(start_idx, len(data)):
        window = data.iloc[: i + 1]
        prices = window.iloc[-1]
        portfolio.set_prices(prices)
        eq = portfolio.equity(prices)
        equity_curve.append(eq)

        if halted or not risk_manager.check_drawdown(eq):
            halted = True
            continue

        sentiment = get_price_sentiment(window)
        vol = get_volatility(window)
        regime = get_market_regime(sentiment, vol)
        executor = BacktestExecutor(portfolio, prices)

        total_stop_exits += portfolio.run_stop_losses(prices, executor)
        if exit_on_ma_break:
            total_ma_exits += run_spy_exits(
                window,
                executor,
                regime,
                ma_window=ma_window,
            )
        total_buys += run_spy_strategy(
            window,
            executor,
            regime,
            i,
            pair_cooldown,
            ma_window=ma_window,
            cooldown_bars=DAILY_COOLDOWN_BARS,
        )

        if verbose and i % 50 == 0:
            holding = config.SPY_BOT_SYMBOL in portfolio.positions
            print(
                f"  {data.index[i].date()} | equity ${round(eq, 2)} | "
                f"holding={holding} | MA{ma_window}"
            )

    curve = pd.Series(equity_curve)
    returns = curve.pct_change().dropna()
    total_ret = (curve.iloc[-1] / portfolio.initial_capital - 1) * 100
    sharpe = (
        (returns.mean() / returns.std()) * np.sqrt(252) if returns.std() != 0 else 0
    )
    max_dd = ((curve / curve.cummax()) - 1).min() * 100
    bench = _benchmark_return(data, start_idx)

    return {
        "ma_window": ma_window,
        "allocation": risk_pct,
        "exit_on_ma": exit_on_ma_break,
        "final_equity": round(curve.iloc[-1], 2),
        "total_return_pct": round(total_ret, 2),
        "benchmark_pct": round(bench, 2) if bench is not None else None,
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "buys": total_buys,
        "ma_exits": total_ma_exits,
        "stop_exits": total_stop_exits,
        "start": data.index[start_idx].date(),
        "end": data.index[-1].date(),
    }


def _print_report(result, days):
    print("--- SPY BOT BACKTEST REPORT ---")
    print(f"Period:           {days} days ({result['start']} to {result['end']})")
    print(f"MA window:        {result['ma_window']}")
    print(f"Allocation:       {result['allocation']:.0%} of equity per entry")
    print(f"Exit on MA break: {result['exit_on_ma']}")
    print(f"Final Equity:     ${result['final_equity']}")
    print(f"Total Return:     {result['total_return_pct']}%")
    if result["benchmark_pct"] is not None:
        print(f"SPY Buy & Hold:   {result['benchmark_pct']}%")
    print(f"Sharpe Ratio:     {result['sharpe']}")
    print(f"Max Drawdown:     {result['max_drawdown_pct']}%")
    print(f"SPY entries:      {result['buys']}")
    print(f"MA exits:         {result['ma_exits']}")
    print(f"Stop-loss exits:  {result['stop_exits']}")
    print("-------------------------------")


def run_spy_backtest(
    days=None,
    refresh=False,
    ma_window=None,
    allocation=None,
    exit_on_ma_break=None,
    verbose=False,
):
    days = days or config.BACKTEST_DAYS
    ma_window = ma_window or config.SPY_MA_WINDOW
    allocation = allocation if allocation is not None else config.SPY_RISK_PER_TRADE
    exit_on_ma_break = (
        config.SPY_EXIT_ON_MA_BREAK if exit_on_ma_break is None else exit_on_ma_break
    )
    min_history = max(50, ma_window)

    print(f"--- STARTING SPY BOT BACKTEST ({days} days) ---")
    try:
        data = _ensure_daily_data(days, refresh=refresh, min_history=min_history)
    except Exception as e:
        print("Database error: " + str(e))
        return None
    if config.SPY_BOT_SYMBOL not in data.columns:
        print(f"{config.SPY_BOT_SYMBOL} not found in daily data.")
        return None

    print(f"Loaded {len(data.columns)} tickers over {len(data)} daily bars.")
    print(
        f"Signal: {config.SPY_BOT_SYMBOL} > MA{ma_window} | "
        f"Allocation: {allocation:.0%} | "
        f"Exit below MA: {exit_on_ma_break} | "
        f"Cooldown: {DAILY_COOLDOWN_BARS} bar(s) (~{COOLDOWN_SECONDS // 60} min live)"
    )

    result = simulate_spy_strategy(
        data,
        ma_window=ma_window,
        risk_pct=allocation,
        exit_on_ma_break=exit_on_ma_break,
        days=days,
        verbose=verbose,
    )
    if result is None:
        print(f"Need at least {min_history} rows; got {len(data)}.")
        return None
    _print_report(result, days)
    return result


def run_compare_backtest(days=None, refresh=False, allocation=None):
    days = days or config.BACKTEST_DAYS
    allocation = allocation if allocation is not None else config.SPY_RISK_PER_TRADE
    windows = config.SPY_MA_WINDOWS
    min_history = max(windows)

    print(f"--- SPY MA WINDOW COMPARISON ({days} days, {allocation:.0%} allocation) ---")
    try:
        data = _ensure_daily_data(days, refresh=refresh, min_history=min_history)
    except Exception as e:
        print("Database error: " + str(e))
        return

    rows = []
    start_idx = max(windows)
    bench = _benchmark_return(data, start_idx)
    for ma_window in windows:
        print(f"Simulating MA{ma_window}...")
        result = simulate_spy_strategy(
            data,
            ma_window=ma_window,
            risk_pct=allocation,
            exit_on_ma_break=True,
            days=days,
            verbose=False,
            start_idx=start_idx,
        )
        if result:
            rows.append(result)

    if not rows:
        print("No results.")
        return

    df = pd.DataFrame(rows)
    df = df[
        [
            "ma_window",
            "total_return_pct",
            "benchmark_pct",
            "sharpe",
            "max_drawdown_pct",
            "buys",
            "ma_exits",
            "stop_exits",
        ]
    ]
    df.columns = [
        "MA",
        "Return %",
        "SPY B&H %",
        "Sharpe",
        "Max DD %",
        "Buys",
        "MA Exits",
        "Stop Exits",
    ]
    print("\n--- COMPARISON TABLE ---")
    print(f"(All strategies start {data.index[start_idx].date()})")
    print(df.to_string(index=False))
    if bench is not None:
        print(f"\nSPY buy-and-hold over same window: {round(bench, 2)}%")
    best = max(rows, key=lambda r: r["sharpe"])
    print(
        f"\nBest Sharpe: MA{best['ma_window']} "
        f"({best['total_return_pct']}% return, {best['max_drawdown_pct']}% max DD)"
    )
    print("------------------------")


def run_all_iterations(days=None, refresh=False):
    """Run every MA x allocation x exit-mode combination and rank results."""
    days = days or config.BACKTEST_DAYS
    windows = config.SPY_MA_WINDOWS
    allocations = config.SPY_ALLOCATIONS
    exit_modes = [True, False]
    start_idx = max(windows)
    total = len(windows) * len(allocations) * len(exit_modes)

    print(f"--- SPY FULL GRID SEARCH ({total} iterations, {days} days) ---")
    print(f"MA windows:   {windows}")
    print(f"Allocations:  {[f'{a:.0%}' for a in allocations]}")
    print(f"MA exit:      on + off")
    try:
        data = _ensure_daily_data(days, refresh=refresh, min_history=start_idx)
    except Exception as e:
        print("Database error: " + str(e))
        return

    if config.SPY_BOT_SYMBOL not in data.columns:
        print(f"{config.SPY_BOT_SYMBOL} not found in daily data.")
        return

    bench = _benchmark_return(data, start_idx)
    start_date = data.index[start_idx].date()
    end_date = data.index[-1].date()
    print(f"Period: {start_date} to {end_date} ({len(data)} bars loaded)")
    if bench is not None:
        print(f"SPY buy-and-hold benchmark: {round(bench, 2)}%")
    print()

    rows = []
    n = 0
    for ma_window in windows:
        for allocation in allocations:
            for exit_on_ma in exit_modes:
                n += 1
                exit_label = "exit" if exit_on_ma else "hold"
                print(
                    f"[{n}/{total}] MA{ma_window} | {allocation:.0%} alloc | "
                    f"MA {exit_label}...",
                    end=" ",
                    flush=True,
                )
                result = simulate_spy_strategy(
                    data,
                    ma_window=ma_window,
                    risk_pct=allocation,
                    exit_on_ma_break=exit_on_ma,
                    days=days,
                    start_idx=start_idx,
                )
                if result:
                    rows.append(result)
                    print(
                        f"return {result['total_return_pct']}% | "
                        f"sharpe {result['sharpe']} | "
                        f"DD {result['max_drawdown_pct']}%"
                    )
                else:
                    print("skipped")

    if not rows:
        print("No results.")
        return

    df = pd.DataFrame(rows)
    df["alloc_pct"] = (df["allocation"] * 100).round(0).astype(int)
    df["ma_exit"] = df["exit_on_ma"].map({True: "yes", False: "no"})
    df = df.sort_values(["sharpe", "total_return_pct"], ascending=False)

    out_cols = [
        "ma_window",
        "alloc_pct",
        "ma_exit",
        "total_return_pct",
        "benchmark_pct",
        "sharpe",
        "max_drawdown_pct",
        "final_equity",
        "buys",
        "ma_exits",
        "stop_exits",
    ]
    display = df[out_cols].copy()
    display.columns = [
        "MA",
        "Alloc %",
        "MA Exit",
        "Return %",
        "SPY B&H %",
        "Sharpe",
        "Max DD %",
        "Final $",
        "Buys",
        "MA Exits",
        "Stop Exits",
    ]

    csv_path = config.SPY_BACKTEST_RESULTS
    df[out_cols].to_csv(csv_path, index=False)
    print(f"\nSaved {len(df)} results to {csv_path}")

    print("\n=== TOP 10 BY SHARPE ===")
    print(display.head(10).to_string(index=False))

    print("\n=== TOP 10 BY RETURN ===")
    by_return = display.sort_values("Return %", ascending=False)
    print(by_return.head(10).to_string(index=False))

    print("\n=== TOP 10 BY LOWEST DRAWDOWN ===")
    by_dd = display.sort_values("Max DD %", ascending=False)
    print(by_dd.head(10).to_string(index=False))

    best_sharpe = df.iloc[0]
    best_return = df.sort_values("total_return_pct", ascending=False).iloc[0]
    print("\n=== SUMMARY ===")
    print(
        f"Best Sharpe:  MA{best_sharpe['ma_window']} | "
        f"{best_sharpe['allocation']:.0%} alloc | "
        f"MA exit={'yes' if best_sharpe['exit_on_ma'] else 'no'} | "
        f"{best_sharpe['total_return_pct']}% return | "
        f"Sharpe {best_sharpe['sharpe']} | "
        f"DD {best_sharpe['max_drawdown_pct']}%"
    )
    print(
        f"Best Return:  MA{best_return['ma_window']} | "
        f"{best_return['allocation']:.0%} alloc | "
        f"MA exit={'yes' if best_return['exit_on_ma'] else 'no'} | "
        f"{best_return['total_return_pct']}% return | "
        f"Sharpe {best_return['sharpe']} | "
        f"DD {best_return['max_drawdown_pct']}%"
    )
    if bench is not None:
        print(f"SPY buy-and-hold: {round(bench, 2)}%")
    print("=================")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest run_spy.py SPY strategy")
    parser.add_argument(
        "--days",
        type=int,
        default=config.BACKTEST_DAYS,
        help=f"Simulation length in calendar days (default: {config.BACKTEST_DAYS})",
    )
    parser.add_argument(
        "--ma",
        type=int,
        default=None,
        help=f"Moving average window (default: {config.SPY_MA_WINDOW})",
    )
    parser.add_argument(
        "--allocation",
        type=float,
        default=None,
        help=f"Fraction of equity per entry (default: {config.SPY_RISK_PER_TRADE})",
    )
    parser.add_argument(
        "--no-ma-exit",
        action="store_true",
        help="Disable selling when price drops below the MA",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help=f"Compare MA windows {config.SPY_MA_WINDOWS}",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run all MA x allocation x MA-exit-on/off combinations",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-download daily history before running",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print progress every 50 bars",
    )
    args = parser.parse_args()

    if args.all:
        run_all_iterations(days=args.days, refresh=args.refresh)
    elif args.compare:
        run_compare_backtest(
            days=args.days,
            refresh=args.refresh,
            allocation=args.allocation,
        )
    else:
        run_spy_backtest(
            days=args.days,
            refresh=args.refresh,
            ma_window=args.ma,
            allocation=args.allocation,
            exit_on_ma_break=not args.no_ma_exit,
            verbose=args.verbose,
        )
