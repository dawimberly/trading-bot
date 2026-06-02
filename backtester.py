"""Backtest that mirrors run_all.py (regime + crypto pairs + equity MA50).

Default: 365-day simulation on daily bars (fetch if missing).
Live bot still uses 5m data via fetch_data.py without --daily.

Run:  python backtester.py
       python backtester.py --days 180
       python fetch_data.py --daily --days 365
"""

import argparse
import warnings

import numpy as np
import pandas as pd

import config
from fetch_data import fetch_daily_history
from modules import deployment_sizing
from modules.data_loader import load_close_matrix
from modules.market_context import (
    get_market_regime,
    get_price_sentiment,
    get_volatility,
)
from modules.macro_signals import load_daily_matrix, yield_gate_blocks
from modules.pipeline_strategies import (
    COOLDOWN_SECONDS,
    resolve_cycle_deploy,
    run_crypto_strategy,
    run_equity_strategy,
    run_spy_exits,
    run_spy_strategy,
)
from modules.pipeline_strategies import regime_entries_paused
from modules.risk_management import RiskManager, trim_long_sleeves_to_cash_target

warnings.filterwarnings("ignore", category=RuntimeWarning)

MIN_HISTORY = max(50, config.SPY_MA_WINDOW)
TX_COST = 0.001
BENCHMARK = "VTI"
# One daily bar ≈ one pipeline day; ~1h cooldown ≈ 1 session on daily data
DAILY_COOLDOWN_BARS = 1


class BacktestExecutor:
    """Mirrors AlpacaExecutor: equity-based sizing with per-sleeve caps."""

    def __init__(self, portfolio, prices):
        self.portfolio = portfolio
        self.prices = prices
        self.orders = []
        self._cofire_notionals = {}

    def begin_deployment_cycle(self):
        self._cofire_notionals = {}
        self._sizing_data = None

    def set_sizing_context(self, data=None):
        self._sizing_data = data

    def set_cofire_allocations(self, allocations):
        self._cofire_notionals = dict(allocations or {})

    @staticmethod
    def _position_market_value(qty, price):
        if price is None or not np.isfinite(price) or qty <= 0:
            return 0.0
        return abs(float(qty) * float(price))

    def _sleeve_exposure(self, predicate):
        total = 0.0
        for symbol, qty in self.portfolio.positions.items():
            sym = config.normalize_symbol(symbol)
            if predicate(sym):
                price = self.prices.get(symbol)
                total += self._position_market_value(qty, price)
        return total

    @staticmethod
    def _is_crypto_position(symbol):
        return config.is_crypto(symbol)

    @staticmethod
    def _is_spy_position(symbol):
        return config.normalize_symbol(symbol) == config.SPY_BOT_SYMBOL

    @staticmethod
    def _is_metal_position(symbol):
        return config.is_metal_symbol(symbol)

    @staticmethod
    def _is_nyse_sleeve_position(symbol):
        if BacktestExecutor._is_crypto_position(symbol):
            return False
        if BacktestExecutor._is_spy_position(symbol):
            return False
        if BacktestExecutor._is_metal_position(symbol):
            return False
        return True

    def crypto_sleeve_value(self):
        return self._sleeve_exposure(self._is_crypto_position)

    def nyse_sleeve_value(self):
        return self._sleeve_exposure(self._is_nyse_sleeve_position)

    def spy_sleeve_value(self):
        return self._sleeve_exposure(self._is_spy_position)

    def _compute_capped_notional(self, sleeve_cap_pct, sleeve_value, sleeve_key=None):
        equity = self.portfolio.equity(self.prices)
        cash = self.portfolio.cash
        return deployment_sizing.resolve_sleeve_notional(
            equity,
            cash,
            sleeve_cap_pct,
            sleeve_value,
            sleeve_key or "",
            self._cofire_notionals,
        )

    def compute_notional(self):
        equity = self.portfolio.equity(self.prices)
        cash = self.portfolio.cash
        raw = round(equity * config.RISK_PER_TRADE, 2)
        capped = min(raw, config.MAX_NOTIONAL_PER_ORDER, round(cash * 0.95, 2))
        return max(config.MIN_NOTIONAL, capped)

    def compute_crypto_notional(self):
        return self._compute_capped_notional(
            config.effective_sleeve_cap(config.CRYPTO_SLEEVE_CAP_PCT),
            self.crypto_sleeve_value(),
            "crypto",
        )

    def compute_nyse_notional(self):
        return self._compute_capped_notional(
            config.effective_sleeve_cap(config.NYSE_SLEEVE_CAP_PCT),
            self.nyse_sleeve_value(),
            "nyse",
        )

    def compute_spy_notional(self):
        base = self._compute_capped_notional(
            config.effective_sleeve_cap(config.SPY_SLEEVE_CAP_PCT),
            self.spy_sleeve_value(),
            "spy",
        )
        return deployment_sizing.apply_spy_ladder(
            base, getattr(self, "_sizing_data", None)
        )

    def _find_position(self, symbol):
        target = config.normalize_symbol(symbol)
        for sym, qty in self.portfolio.positions.items():
            if config.normalize_symbol(sym) == target:
                return sym, qty
        return None, 0.0

    def execute_full_exit(self, symbol):
        sym, qty = self._find_position(symbol)
        if sym is None or qty <= 0:
            return None
        price = self.prices.get(sym)
        if price is None or not np.isfinite(price) or price <= 0:
            return None
        return self.execute_order(sym, "sell", notional=round(qty * price, 2))

    def execute_order(self, symbol, side, notional=None, reduce_only=False):
        price = self.prices.get(symbol)
        if price is None or not np.isfinite(price) or price <= 0:
            return None
        order = self.portfolio.trade(
            symbol, side.lower(), price, tx_cost=TX_COST, notional=notional
        )
        if order:
            self.orders.append(order)
        return order


class BacktestPortfolio:
    def __init__(self, initial_capital=10000.0):
        self.cash = initial_capital
        self.initial_capital = initial_capital
        self.positions = {}

    def equity(self, prices):
        total = self.cash
        for symbol, qty in self.positions.items():
            p = prices.get(symbol)
            if p is not None and np.isfinite(p):
                total += qty * p
        return total

    def trade(self, symbol, side, price, tx_cost=TX_COST, notional=None):
        if notional is None:
            notional = round(
                min(
                    self.cash * config.RISK_PER_TRADE,
                    config.MAX_NOTIONAL_PER_ORDER,
                    self.cash * 0.95,
                ),
                2,
            )
            notional = max(config.MIN_NOTIONAL, notional)
        if side == "buy":
            if notional < 1 or self.cash < notional:
                return None
            cost = notional * (1 + tx_cost)
            if cost > self.cash:
                return None
            qty = notional / price
            self.cash -= cost
            self.positions[symbol] = self.positions.get(symbol, 0) + qty
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
            return {"symbol": symbol, "side": "sell", "qty": sell_qty, "notional": sell_notional}
        return None


def _benchmark_return(data, start_idx):
    if BENCHMARK not in data.columns:
        return None
    col = data[BENCHMARK].iloc[start_idx:].dropna()
    if len(col) < 2 or col.iloc[0] <= 0:
        return None
    return (col.iloc[-1] / col.iloc[0] - 1) * 100


def _ensure_daily_data(days, refresh=False, use_max=False):
    if use_max:
        if not refresh:
            data = load_close_matrix(interval="1d")
            if len(data) >= MIN_HISTORY + 10:
                return data
        print("--- Downloading max daily history (may take a few minutes) ---")
        fetch_daily_history(use_max=True)
        return load_close_matrix(interval="1d")
    min_rows = max(MIN_HISTORY + 10, int(days * 0.85))
    if not refresh:
        data = load_close_matrix(interval="1d", days=days)
        if len(data) >= min_rows:
            return data
    print(f"--- Downloading {days} days of daily history ---")
    fetch_daily_history(days)
    return load_close_matrix(interval="1d", days=days)


def run_backtest(data, *, track_spy_fill=False, verbose=False):
    """Run fund pipeline on daily data; return performance + optional SPY fill metrics."""
    start_date = data.index[MIN_HISTORY]
    end_date = data.index[-1]
    cooldown_bars = DAILY_COOLDOWN_BARS
    sharpe_scale = np.sqrt(252)
    sim_days = (end_date - start_date).days

    portfolio = BacktestPortfolio()
    pair_cooldown = {}
    risk_manager = RiskManager(max_drawdown_pct=config.MAX_DRAWDOWN_PCT)
    equity_curve = []
    regime_counts = {}
    total_crypto = 0
    total_equity = 0
    total_spy = 0
    total_orders = 0
    pause_days = 0
    halt_liquidations = 0

    spy_cap_pct = config.effective_sleeve_cap(config.SPY_SLEEVE_CAP_PCT)
    macro_daily = None
    if config.game_plan_active() and config.YIELD_GATE_ENABLED:
        try:
            macro_daily = load_daily_matrix(days=max(450, len(data) + 50))
        except Exception:
            macro_daily = None

    spy_fill = {
        "tracking": track_spy_fill,
        "first_signal_cycle": None,
        "spy_buys": 0,
        "cycles_to_90pct": None,
        "trades_to_90pct": None,
        "hours_to_90pct": None,
        "reached_90pct": False,
    }

    for i in range(MIN_HISTORY, len(data)):
        window = data.iloc[: i + 1]
        prices = window.iloc[-1]
        eq = portfolio.equity(prices)
        equity_curve.append(eq)

        prev_halted = risk_manager.halted
        can_trade = risk_manager.check_drawdown(eq)
        if not can_trade:
            if risk_manager.should_liquidate_on_breach():
                halt_liquidations += trim_long_sleeves_to_cash_target(
                    portfolio, prices, config.HALT_TARGET_CASH_PCT, TX_COST
                )
            if verbose and not prev_halted and risk_manager.halted:
                print(
                    f"!!! RISK HALT at {data.index[i].date()} "
                    f"(equity ${round(eq, 2)}, DD {risk_manager.current_drawdown(eq):.1%}) !!!"
                )
            continue
        if verbose and prev_halted and not risk_manager.halted:
            print(
                f"--- RISK RESUME at {data.index[i].date()} "
                f"(equity ${round(eq, 2)}, DD {risk_manager.current_drawdown(eq):.1%}) ---"
            )

        sentiment = get_price_sentiment(window)
        vol = get_volatility(window)
        regime = get_market_regime(sentiment, vol)
        regime_counts[regime] = regime_counts.get(regime, 0) + 1
        if regime_entries_paused(regime, window, sentiment):
            pause_days += 1

        yield_gated = False
        if macro_daily is not None and not macro_daily.empty:
            bar_date = data.index[i]
            macro_window = macro_daily.loc[:bar_date]
            if len(macro_window) >= 50:
                yield_gated = yield_gate_blocks(macro_window)

        executor = BacktestExecutor(portfolio, prices)
        executor.set_sizing_context(window)
        resolve_cycle_deploy(
            window,
            executor,
            regime,
            i,
            pair_cooldown,
            cooldown_bars=cooldown_bars,
            volatility=vol,
            market_open=True,
            yield_gated=yield_gated,
        )
        total_crypto += run_crypto_strategy(
            window,
            executor,
            regime,
            i,
            pair_cooldown,
            cooldown_bars=cooldown_bars,
            volatility=vol,
        )
        total_spy += run_spy_exits(window, executor, regime)
        total_spy += run_spy_strategy(
            window,
            executor,
            regime,
            i,
            pair_cooldown,
            cooldown_bars=cooldown_bars,
            yield_gated=yield_gated,
        )
        total_equity += run_equity_strategy(
            window,
            executor,
            regime,
            i,
            pair_cooldown,
            cooldown_bars=cooldown_bars,
            yield_gated=yield_gated,
        )
        total_orders += len(executor.orders)

        if track_spy_fill:
            spy_val = executor.spy_sleeve_value()
            spy_cap = eq * spy_cap_pct
            target_90 = 0.9 * spy_cap
            from modules.pipeline_strategies import _spy_buy_intent

            wants_spy = _spy_buy_intent(
                window, regime, i, pair_cooldown, cooldown_bars=cooldown_bars
            )
            if spy_fill["first_signal_cycle"] is None and wants_spy:
                spy_fill["first_signal_cycle"] = i - MIN_HISTORY
            for order in executor.orders:
                if (
                    order.get("side") == "buy"
                    and config.normalize_symbol(order.get("symbol", ""))
                    == config.SPY_BOT_SYMBOL
                ):
                    spy_fill["spy_buys"] += 1
            if not spy_fill["reached_90pct"] and spy_val >= target_90 - config.MIN_NOTIONAL:
                spy_fill["reached_90pct"] = True
                base = spy_fill["first_signal_cycle"] or 0
                spy_fill["cycles_to_90pct"] = i - MIN_HISTORY - base
                spy_fill["trades_to_90pct"] = spy_fill["spy_buys"]
                spy_fill["hours_to_90pct"] = round(
                    spy_fill["cycles_to_90pct"] * (COOLDOWN_SECONDS / 3600), 1
                )

    curve = pd.Series(equity_curve)
    returns = curve.pct_change().dropna()
    total_ret = (curve.iloc[-1] / portfolio.initial_capital - 1) * 100
    sharpe = (
        (returns.mean() / returns.std()) * sharpe_scale if returns.std() != 0 else 0
    )
    max_dd = ((curve / curve.cummax()) - 1).min() * 100
    bench = _benchmark_return(data, MIN_HISTORY)

    return {
        "start_date": str(start_date.date()),
        "end_date": str(end_date.date()),
        "sim_days": sim_days,
        "final_equity": round(curve.iloc[-1], 2),
        "total_return_pct": round(total_ret, 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "benchmark_return_pct": round(bench, 2) if bench is not None else None,
        "spy_signals": total_spy,
        "crypto_signals": total_crypto,
        "nyse_signals": total_equity,
        "total_orders": total_orders,
        "regime_counts": regime_counts,
        "spy_fill": spy_fill,
        "halt_events": risk_manager.halt_events,
        "resume_events": risk_manager.resume_events,
        "pause_days": pause_days,
        "halt_liquidations": halt_liquidations,
    }


def run_performance_test(days=None, refresh=False, use_max=False):
    if use_max:
        print("--- STARTING FUND BACKTEST (max available daily history) ---")
    else:
        days = days or config.BACKTEST_DAYS
        print(f"--- STARTING FUND BACKTEST ({days} days) ---")
    try:
        data = _ensure_daily_data(days or 0, refresh=refresh, use_max=use_max)
    except Exception as e:
        print("Database error: " + str(e))
        return
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} rows; got {len(data)}.")
        print("Run: python fetch_data.py --daily --max")
        return

    start_date = data.index[MIN_HISTORY]
    end_date = data.index[-1]
    cooldown_bars = DAILY_COOLDOWN_BARS
    bar_label = "daily bars"
    sim_days = (end_date - start_date).days

    print(f"Loaded {len(data.columns)} tickers over {len(data)} {bar_label}.")
    print(f"Simulation: {start_date.date()} to {end_date.date()}")
    print(f"Cooldown: {cooldown_bars} bar(s) (~{COOLDOWN_SECONDS // 60} min live logic)")
    config.print_recommended_stack_flags()

    result = run_backtest(data, track_spy_fill=False, verbose=True)
    curve_end = result["final_equity"]
    total_ret = result["total_return_pct"]
    sharpe = result["sharpe"]
    max_dd = result["max_drawdown_pct"]
    bench = result["benchmark_return_pct"]
    total_crypto = result["crypto_signals"]
    total_equity = result["nyse_signals"]
    total_spy = result["spy_signals"]
    total_orders = result["total_orders"]
    regime_counts = result["regime_counts"]

    print("--- FUND BACKTEST REPORT (SPY + vol-gated crypto + NYSE) ---")
    print(
        f"Simulation:       {start_date.date()} to {end_date.date()} "
        f"(~{sim_days} days, {len(data)} {bar_label})"
    )
    alloc = config.fund_allocation_pct()
    print(
        f"Sleeves:          SPY {alloc['spy']:.0%} | "
        f"crypto {alloc['crypto']:.0%} | "
        f"NYSE {alloc['nyse']:.0%} | "
        f"metal {alloc['metal']:.0%} | "
        f"cash {alloc['cash_buffer']:.0%}"
    )
    print(f"Crypto vol-only:  {config.CRYPTO_VOL_ONLY}")
    print(f"Final Equity:     ${curve_end}")
    print(f"Total Return:     {round(total_ret, 2)}%")
    if bench is not None:
        print(f"VTI Buy & Hold:   {round(bench, 2)}%")
    print(f"Sharpe Ratio:     {round(sharpe, 2)}")
    print(f"Max Drawdown:     {round(max_dd, 2)}%")
    print(f"SPY signals:      {total_spy}")
    print(f"Crypto signals:   {total_crypto}")
    print(f"NYSE signals:     {total_equity}")
    print(f"Total orders:     {total_orders}")
    print("Regime distribution:")
    for name, count in sorted(regime_counts.items(), key=lambda x: -x[1]):
        print(f"  {name}: {count}")
    print("---------------------------------------------------")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest run_all.py pipeline")
    parser.add_argument(
        "--days",
        type=int,
        default=config.BACKTEST_DAYS,
        help=f"Simulation length in calendar days (default: {config.BACKTEST_DAYS})",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-download daily history before running",
    )
    parser.add_argument(
        "--max",
        action="store_true",
        help="Use maximum available daily history (full universe, yfinance max)",
    )
    args = parser.parse_args()
    run_performance_test(days=args.days, refresh=args.refresh, use_max=args.max)
