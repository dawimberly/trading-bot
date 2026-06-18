"""Backtest that mirrors run_all.py (regime + crypto pairs + equity MA50).

Default: 365-day simulation on daily bars (fetch if missing).
Live bot still uses 5m data via fetch_data.py without --daily.

Satellite research scripts (shared helpers in modules/backtest_common.py):
  backtester_wisdom.py      — wisdom / governor modes
  backtester_macro_hedge.py — bond/yield stress overlays
  backtester_metals.py      — metal sleeve variants
  backtester_long_short.py  — short-sleeve grid

Run:  python backtester.py
       python backtester.py --days 180
       python backtester.py --days 365 --paper-aggressive --compare-final
       python backtester.py --days 365 --paper-aggressive --strict-pit --compare-pit
       python backtester.py --days 365 --paper-aggressive --strict-pit --compare-blended-conservative
       python backtester.py --days 365 --paper-aggressive --fast-mode
       python backtester.py --days 365 --vti-core 0.80 --no-thinking
       python fetch_data.py --daily --days 365

Core engine: modules/backtester_core.py (data cache, metrics, slippage, walk-forward).
"""

import argparse
import os
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

import config
from fetch_data import fetch_daily_history, fetch_daily_history_for_tickers
from modules import deployment_sizing
from modules.data_loader import load_close_matrix
from modules.market_context import (
    cross_asset_vol_score,
    get_market_regime,
    get_price_sentiment,
    get_volatility,
)
from modules.macro_signals import load_daily_matrix, macro_stress, yield_gate_blocks
from modules.pipeline_strategies import (
    COOLDOWN_SECONDS,
    resolve_cycle_deploy,
    run_crypto_strategy,
    run_equity_strategy,
    run_equity_pairs_strategy,
    run_bond_strategy,
    run_international_strategy,
    run_ipo_safety_trims,
    run_spy_exits,
    run_spy_strategy,
)
from modules.pipeline_strategies import regime_entries_paused
from modules.console_output import print_table
from modules.backtester_core import (
    DEFAULT_EXPORT_CSV,
    DEFAULT_EXPORT_JSON,
    DEFAULT_HTML_REPORT,
    DEFAULT_CRYPTO_SLIPPAGE_BPS,
    DEFAULT_EQUITY_SLIPPAGE_BPS,
    FAST_MODE_MAX_TICKERS,
    LAST_BACKTEST_RESULT,
    ROLLING_METRIC_WINDOW,
    RUN_OPTIONS,
    apply_fast_mode_data,
    apply_run_options_to_config,
    apply_default_execution_costs,
    compute_performance_metrics,
    effective_execution,
    ensure_daily_data_cached,
    export_results_csv,
    export_results_json,
    format_enhanced_final_table,
    format_slippage_table,
    format_walk_forward_table,
    generate_html_report,
    get_indicator_cache,
    parallel_map_backtests,
    prepare_indicator_cache,
    release_backtest_memory,
    reset_caches,
    run_slippage_sensitivity,
    store_last_result,
    walk_forward_purged,
    walk_forward_summary,
)
from modules.risk_management import RiskManager, trim_long_sleeves_to_cash_target

warnings.filterwarnings("ignore", category=RuntimeWarning)

MIN_HISTORY = max(50, config.SPY_MA_WINDOW)
WARMUP_CALENDAR_BUFFER = 45  # calendar days before period_start for indicator warmup
TX_COST = 0.001  # legacy fallback when ALPACA_CRYPTO_FEE_AWARE=false
BENCHMARK = "VTI"
VTI_CORE_SYMBOL = "VTI"


def _tx_cost_for_symbol(symbol: str) -> float:
    """Alpaca: $0 equity commissions; crypto taker fee per leg when fee-aware."""
    if config.is_crypto(symbol):
        if config.ALPACA_CRYPTO_FEE_AWARE:
            return config.ALPACA_CRYPTO_TAKER_FEE_PCT
        return TX_COST
    return 0.0
# One daily bar ≈ one pipeline day; ~1h cooldown ≈ 1 session on daily data
DAILY_COOLDOWN_BARS = 1


class BacktestExecutor:
    """Mirrors AlpacaExecutor: equity-based sizing with per-sleeve caps."""

    def __init__(
        self,
        portfolio,
        prices,
        *,
        active_fraction: float = 1.0,
        cap_scale: float | None = None,
    ):
        self.portfolio = portfolio
        self.prices = prices
        self.orders = []
        self._cofire_notionals = {}
        self._active_fraction = max(0.0, min(1.0, float(active_fraction)))
        self._cap_scale = (
            max(0.0, float(cap_scale))
            if cap_scale is not None
            else self._active_fraction
        )
        self._regime_spy_scale = 1.0
        self._regime_nyse_scale = 1.0

    def set_regime_sleeve_scales(self, *, spy_scale: float = 1.0, nyse_scale: float = 1.0) -> None:
        self._regime_spy_scale = float(spy_scale)
        self._regime_nyse_scale = float(nyse_scale)

    def set_thinking_sleeve_scales(
        self,
        *,
        spy_scale: float = 1.0,
        nyse_scale: float = 1.0,
        crypto_scale: float = 1.0,
    ) -> None:
        self._thinking_spy_scale = float(spy_scale)
        self._thinking_nyse_scale = float(nyse_scale)
        self._thinking_crypto_scale = float(crypto_scale)

    def set_pod_risk_scales(self, scales: dict[str, float] | None) -> None:
        self._pod_risk_scales = dict(scales) if scales else {}

    def pod_risk_scale(self, pod: str) -> float:
        return float(getattr(self, "_pod_risk_scales", {}).get(pod, 1.0))

    def _get_account(self):
        """Alpaca-compatible shim for pipeline_strategies (live uses real account)."""
        equity = self.portfolio.equity(self.prices)

        class _Acct:
            pass

        acct = _Acct()
        acct.equity = equity
        acct.cash = self.portfolio.cash
        return acct

    def get_order_params(self, symbol):
        """Return order parameters for symbol handling in stat arb and crypto flows."""
        is_crypto_sym = config.is_crypto(symbol)
        formatted_symbol = symbol.replace("-", "/") if is_crypto_sym else symbol
        tif = "GTC" if is_crypto_sym else "DAY"
        return formatted_symbol, tif, is_crypto_sym

    def begin_deployment_cycle(self):
        self._cofire_notionals = {}
        self._sizing_data = None
        self._paper_feature_flags = config.get_paper_feature_flags()

    def set_sizing_context(self, data=None):
        self._sizing_data = data

    def set_wisdom_sizing_multiplier(self, multiplier: float = 1.0) -> None:
        self._wisdom_sizing_multiplier = float(multiplier)

    def register_pair_symbols(self, long_sym: str, short_sym: str) -> None:
        symbols = getattr(self, "_pair_symbols", None)
        if symbols is None:
            symbols = set()
            self._pair_symbols = symbols
        symbols.add(long_sym)
        symbols.add(short_sym)

    def pair_sleeve_value(self) -> float:
        total = 0.0
        for sym in getattr(self, "_pair_symbols", ()) or ():
            qty = self.portfolio.positions.get(sym, 0)
            price = self.prices.get(sym)
            if price is not None and np.isfinite(price):
                total += float(qty) * float(price)
        return total

    def _apply_wisdom_multiplier(self, notional: float | None) -> float | None:
        if notional is None:
            return None
        mult = getattr(self, "_wisdom_sizing_multiplier", 1.0)
        if mult >= 0.999:
            return notional
        scaled = round(notional * mult, 2)
        if scaled < config.MIN_NOTIONAL:
            return None
        return scaled

    def set_cofire_allocations(self, allocations):
        if not config.effective_cofire_budget_enabled():
            self._cofire_notionals = {}
            return
        self._cofire_notionals = dict(allocations or {})

    def paper_feature_flags(self) -> dict[str, bool]:
        return dict(getattr(self, "_paper_feature_flags", None) or config.get_paper_feature_flags())

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
        if config.normalize_symbol(symbol) == VTI_CORE_SYMBOL:
            return False
        if config.is_international_adr(symbol):
            return False
        if config.is_bond_symbol(symbol):
            return False
        return True

    @staticmethod
    def _is_bond_sleeve_position(symbol):
        return config.is_bond_symbol(symbol)

    @staticmethod
    def _is_international_sleeve_position(symbol):
        return config.is_international_adr(symbol)

    def crypto_sleeve_value(self):
        return self._sleeve_exposure(self._is_crypto_position)

    def nyse_sleeve_value(self):
        return self._sleeve_exposure(self._is_nyse_sleeve_position)

    def international_sleeve_value(self):
        return self._sleeve_exposure(self._is_international_sleeve_position)

    def bond_sleeve_value(self):
        return self._sleeve_exposure(self._is_bond_sleeve_position)

    def spy_sleeve_value(self):
        return self._sleeve_exposure(self._is_spy_position)

    def _scaled_cap_pct(self, sleeve_cap_pct: float, *, sleeve: str | None = None) -> float:
        scale = self._cap_scale
        if sleeve == "spy":
            scale *= getattr(self, "_regime_spy_scale", 1.0)
            scale *= getattr(self, "_thinking_spy_scale", 1.0)
        elif sleeve == "nyse":
            scale *= getattr(self, "_regime_nyse_scale", 1.0)
            scale *= getattr(self, "_thinking_nyse_scale", 1.0)
        elif sleeve == "crypto":
            scale *= getattr(self, "_thinking_crypto_scale", 1.0)
            scale *= self.pod_risk_scale("crypto")
        if sleeve == "spy":
            scale *= self.pod_risk_scale("spy")
        elif sleeve == "nyse":
            scale *= self.pod_risk_scale("nyse")
        return round(sleeve_cap_pct * scale, 6)

    def _compute_capped_notional_raw(self, sleeve_cap_pct, sleeve_value, sleeve_key=None):
        equity = self.portfolio.equity(self.prices)
        cash = self.portfolio.cash
        return deployment_sizing.resolve_sleeve_notional(
            equity,
            cash,
            self._scaled_cap_pct(sleeve_cap_pct),
            sleeve_value,
            sleeve_key or "",
            self._cofire_notionals,
        )

    def _compute_capped_notional(self, sleeve_cap_pct, sleeve_value, sleeve_key=None):
        return self._apply_wisdom_multiplier(
            self._compute_capped_notional_raw(sleeve_cap_pct, sleeve_value, sleeve_key)
        )

    def _risk_per_trade(self, equity: float) -> float:
        if config.backtest_small_account_context() or config.paper_aggressive_context():
            return config.effective_risk_per_trade(equity)
        return config.RISK_PER_TRADE

    def compute_notional(self):
        equity = self.portfolio.equity(self.prices)
        cash = self.portfolio.cash
        risk = self._risk_per_trade(equity)
        if config.backtest_small_account_context():
            max_order = config.effective_max_notional_per_order(equity)
            min_n = config.effective_min_notional(equity)
        else:
            max_order = config.MAX_NOTIONAL_PER_ORDER
            min_n = config.MIN_NOTIONAL
        raw = round(equity * risk, 2)
        capped = min(raw, max_order, round(cash * 0.95, 2))
        if capped < min_n:
            return None
        return self._apply_wisdom_multiplier(capped)

    def compute_crypto_notional(self):
        return self._compute_capped_notional(
            config.CRYPTO_SLEEVE_CAP_PCT,
            self.crypto_sleeve_value(),
            "crypto",
        )

    def compute_nyse_notional(self):
        equity = self.portfolio.equity(self.prices)
        cash = self.portfolio.cash
        return self._apply_wisdom_multiplier(
            deployment_sizing.resolve_sleeve_notional(
                equity,
                cash,
                self._scaled_cap_pct(config.NYSE_SLEEVE_CAP_PCT, sleeve="nyse"),
                self.nyse_sleeve_value(),
                "nyse",
                self._cofire_notionals,
            )
        )

    def compute_international_notional(self):
        cap_pct = float(getattr(self, "international_cap_pct", 0.0) or 0.0)
        if cap_pct <= 0:
            return None
        equity = self.portfolio.equity(self.prices)
        cash = self.portfolio.cash
        return self._apply_wisdom_multiplier(
            deployment_sizing.resolve_sleeve_notional(
                equity,
                cash,
                self._scaled_cap_pct(cap_pct, sleeve="nyse"),
                self.international_sleeve_value(),
                "international",
                self._cofire_notionals,
            )
        )

    def compute_bond_notional(self):
        cap_pct = float(getattr(self, "bond_cap_pct", 0.0) or 0.0)
        if cap_pct <= 0:
            return None
        equity = self.portfolio.equity(self.prices)
        cash = self.portfolio.cash
        return self._apply_wisdom_multiplier(
            deployment_sizing.resolve_sleeve_notional(
                equity,
                cash,
                self._scaled_cap_pct(cap_pct, sleeve="nyse"),
                self.bond_sleeve_value(),
                "bond",
                self._cofire_notionals,
            )
        )

    def compute_spy_notional(self):
        equity = self.portfolio.equity(self.prices)
        cash = self.portfolio.cash
        base = self._apply_wisdom_multiplier(
            deployment_sizing.resolve_sleeve_notional(
                equity,
                cash,
                self._scaled_cap_pct(config.SPY_SLEEVE_CAP_PCT, sleeve="spy"),
                self.spy_sleeve_value(),
                "spy",
                self._cofire_notionals,
            )
        )
        return deployment_sizing.apply_spy_ladder(
            base, getattr(self, "_sizing_data", None)
        )

    def _find_position(self, symbol):
        target = config.normalize_symbol(symbol)

        class _Pos:
            def __init__(self, sym, qty, avg_entry, current_price):
                self.symbol = sym
                self.qty = qty
                self.avg_entry_price = avg_entry
                self.current_price = current_price

        for sym, qty in self.portfolio.positions.items():
            if config.normalize_symbol(sym) == target:
                price = self.prices.get(sym) or 0.0
                current = float(price) if price is not None else 0.0
                avg_entry = current
                total_cost = self.portfolio.entry_cost.get(sym)
                if total_cost and float(qty) > 0:
                    avg_entry = float(total_cost) / float(qty)
                return _Pos(sym, qty, avg_entry, current)
        return None

    def execute_reduce_notional(self, symbol, reduce_notional, *, reason="reduce", sleeve=None):
        pos = self._find_position(symbol)
        if pos is None or pos.qty <= 0:
            return None
        price = self.prices.get(pos.symbol)
        if price is None or not np.isfinite(price) or price <= 0:
            return None
        sell_notional = min(float(reduce_notional), float(pos.qty) * float(price))
        if sell_notional < 1:
            return None
        return self.execute_order(
            pos.symbol,
            "sell",
            notional=round(sell_notional, 2),
            reason=reason,
            sleeve=sleeve,
        )

    def profit_rebuy_blocked(self, symbol, now, *, cooldown_bars=None) -> bool:
        from modules.profit_target import profit_rebuy_blocked as _blocked

        return _blocked(self, symbol, now, cooldown_bars=cooldown_bars)

    def run_profit_target_exits(self, **kwargs) -> int:
        from modules.profit_target import run_profit_target_exits

        return run_profit_target_exits(self, **kwargs)

    def run_scaling_strategy(self, **kwargs) -> int:
        from modules.scaling_strategy import run_scaling_strategy

        return run_scaling_strategy(self, **kwargs)

    def execute_full_exit(self, symbol, **kwargs):
        pos = self._find_position(symbol)
        if pos is None or pos.qty <= 0:
            return None
        price = self.prices.get(pos.symbol)
        if price is None or not np.isfinite(price) or price <= 0:
            return None
        return self.execute_order(
            pos.symbol, "sell", notional=round(pos.qty * price, 2), **kwargs
        )

    def execute_order(self, symbol, side, notional=None, reduce_only=False, **kwargs):
        price = self.prices.get(symbol)
        if price is None or not np.isfinite(price) or price <= 0:
            return None
        if config.is_crypto(symbol) and side.lower() == "buy":
            notional = deployment_sizing.apply_alpaca_crypto_fee_reserve(
                notional, equity=self.portfolio.equity(self.prices)
            )
            if notional is None:
                return None
        order = self.portfolio.trade(
            symbol,
            side.lower(),
            price,
            tx_cost=_tx_cost_for_symbol(symbol),
            notional=notional,
        )
        if order:
            self.orders.append(order)
        return order


class BacktestPortfolio:
    def __init__(self, initial_capital=10000.0):
        self.cash = initial_capital
        self.initial_capital = initial_capital
        self.positions = {}
        self.entry_cost = {}
        self.execution_cost_usd = 0.0

    def equity(self, prices):
        total = self.cash
        for symbol, qty in self.positions.items():
            p = prices.get(symbol)
            if p is not None and np.isfinite(p):
                total += qty * p
        return total

    def _track_execution_cost(
        self, raw_price: float, fill_price: float, qty: float, tx_cost: float
    ) -> None:
        notional = abs(qty * fill_price)
        slip = abs(fill_price - raw_price) * abs(qty)
        fee = notional * max(0.0, tx_cost)
        self.execution_cost_usd += slip + fee

    def trade(self, symbol, side, price, tx_cost=TX_COST, notional=None):
        raw_price = float(price)
        price, tx_cost, slip_pct = effective_execution(raw_price, side, symbol, tx_cost)
        if notional is None:
            equity = self.equity({symbol: price})
            if config.backtest_small_account_context() or config.paper_aggressive_context():
                risk = config.effective_risk_per_trade(equity)
                max_order = (
                    config.effective_max_notional_per_order(equity)
                    if config.backtest_small_account_context()
                    else config.MAX_NOTIONAL_PER_ORDER
                )
                min_n = (
                    config.effective_min_notional(equity)
                    if config.backtest_small_account_context()
                    else config.MIN_NOTIONAL
                )
            else:
                risk = config.RISK_PER_TRADE
                max_order = config.MAX_NOTIONAL_PER_ORDER
                min_n = config.MIN_NOTIONAL
            notional = round(
                min(equity * risk, max_order, self.cash * 0.95),
                2,
            )
            if notional < min_n:
                return None
        if side == "buy":
            existing = self.positions.get(symbol, 0)
            if existing < 0:
                if notional is None or notional < 1:
                    return None
                cover_qty = min(abs(existing), notional / price)
                cost = cover_qty * price * (1 + tx_cost)
                if cost > self.cash:
                    return None
                self.cash -= cost
                self.positions[symbol] = existing + cover_qty
                if abs(self.positions[symbol]) < 1e-9:
                    del self.positions[symbol]
                self._track_execution_cost(raw_price, price, cover_qty, tx_cost)
                return {
                    "symbol": symbol,
                    "side": "buy",
                    "qty": cover_qty,
                    "notional": round(cover_qty * price, 2),
                    "pair_cover": True,
                }
            if notional < 1 or self.cash < notional:
                return None
            cost = notional * (1 + tx_cost)
            if cost > self.cash:
                return None
            qty = notional / price
            self.cash -= cost
            prev_qty = self.positions.get(symbol, 0.0)
            if prev_qty > 0:
                self.entry_cost[symbol] = self.entry_cost.get(symbol, 0.0) + cost
            else:
                self.entry_cost[symbol] = cost
            self.positions[symbol] = prev_qty + qty
            self._track_execution_cost(raw_price, price, qty, tx_cost)
            return {"symbol": symbol, "side": "buy", "qty": qty, "notional": notional}
        if side == "sell":
            qty = self.positions.get(symbol, 0)
            if qty <= 0:
                if (
                    (
                        config.effective_equity_pairs_enabled()
                        or config.effective_stat_arb_enabled()
                    )
                    and not config.is_crypto(symbol)
                ):
                    if notional is None or notional < 1:
                        return None
                    short_qty = notional / price
                    proceeds = notional * (1 - tx_cost)
                    self.cash += proceeds
                    self.positions[symbol] = self.positions.get(symbol, 0.0) - short_qty
                    self._track_execution_cost(raw_price, price, short_qty, tx_cost)
                    return {
                        "symbol": symbol,
                        "side": "sell",
                        "qty": short_qty,
                        "notional": notional,
                        "pair_short": True,
                    }
                if config.effective_stat_arb_enabled() and config.is_crypto(symbol):
                    if notional is None or notional < 1:
                        return None
                    short_qty = notional / price
                    proceeds = notional * (1 - tx_cost)
                    self.cash += proceeds
                    self.positions[symbol] = self.positions.get(symbol, 0.0) - short_qty
                    self._track_execution_cost(raw_price, price, short_qty, tx_cost)
                    return {
                        "symbol": symbol,
                        "side": "sell",
                        "qty": short_qty,
                        "notional": notional,
                        "pair_short": True,
                    }
                return None
            sell_notional = min(notional, qty * price) if notional else qty * price
            sell_qty = sell_notional / price
            proceeds = sell_notional * (1 - tx_cost)
            self.cash += proceeds
            total_cost = self.entry_cost.get(symbol, 0.0)
            if qty > 0 and total_cost > 0:
                self.entry_cost[symbol] = total_cost * (1.0 - sell_qty / qty)
            self.positions[symbol] = qty - sell_qty
            if self.positions[symbol] < 1e-9:
                del self.positions[symbol]
                self.entry_cost.pop(symbol, None)
            try:
                from modules.vol_position_sizing import release_top1_risk_on_sell

                release_top1_risk_on_sell(self, symbol, qty, sell_qty)
            except ImportError:
                pass
            self._track_execution_cost(raw_price, price, sell_qty, tx_cost)
            return {"symbol": symbol, "side": "sell", "qty": sell_qty, "notional": sell_notional}
        return None


def _rebalance_vti_core(portfolio, prices, core_pct: float) -> None:
    """Hold passive VTI at core_pct of total equity (commission-free)."""
    if core_pct <= 0 or VTI_CORE_SYMBOL not in prices.index:
        return
    price = prices.get(VTI_CORE_SYMBOL)
    if price is None or not np.isfinite(price) or float(price) <= 0:
        return
    price = float(price)
    eq = portfolio.equity(prices)
    target = round(eq * core_pct, 2)
    qty = portfolio.positions.get(VTI_CORE_SYMBOL, 0)
    current = float(qty) * price
    delta = round(target - current, 2)
    min_n = config.effective_min_notional(eq)
    if abs(delta) < min_n:
        return
    if delta > 0:
        portfolio.trade(
            VTI_CORE_SYMBOL, "buy", price, tx_cost=0.0, notional=delta
        )
    else:
        portfolio.trade(
            VTI_CORE_SYMBOL, "sell", price, tx_cost=0.0, notional=-delta
        )


def _parallel_backtest_worker(payload: tuple) -> dict:
    """ProcessPool worker — must stay at module top level for pickling."""
    data, kwargs = payload
    saved_social = config.SOCIAL_SLEEVE_ENABLED
    config.SOCIAL_SLEEVE_ENABLED = False
    try:
        return run_backtest(
            data, track_active_exposure=True, track_metrics=True, **kwargs
        )
    finally:
        config.SOCIAL_SLEEVE_ENABLED = saved_social


def _benchmark_return(data, start_idx):
    if BENCHMARK not in data.columns:
        return None
    col = data[BENCHMARK].iloc[start_idx:].dropna()
    if len(col) < 2 or col.iloc[0] <= 0:
        return None
    return (col.iloc[-1] / col.iloc[0] - 1) * 100


def _calendar_days_to_fetch(sim_days: int) -> int:
    """yfinance period is calendar days; reserve MA warmup + buffer for trading bars."""
    return int(sim_days + MIN_HISTORY + WARMUP_CALENDAR_BUFFER)


def _min_rows_for_backtest(sim_days: int) -> int:
    return MIN_HISTORY + max(10, int(sim_days * 0.85))


def _ensure_daily_data(days, refresh=False, use_max=False):
    return ensure_daily_data_cached(
        days,
        refresh=refresh,
        use_max=use_max,
        min_history=MIN_HISTORY,
        backtest_days=config.BACKTEST_DAYS,
        load_close_matrix=load_close_matrix,
        fetch_daily_history=fetch_daily_history,
        fetch_daily_history_for_tickers=fetch_daily_history_for_tickers,
    )


def _prefetch_screener_for_backtest(days, *, refresh=False, use_max=False) -> list[str]:
    """Ensure screener tickers exist in SQLite for dynamic-universe backtests."""
    from modules.dynamic_universe import (
        ensure_screener_prices_loaded,
        maybe_refresh_screener_universe,
    )

    saved_dyn = config.PAPER_DYNAMIC_UNIVERSE_ENABLED
    config.PAPER_DYNAMIC_UNIVERSE_ENABLED = True
    config.set_paper_aggressive_context(True)
    try:
        maybe_refresh_screener_universe(force=refresh)
        screener = config.load_screener_universe_tickers() or []
        fetch_days = _calendar_days_to_fetch(days or config.BACKTEST_DAYS)
        ensure_screener_prices_loaded(
            days=fetch_days if not use_max else None,
            use_max=use_max,
        )
        reset_caches(disk=False)
        return screener
    finally:
        config.PAPER_DYNAMIC_UNIVERSE_ENABLED = saved_dyn


def _static_equity_universe(data_columns) -> list[str]:
    return [c for c in data_columns if config._nyse_eligible_symbol(c)]


def _dynamic_equity_universe(data_columns) -> list[str]:
    return config.nyse_momentum_universe(data_columns)


def _universe_sample_lines(data, screener: list[str]) -> list[str]:
    """Highlight NASDAQ / IPO names present in the dynamic pool."""
    from modules.dynamic_universe import load_screener_ticker_meta, screener_coverage_report

    cov = screener_coverage_report(data.columns)
    meta = load_screener_ticker_meta()
    watch = {"NVDA", "TSLA", "AMD", "AAPL", "SPCX", "META", "GOOGL", "AMZN", "MSFT"}
    lines: list[str] = []
    if cov["screener_count"]:
        lines.append(
            f"  Screener price coverage: {cov['present_count']}/{cov['screener_count']} "
            f"({cov['coverage_pct']:.0f}%)"
        )
    dyn = set(_dynamic_equity_universe(data.columns))
    for sym in sorted(watch & dyn):
        row = meta.get(sym, {})
        ipo = " IPO" if row.get("is_ipo") else ""
        exch = row.get("exchange") or "?"
        lines.append(f"  {sym} ({exch}{ipo})")
    ipo_in_pool = [
        row["ticker"]
        for row in meta.values()
        if row.get("is_ipo") and row.get("ticker") in dyn
    ]
    if ipo_in_pool:
        lines.append(f"  IPO slots in pool: {', '.join(sorted(ipo_in_pool)[:8])}")
    if "SPCX" in watch and "SPCX" not in dyn and "SPCX" in screener:
        lines.append("  SPCX in screener file but no price column in backtest window")
    if cov["missing_count"] > 0:
        lines.append(
            f"  Missing prices: {', '.join(cov['missing'][:6])}"
            + (f" (+{cov['missing_count'] - 6})" if cov["missing_count"] > 6 else "")
        )
    return lines


def _paper_cap_scale_for_vti(vti_core_pct: float) -> float:
    """Paper aggressive sleeve scale for a given VTI core fraction."""
    active_fraction = max(0.0, 1.0 - vti_core_pct)
    lf = config.long_fund_scale()
    long_sum = (
        config.SPY_SLEEVE_CAP_PCT
        + config.CRYPTO_SLEEVE_CAP_PCT
        + config.NYSE_SLEEVE_CAP_PCT
    )
    if long_sum <= 0:
        return 0.0
    base_scale = round(lf * active_fraction, 6)
    base_deploy = round(base_scale * long_sum, 6)
    max_active = base_scale
    target_deploy = round(
        min(max_active, base_deploy * config.PAPER_ACTIVE_SLEEVE_BOOST), 6
    )
    return round(target_deploy / long_sum, 6)


def _backtest_cap_scale(vti_core_pct: float, *, paper_aggressive: bool) -> float:
    if paper_aggressive:
        return _paper_cap_scale_for_vti(vti_core_pct)
    return round(config.long_fund_scale() * max(0.0, 1.0 - vti_core_pct), 6)


def _resolve_backtest_vti_pct(
    equity: float,
    *,
    vol_score: float | None,
    volatility: str,
    macro_stress_flag: bool,
    paper_aggressive: bool,
    fixed_vti_core_pct: float,
) -> float:
    if not paper_aggressive:
        return fixed_vti_core_pct
    if not config.PAPER_DYNAMIC_VTI_ENABLED:
        return fixed_vti_core_pct
    return config.get_vti_core_pct(
        equity,
        vol_score=vol_score,
        macro_stress=macro_stress_flag,
        volatility=volatility,
    )


def _small_account_start_equity() -> float:
    return float(config.SMALL_ACCOUNT_BACKTEST_EQUITY)


def run_backtest(
    data,
    *,
    track_spy_fill=False,
    verbose=False,
    wisdom_mode=None,
    monthly_web=None,
    track_metrics=False,
    vti_core_pct: float = 0.0,
    paper_aggressive: bool = False,
    small_account: bool = False,
    paper_dynamic_vti: bool | None = None,
    paper_sleeve_features: bool | None = None,
    paper_social_enhanced: bool | None = None,
    paper_macro_regime: bool | None = None,
    paper_options_sleeve: bool | None = None,
    paper_dynamic_risk: bool | None = None,
    paper_market_neutral_pairs: bool | None = None,
    paper_stat_arb: bool | None = None,
    paper_stat_arb_optimized: bool | None = None,
    paper_thinking: bool | None = None,
    paper_crypto_v2: bool | None = None,
    paper_crypto_universe_expanded: bool | None = None,
    paper_risk_parity: bool | None = None,
    paper_vol_trading: bool | None = None,
    paper_vol_live_parity: bool = False,
    paper_dynamic_universe: bool | None = None,
    paper_dynamic_universe_strict: bool | None = None,
    paper_ipo_safety: bool | None = None,
    paper_international_sleeve: bool | None = None,
    paper_bond_sleeve: bool | None = None,
    paper_profit_target: bool | None = None,
    paper_scaling_strategy: bool | None = None,
    paper_sector_rotation: bool | None = None,
    paper_pattern_awareness: bool | None = None,
    paper_pattern_bearish_only: bool | None = None,
    paper_vol_position_sizing: bool | None = None,
    paper_loss_cutting: bool | None = None,
    top1_vol_conservative: bool | None = None,
    top1_loss_conservative: bool | None = None,
    top1_sector_rotation_conservative: bool | None = None,
    paper_sector_rotation_hybrid: bool | None = None,
    strict_pit: bool | None = None,
    paper_tech_guard: bool | None = None,
    track_active_exposure: bool = False,
    simulate_live_thinking: bool = False,
    live_thinking_start_equity: float | None = None,
    with_news: bool = False,
):
    """Run fund pipeline on daily data; return performance + optional SPY fill metrics."""
    apply_run_options_to_config()
    apply_default_execution_costs()
    if RUN_OPTIONS.fast_mode:
        data = apply_fast_mode_data(data)
    prepare_indicator_cache(data, spy_ma_window=config.SPY_MA_WINDOW)
    saved_paper_ctx = config.paper_aggressive_context()
    saved_small_ctx = config.backtest_small_account_context()
    saved_social = config.SOCIAL_SLEEVE_ENABLED
    saved_dynamic_vti = config.PAPER_DYNAMIC_VTI_ENABLED
    saved_paper_sleeve_flags = config.snapshot_paper_sleeve_flags()
    saved_macro_overrides = config.SOCIAL_MACRO_OVERRIDES_ENABLED
    saved_macro_boost = config.PAPER_SOCIAL_MACRO_BOOST_ENABLED
    saved_paper_macro = config.PAPER_MACRO_REGIME_ADAPTOR_ENABLED
    saved_paper_options = config.PAPER_OPTIONS_SLEEVE_ENABLED
    saved_paper_dynamic_risk = config.PAPER_DYNAMIC_RISK_ENABLED
    saved_paper_market_neutral_pairs = config.PAPER_MARKET_NEUTRAL_PAIRS
    saved_paper_stat_arb = config.PAPER_STAT_ARB_ENABLED
    saved_paper_stat_arb_opt = config.PAPER_STAT_ARB_OPTIMIZED
    saved_paper_thinking = config.PAPER_THINKING_ENGINE_ENABLED
    saved_paper_crypto_v2 = config.PAPER_CRYPTO_V2_ENABLED
    saved_paper_crypto_expanded = config.PAPER_CRYPTO_UNIVERSE_EXPANDED
    saved_paper_risk_parity = config.PAPER_RISK_PARITY_ENABLED
    saved_paper_vol_trading = config.PAPER_VOL_TRADING_ENABLED
    saved_paper_dynamic_univ = config.PAPER_DYNAMIC_UNIVERSE_ENABLED
    saved_paper_dynamic_univ_strict = config.PAPER_DYNAMIC_UNIVERSE_STRICT
    saved_paper_ipo_safety = config.PAPER_IPO_SAFETY_ENABLED
    saved_paper_international = config.PAPER_INTERNATIONAL_SLEEVE_ENABLED
    saved_paper_bond = config.PAPER_BOND_SLEEVE_ENABLED
    saved_paper_profit_target = config.PAPER_PROFIT_TARGET_ENABLED
    saved_paper_scaling = config.PAPER_SCALING_STRATEGY_ENABLED
    saved_paper_sector_rotation = config.PAPER_SECTOR_ROTATION_ENABLED
    saved_paper_pattern_awareness = config.PAPER_PATTERN_AWARENESS_ENABLED
    saved_paper_pattern_bearish_only = config.PAPER_PATTERN_BEARISH_ONLY
    saved_paper_vol_position_sizing = config.PAPER_VOL_POSITION_SIZING_ENABLED
    saved_paper_loss_cutting = config.PAPER_LOSS_CUTTING_ENABLED
    from modules.vol_position_sizing import (
        set_vol_sizing_conservative,
        vol_sizing_conservative_mode,
    )
    from modules.loss_cutting import (
        loss_cutting_conservative_mode,
        set_loss_cutting_conservative,
    )
    from modules.sector_rotation import (
        sector_rotation_conservative_active,
        set_sector_rotation_conservative,
    )

    saved_top1_vol_conservative = vol_sizing_conservative_mode()
    saved_top1_loss_conservative = loss_cutting_conservative_mode()
    saved_top1_sector_rotation_conservative = sector_rotation_conservative_active()
    saved_sector_rotation_hybrid = config.SECTOR_ROTATION_HYBRID_MODE
    saved_sector_rotation_conservative_cfg = config.SECTOR_ROTATION_CONSERVATIVE_MODE
    saved_strict_pit = config.backtest_strict_pit_context()
    saved_sector_rotation = config.SECTOR_ROTATION_ENABLED
    saved_paper_tech_guard = config.PAPER_TECH_GUARD_ENABLED
    saved_intl_prefetch = config.backtest_international_prefetch()
    saved_bond_prefetch = config.backtest_bond_prefetch()
    saved_backtest_paper_sleeves = config.backtest_paper_sleeves_context()
    saved_live_thinking_ctx = config.live_thinking_sim_context()
    config.set_paper_aggressive_context(paper_aggressive)
    config.set_backtest_paper_sleeves_context(paper_aggressive)
    config.set_backtest_small_account_context(small_account)
    pit_on = bool(strict_pit if strict_pit is not None else RUN_OPTIONS.strict_pit)
    config.set_backtest_strict_pit_context(pit_on)
    if pit_on:
        from modules.pit_replay import apply_strict_pit_execution_costs

        apply_strict_pit_execution_costs(RUN_OPTIONS)
    if simulate_live_thinking and small_account:
        config.set_live_thinking_sim_context(True)
        if paper_thinking is not False:
            config.PAPER_THINKING_ENGINE_ENABLED = True
    if paper_dynamic_vti is not None:
        config.PAPER_DYNAMIC_VTI_ENABLED = bool(paper_dynamic_vti)
    if paper_aggressive and paper_sleeve_features is not None:
        config.apply_paper_sleeve_flags(
            {
                "nyse_overlap": paper_sleeve_features,
                "adaptive_chunk": paper_sleeve_features,
                "cofire_budget": paper_sleeve_features,
                "spy_exit_on_ma_break": paper_sleeve_features,
            }
        )
    if paper_social_enhanced is not None:
        config.SOCIAL_MACRO_OVERRIDES_ENABLED = bool(paper_social_enhanced)
        config.PAPER_SOCIAL_MACRO_BOOST_ENABLED = bool(paper_social_enhanced)
    if paper_macro_regime is not None:
        config.PAPER_MACRO_REGIME_ADAPTOR_ENABLED = bool(paper_macro_regime)
    if paper_options_sleeve is not None:
        config.PAPER_OPTIONS_SLEEVE_ENABLED = bool(paper_options_sleeve)
    if paper_dynamic_risk is not None:
        config.PAPER_DYNAMIC_RISK_ENABLED = bool(paper_dynamic_risk)
    if paper_market_neutral_pairs is not None:
        config.PAPER_MARKET_NEUTRAL_PAIRS = bool(paper_market_neutral_pairs)
    if paper_stat_arb is not None:
        config.PAPER_STAT_ARB_ENABLED = bool(paper_stat_arb)
    if paper_stat_arb_optimized is not None:
        config.PAPER_STAT_ARB_OPTIMIZED = bool(paper_stat_arb_optimized)
    if paper_thinking is not None:
        config.PAPER_THINKING_ENGINE_ENABLED = bool(paper_thinking)
    elif RUN_OPTIONS.no_thinking or RUN_OPTIONS.fast_mode:
        config.PAPER_THINKING_ENGINE_ENABLED = False
    if paper_crypto_v2 is not None:
        config.PAPER_CRYPTO_V2_ENABLED = bool(paper_crypto_v2)
    if paper_crypto_universe_expanded is not None:
        config.PAPER_CRYPTO_UNIVERSE_EXPANDED = bool(paper_crypto_universe_expanded)
    if paper_risk_parity is not None:
        config.PAPER_RISK_PARITY_ENABLED = bool(paper_risk_parity)
    if paper_vol_trading is not None:
        config.PAPER_VOL_TRADING_ENABLED = bool(paper_vol_trading)
    if paper_dynamic_universe is not None:
        config.PAPER_DYNAMIC_UNIVERSE_ENABLED = bool(paper_dynamic_universe)
    if paper_dynamic_universe_strict is not None:
        config.PAPER_DYNAMIC_UNIVERSE_STRICT = bool(paper_dynamic_universe_strict)
    if paper_ipo_safety is not None:
        config.PAPER_IPO_SAFETY_ENABLED = bool(paper_ipo_safety)
    if paper_international_sleeve is not None:
        config.PAPER_INTERNATIONAL_SLEEVE_ENABLED = bool(paper_international_sleeve)
        config.set_backtest_international_prefetch(bool(paper_international_sleeve))
    if paper_bond_sleeve is not None:
        config.PAPER_BOND_SLEEVE_ENABLED = bool(paper_bond_sleeve)
        config.set_backtest_bond_prefetch(bool(paper_bond_sleeve))
    if paper_profit_target is not None:
        config.PAPER_PROFIT_TARGET_ENABLED = bool(paper_profit_target)
    if paper_scaling_strategy is not None:
        config.PAPER_SCALING_STRATEGY_ENABLED = bool(paper_scaling_strategy)
    if paper_pattern_awareness is not None:
        config.PAPER_PATTERN_AWARENESS_ENABLED = bool(paper_pattern_awareness)
    if paper_pattern_bearish_only is not None:
        config.PAPER_PATTERN_BEARISH_ONLY = bool(paper_pattern_bearish_only)
    if paper_vol_position_sizing is not None:
        config.PAPER_VOL_POSITION_SIZING_ENABLED = bool(paper_vol_position_sizing)
    if paper_loss_cutting is not None:
        config.PAPER_LOSS_CUTTING_ENABLED = bool(paper_loss_cutting)
    if top1_vol_conservative is not None:
        set_vol_sizing_conservative(bool(top1_vol_conservative))
    if top1_loss_conservative is not None:
        set_loss_cutting_conservative(bool(top1_loss_conservative))
    if top1_sector_rotation_conservative is not None:
        config.SECTOR_ROTATION_CONSERVATIVE_MODE = bool(top1_sector_rotation_conservative)
        set_sector_rotation_conservative(bool(top1_sector_rotation_conservative))
    if paper_sector_rotation_hybrid is not None:
        config.SECTOR_ROTATION_HYBRID_MODE = bool(paper_sector_rotation_hybrid)
    if paper_sector_rotation is not None:
        config.PAPER_SECTOR_ROTATION_ENABLED = bool(paper_sector_rotation)
        if not paper_aggressive:
            config.SECTOR_ROTATION_ENABLED = bool(paper_sector_rotation)
    if paper_tech_guard is not None:
        config.PAPER_TECH_GUARD_ENABLED = bool(paper_tech_guard)
    if RUN_OPTIONS.fast_mode:
        apply_run_options_to_config()
    if paper_aggressive and not any(
        flag is True
        for flag in (
            paper_risk_parity,
            paper_macro_regime,
            paper_stat_arb_optimized,
            paper_social_enhanced,
        )
    ):
        config.enforce_best_paper_stack()
    if paper_aggressive and (
        config.PAPER_OPTIONS_SLEEVE_ENABLED or config.PAPER_VOL_TRADING_ENABLED
    ):
        from modules.options_sleeve import ensure_vix_daily

        ensure_vix_daily()

    fixed_vti_core_pct = vti_core_pct
    if paper_aggressive and not config.PAPER_DYNAMIC_VTI_ENABLED:
        fixed_vti_core_pct = (
            vti_core_pct if vti_core_pct > 0 else config.PAPER_VTI_CORE_PCT
        )

    start_date = data.index[MIN_HISTORY]
    end_date = data.index[-1]
    cooldown_bars = DAILY_COOLDOWN_BARS
    sharpe_scale = np.sqrt(252)
    sim_days = (end_date - start_date).days
    cap_scale = _backtest_cap_scale(fixed_vti_core_pct, paper_aggressive=paper_aggressive)

    initial_capital = (
        float(live_thinking_start_equity)
        if small_account and live_thinking_start_equity is not None
        else (_small_account_start_equity() if small_account else 10000.0)
    )
    portfolio = BacktestPortfolio(initial_capital=initial_capital)
    from collections import Counter

    portfolio.international_stats = {"trades": 0, "symbols": Counter(), "active_bars": 0}
    portfolio.bond_stats = {
        "trades": 0,
        "buys": 0,
        "sells": 0,
        "symbols": Counter(),
        "active_bars": 0,
        "max_cap_pct": 0.0,
    }
    pair_cooldown = {}
    risk_manager = RiskManager(max_drawdown_pct=config.MAX_DRAWDOWN_PCT)
    equity_curve = []
    regime_counts = {}
    total_crypto = 0
    total_equity = 0
    total_international = 0
    total_bond = 0
    total_spy = 0
    total_spy_entries = 0
    total_spy_exits = 0
    total_orders = 0
    pause_days = 0
    halt_liquidations = 0
    exposure_samples = []
    vti_core_samples = []
    active_exposure_samples = []
    tech_exposure_samples = []
    spy_exposure_samples = []
    nyse_exposure_samples = []
    crypto_exposure_samples = []
    cofire_days = 0
    prev_crypto_value = 0.0
    crypto_pnl_contribution = 0.0
    trade_days = 0
    total_social = 0
    gld_target_days = 0
    social_sim_days = 0
    social_portfolio = None
    social_curve = []
    macro_portfolio = None
    macro_curve = []
    regime_shift_days = 0
    macro_gld_days = 0
    macro_energy_days = 0
    macro_sim_days = 0
    risk_samples: list[float] = []
    high_risk_days = 0
    pairs_traded = 0
    pair_daily_pnl: list[float] = []
    prev_pair_sleeve_value = 0.0
    options_state: dict = {
        "contracts": [],
        "last_roll_i": -999,
        "total_premium": 0.0,
        "assignment_drag": 0.0,
        "rolls": 0,
        "calm_days": 0,
        "premium_days": 0,
    }
    vol_state: dict = {
        "mode": "flat",
        "notional": 0.0,
        "last_roll_i": -999,
        "premium_collected": 0.0,
        "protection_pnl": 0.0,
        "cum_pnl": 0.0,
        "trades": 0,
    }
    equity_peak = initial_capital
    monthly_web = None
    if config.effective_macro_regime_adaptor_enabled():
        from modules.macro_regime_adaptor import run_macro_regime_backtest_day

        macro_portfolio = BacktestPortfolio(initial_capital=initial_capital)
    elif config.effective_social_sleeve_enabled():
        from modules.social_sleeve_backtest import (
            run_social_backtest_day,
            social_score_for_backtest,
        )
        from modules.wayback_sentiment import load_monthly_web_sentiment

        try:
            monthly_web = load_monthly_web_sentiment()
        except Exception:
            monthly_web = None
        social_portfolio = BacktestPortfolio(initial_capital=initial_capital)

    spy_cap_pct = round(config.SPY_SLEEVE_CAP_PCT * cap_scale, 6)
    macro_daily = None
    need_macro_daily = (
        config.game_plan_active()
        or (paper_aggressive and config.PAPER_DYNAMIC_VTI_ENABLED)
        or config.effective_macro_regime_adaptor_enabled()
        or config.effective_bond_sleeve_enabled()
    )
    if need_macro_daily:
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
    pod_risk_state: dict = {"peaks": {}}
    sample_rp_meta: dict | None = None
    thinking_cache = {
        "regime": None,
        "regime_bucket": None,
        "last_news_date": None,
        "last_tilt_bar": None,
        "last_deltas": {},
        "scales": {"spy_scale": 1.0, "nyse_scale": 1.0, "crypto_scale": 1.0},
        "vti_pct": None,
        "events": [],
    }
    from modules.crypto_dual_sleeve import CryptoV2State

    crypto_v2_book = CryptoV2State()
    last_executor = None

    for i in range(MIN_HISTORY, len(data)):
        window = data.iloc[: i + 1]
        prices = window.iloc[-1]
        eq = portfolio.equity(prices)
        if small_account:
            config.configure_account_profile(eq)
        equity_curve.append(eq)

        prev_halted = risk_manager.halted
        can_trade = risk_manager.check_drawdown(eq)
        if not can_trade:
            if risk_manager.should_liquidate_on_breach():
                halt_liquidations += trim_long_sleeves_to_cash_target(
                    portfolio,
                    prices,
                    config.HALT_TARGET_CASH_PCT,
                    TX_COST,
                    protect_symbols=(
                        frozenset({VTI_CORE_SYMBOL}) if vti_core_pct > 0 else None
                    ),
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

        if wisdom_mode:
            from modules.wisdom_sentiment import resolve_backtest_regime

            regime, vol, wisdom_paused, sizing_mult = resolve_backtest_regime(
                window,
                data.index[i],
                monthly_web,
                wisdom_mode=wisdom_mode,
            )
            sentiment = get_price_sentiment(window)
            if wisdom_paused:
                pause_days += 1
        else:
            sentiment = get_price_sentiment(window)
            vol = get_volatility(window)
            regime = get_market_regime(sentiment, vol)
            sizing_mult = 1.0
            if regime_entries_paused(regime, window, sentiment):
                pause_days += 1
        regime_counts[regime] = regime_counts.get(regime, 0) + 1

        yield_gated = False
        macro_stress_flag = False
        if macro_daily is not None and not macro_daily.empty:
            bar_date = data.index[i]
            macro_window = macro_daily.loc[:bar_date]
            if len(macro_window) >= 50:
                yield_gated = yield_gate_blocks(macro_window)
                macro_stress_flag = macro_stress(macro_window, regime)

        vol_score = cross_asset_vol_score(window)
        if paper_aggressive:
            if config.PAPER_DYNAMIC_RISK_ENABLED:
                config.set_dynamic_risk_context(
                    vol_score=vol_score,
                    regime=regime,
                    macro_stress=macro_stress_flag,
                )
                day_risk = config.effective_risk_per_trade(
                    eq,
                    vol_score=vol_score,
                    regime=regime,
                    macro_stress=macro_stress_flag,
                )
            else:
                day_risk = config.RISK_PER_TRADE
            risk_samples.append(day_risk)
            if day_risk >= config.PAPER_RISK_CALM_BULL_PCT - 1e-9:
                high_risk_days += 1
        vti_core_pct = _resolve_backtest_vti_pct(
            eq,
            vol_score=vol_score,
            volatility=vol,
            macro_stress_flag=macro_stress_flag,
            paper_aggressive=paper_aggressive,
            fixed_vti_core_pct=fixed_vti_core_pct,
        )
        thinking_scales = dict(thinking_cache["scales"])
        live_thinking = simulate_live_thinking and small_account
        thinking_on = config.effective_thinking_engine_enabled() and (
            paper_aggressive or live_thinking
        )
        pit_slot = "premarket" if with_news else "close"
        if config.effective_strict_pit_backtest():
            from modules.pit_replay import (
                effective_as_of_ts,
                pit_thinking_window,
                set_pit_bar_context,
            )

            set_pit_bar_context(data.index[i], i, pit_slot)
        if thinking_on:
            from modules.thinking_engine import (
                THINKING_COOLDOWN_BREAK_IMPACT,
                THINKING_MIN_MATERIAL_DELTA,
                THINKING_TILT_COOLDOWN_BARS,
                apply_thinking_tilt_to_caps,
                build_backtest_thinking_result,
                deltas_materially_changed,
                executor_scales_from_caps,
                regime_tilt_bucket,
            )

            tilt_max_delta = (
                config.LIVE_THINKING_MAX_SLEEVE_DELTA if live_thinking else None
            )
            regime_bucket = regime_tilt_bucket(regime)
            refresh = thinking_cache.get("regime_bucket") != regime_bucket
            last_tilt_bar = thinking_cache.get("last_tilt_bar")
            bar_date = data.index[i].date()
            bar_ts = data.index[i]
            think_window = window
            pit_as_of = None
            if config.effective_strict_pit_backtest():
                think_window = pit_thinking_window(
                    window,
                    bar_index=i,
                    slot=pit_slot,
                    strict=True,
                )
                pit_as_of = effective_as_of_ts(bar_ts, slot=pit_slot, strict=True)
            news_digest: dict | None = None
            if with_news:
                if thinking_cache.get("last_news_date") != bar_date:
                    thinking_cache["last_news_date"] = bar_date
                    from modules.thinking_news import (
                        NEWS_IMPACT_MIN_FOR_CAP_DELTAS,
                        NEWS_IMPACT_MIN_FOR_LIVE_TILT,
                        synthesize_backtest_news,
                    )

                    peek = synthesize_backtest_news(
                        think_window,
                        regime,
                        vol,
                        slot="premarket",
                        bar_ts=bar_ts,
                        bar_index=i,
                    )
                    thinking_cache["daily_news_peek"] = peek
                    impact_min = (
                        NEWS_IMPACT_MIN_FOR_LIVE_TILT
                        if live_thinking
                        else NEWS_IMPACT_MIN_FOR_CAP_DELTAS
                    )
                    if float(peek.get("news_impact_score") or 0) >= impact_min:
                        refresh = True
            if refresh and with_news and not live_thinking:
                from modules.thinking_news import (
                    NEWS_IMPACT_MIN_FOR_CAP_DELTAS,
                    NEWS_IMPACT_NEUTRAL_PAPER_MIN,
                    is_neutral_thinking_regime,
                )

                peek = thinking_cache.get("daily_news_peek") or {}
                impact = float(peek.get("news_impact_score") or 0)
                min_req = (
                    NEWS_IMPACT_NEUTRAL_PAPER_MIN
                    if is_neutral_thinking_regime(regime)
                    else NEWS_IMPACT_MIN_FOR_CAP_DELTAS
                )
                if impact < min_req:
                    refresh = False
                    thinking_cache["regime"] = regime
                    thinking_cache["regime_bucket"] = regime_bucket
            if refresh and last_tilt_bar is not None:
                if (i - int(last_tilt_bar)) < THINKING_TILT_COOLDOWN_BARS:
                    peek_impact = float(
                        (thinking_cache.get("daily_news_peek") or {}).get(
                            "news_impact_score"
                        )
                        or 0
                    )
                    if peek_impact < THINKING_COOLDOWN_BREAK_IMPACT:
                        refresh = False
            if refresh:
                vti_before = vti_core_pct
                if with_news:
                    from modules.thinking_news import synthesize_backtest_news

                    news_digest = thinking_cache.pop("daily_news_peek", None)
                    if news_digest is None:
                        news_digest = synthesize_backtest_news(
                            think_window,
                            regime,
                            vol,
                            slot="premarket",
                            bar_ts=bar_ts,
                            bar_index=i,
                        )
                    thinking = build_backtest_thinking_result(
                        think_window,
                        regime,
                        vol,
                        news_headlines=news_digest.get("headlines"),
                        news_slot="premarket",
                        as_of=pit_as_of,
                        pit_slot=pit_slot,
                    )
                else:
                    thinking = build_backtest_thinking_result(
                        think_window,
                        regime,
                        vol,
                        as_of=pit_as_of,
                        pit_slot=pit_slot,
                    )
                base_caps = dict(config.fund_allocation_pct())
                base_caps["vti_core"] = vti_core_pct
                merged, deltas, log_line = apply_thinking_tilt_to_caps(
                    base_caps,
                    thinking["suggested_tilt"],
                    confidence=thinking["confidence"],
                    market_summary=thinking["market_summary"],
                    equity=eq,
                    max_sleeve_delta=tilt_max_delta,
                    allow_small_account=live_thinking,
                    tilt_rationale=str(thinking.get("tilt_rationale") or ""),
                )
                thinking_cache["regime"] = regime
                thinking_cache["regime_bucket"] = regime_bucket
                material = {
                    k: v
                    for k, v in deltas.items()
                    if abs(float(v)) >= THINKING_MIN_MATERIAL_DELTA
                }
                if material and deltas_materially_changed(
                    thinking_cache.get("last_deltas"), deltas
                ):
                    thinking_cache["vti_pct"] = merged["vti_core"]
                    thinking_cache["scales"] = {
                        "spy_scale": executor_scales_from_caps(base_caps, merged).get(
                            "spy", 1.0
                        ),
                        "nyse_scale": executor_scales_from_caps(base_caps, merged).get(
                            "nyse", 1.0
                        ),
                        "crypto_scale": executor_scales_from_caps(
                            base_caps, merged
                        ).get("crypto", 1.0),
                    }
                    thinking_cache["last_thinking"] = thinking
                    thinking_cache["last_tilt_bar"] = i
                    thinking_cache["last_deltas"] = dict(deltas)
                    summary = thinking.get("market_summary") or {}
                    vix = summary.get("vix")
                    event: dict = {
                        "date": str(data.index[i].date()),
                        "regime": regime,
                        "vol": vol,
                        "vix": vix,
                        "narrative": thinking.get("narrative"),
                        "tilt": thinking.get("suggested_tilt"),
                        "deltas": deltas,
                        "vti_before": round(vti_before, 4),
                        "vti_after": round(float(merged["vti_core"]), 4),
                        "log": log_line,
                        "confidence": thinking.get("confidence"),
                        "validation_score": thinking.get("validation_score"),
                    }
                    if news_digest:
                        event["news_slot"] = news_digest.get("slot")
                        event["news_impact_score"] = news_digest.get("news_impact_score")
                        event["news_theme_summary"] = news_digest.get("theme_summary")
                    elif with_news:
                        event["news_impact_score"] = thinking.get("news_impact_score")
                        event["news_theme_summary"] = summary.get("news_theme_summary")
                    thinking_cache["events"].append(event)
                elif not material:
                    thinking_cache["regime"] = regime
                    thinking_cache["regime_bucket"] = regime_bucket
            if thinking_cache["vti_pct"] is not None:
                vti_core_pct = float(thinking_cache["vti_pct"])
            thinking_scales = dict(thinking_cache["scales"])
        cap_scale = _backtest_cap_scale(vti_core_pct, paper_aggressive=paper_aggressive)
        macro_regime = None
        if config.effective_macro_regime_adaptor_enabled():
            from modules.macro_regime_adaptor import (
                apply_yield_gate_boost,
                evaluate_macro_regime,
            )

            macro_window = None
            if macro_daily is not None and not macro_daily.empty:
                macro_window = macro_daily.loc[: data.index[i]]
            macro_regime = evaluate_macro_regime(
                window, daily_macro=macro_window, ts=data.index[i]
            )
            if macro_regime.get("active"):
                regime_shift_days += 1
                yield_gated = apply_yield_gate_boost(yield_gated, macro_regime)
        spy_cap_pct = round(config.SPY_SLEEVE_CAP_PCT * cap_scale, 6)
        if track_active_exposure or paper_aggressive:
            vti_core_samples.append(vti_core_pct)
            active_exposure_samples.append(max(0.0, 1.0 - vti_core_pct))
        from modules.tech_concentration_guard import tech_weight, _prices_mapping

        tw_caps = dict(config.fund_allocation_pct())
        tw_caps["vti_core"] = vti_core_pct
        tech_exposure_samples.append(
            tech_weight(
                tw_caps,
                positions=portfolio.positions,
                prices=_prices_mapping(prices),
            )
        )

        if vti_core_pct > 0:
            _rebalance_vti_core(portfolio, prices, vti_core_pct)
        active_fraction = max(0.0, 1.0 - vti_core_pct)
        if paper_aggressive:
            sizing_mult = max(
                float(sizing_mult), config.PAPER_WISDOM_SIZING_FLOOR
            )
        executor = BacktestExecutor(
            portfolio,
            prices,
            active_fraction=active_fraction,
            cap_scale=cap_scale,
        )
        if config.effective_crypto_v2_enabled():
            executor._crypto_v2_book = crypto_v2_book
        last_executor = executor
        if macro_regime is not None and macro_regime.get("active"):
            executor.set_regime_sleeve_scales(
                spy_scale=macro_regime.get("spy_scale", 1.0),
                nyse_scale=macro_regime.get("nyse_scale", 1.0),
            )
        executor.set_thinking_sleeve_scales(**thinking_scales)
        if config.effective_vol_position_sizing_enabled() or config.effective_loss_cutting_enabled():
            from modules.vol_position_sizing import set_top1_sizing_context

            set_top1_sizing_context(executor, thinking_cache.get("last_thinking"))
        if config.effective_risk_parity_enabled() and paper_aggressive:
            from modules.risk_parity_sleeve import apply_risk_parity_cycle
            from modules.thinking_engine import executor_scales_from_caps

            opt_val = sum(
                float(c.get("notional", 0))
                for c in options_state.get("contracts", [])
            )
            base_caps = dict(config.fund_allocation_pct())
            base_caps["vti_core"] = vti_core_pct
            merged_caps, pod_scales, rp_meta, _ = apply_risk_parity_cycle(
                window,
                regime,
                vol,
                executor,
                macro_stress=macro_stress_flag,
                equity=eq,
                base_caps=base_caps,
                pair_value=executor.pair_sleeve_value(),
                vol_value=abs(float(vol_state.get("notional", 0))),
                options_value=opt_val,
                persist_pod=False,
            )
            if rp_meta and sample_rp_meta is None:
                sample_rp_meta = rp_meta
            pod_risk_state.update({"peaks": pod_risk_state.get("peaks", {})})
            new_vti = float(merged_caps.get("vti_core", vti_core_pct))
            if abs(new_vti - vti_core_pct) > 0.004:
                vti_core_pct = new_vti
                if vti_core_pct > 0:
                    _rebalance_vti_core(portfolio, prices, vti_core_pct)
            cap_scales = executor_scales_from_caps(base_caps, merged_caps)
            thinking_scales = {
                "spy_scale": thinking_scales["spy_scale"] * cap_scales.get("spy", 1.0),
                "nyse_scale": thinking_scales["nyse_scale"] * cap_scales.get("nyse", 1.0),
                "crypto_scale": thinking_scales["crypto_scale"] * cap_scales.get("crypto", 1.0),
            }
            executor.set_thinking_sleeve_scales(**thinking_scales)
            executor.set_pod_risk_scales(pod_scales)
        executor.set_sizing_context(window)
        executor.set_wisdom_sizing_multiplier(sizing_mult)
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
        if getattr(executor, "_cofire_notionals", None) and len(executor._cofire_notionals) >= 2:
            cofire_days += 1
        crypto_n = run_crypto_strategy(
            window,
            executor,
            regime,
            i,
            pair_cooldown,
            cooldown_bars=cooldown_bars,
            volatility=vol,
        )
        total_crypto += crypto_n
        if (
            config.effective_stat_arb_enabled()
            or config.effective_market_neutral_pairs_enabled()
            or config.effective_crypto_v2_enabled()
        ):
            pairs_traded += crypto_n
        spy_exit_n = run_spy_exits(window, executor, regime)
        from modules.profit_target import run_profit_target_exits

        run_profit_target_exits(
            executor,
            bar_idx=i,
            full_data=data,
            now=i,
            equity_session_open=True,
        )
        from modules.scaling_strategy import run_scaling_strategy

        run_scaling_strategy(
            executor,
            bar_idx=i,
            full_data=data,
            equity_session_open=True,
        )
        from modules.loss_cutting import run_loss_cutting_exits

        run_loss_cutting_exits(
            executor,
            bar_idx=i,
            full_data=data,
            equity_session_open=True,
        )
        spy_entry_n = run_spy_strategy(
            window,
            executor,
            regime,
            i,
            pair_cooldown,
            cooldown_bars=cooldown_bars,
            yield_gated=yield_gated,
        )
        total_spy_exits += spy_exit_n
        total_spy_entries += spy_entry_n
        total_spy += spy_exit_n + spy_entry_n
        if config.effective_equity_pairs_enabled():
            eq_pairs = run_equity_pairs_strategy(
                window,
                executor,
                regime,
                i,
                pair_cooldown,
                cooldown_bars=cooldown_bars,
                yield_gated=yield_gated,
            )
            total_equity += eq_pairs
            pairs_traded += eq_pairs
        else:
            run_ipo_safety_trims(data, executor, bar_idx=i)
            total_equity += run_equity_strategy(
                window,
                executor,
                regime,
                i,
                pair_cooldown,
                cooldown_bars=cooldown_bars,
                yield_gated=yield_gated,
                full_data=data,
                bar_idx=i,
            )
        if config.effective_international_sleeve_enabled():
            intl_scales = {
                "nyse": float(thinking_scales.get("nyse_scale", 1.0)),
                "international": float(thinking_scales.get("nyse_scale", 1.0)),
            }
            total_international += run_international_strategy(
                window,
                executor,
                regime,
                i,
                pair_cooldown,
                cooldown_bars=cooldown_bars,
                yield_gated=yield_gated,
                full_data=data,
                bar_idx=i,
                thinking_scales=intl_scales,
            )
        if config.effective_bond_sleeve_enabled():
            from modules.options_sleeve import vix_as_of

            macro_window = None
            if macro_daily is not None and not macro_daily.empty:
                macro_window = macro_daily.loc[: data.index[i]]
            total_bond += run_bond_strategy(
                window,
                executor,
                regime,
                i,
                pair_cooldown,
                cooldown_bars=cooldown_bars,
                volatility=vol,
                vol_score=vol_score,
                vix=vix_as_of(data.index[i]),
                macro_stress=macro_stress_flag,
                macro_window=macro_window,
                thinking_scales=thinking_scales,
            )
        pair_val = executor.pair_sleeve_value()
        pair_daily_pnl.append(pair_val - prev_pair_sleeve_value)
        prev_pair_sleeve_value = pair_val
        if macro_portfolio is not None and macro_regime is not None:
            macro_actions, macro_meta = run_macro_regime_backtest_day(
                macro_portfolio, prices, macro_regime, market_open=True
            )
            total_social += len(macro_actions)
            macro_sim_days += 1
            tgt = macro_meta.get("target")
            if tgt == "GLD":
                macro_gld_days += 1
            elif tgt in ("XOM", "XLE"):
                macro_energy_days += 1
            macro_curve.append(macro_portfolio.equity(prices))
        elif social_portfolio is not None:
            agg = social_score_for_backtest(data.index[i], window, monthly_web)
            social_actions, social_meta = run_social_backtest_day(
                social_portfolio, prices, agg, market_open=True
            )
            total_social += len(social_actions)
            social_sim_days += 1
            if social_meta.get("target") == "GLD":
                gld_target_days += 1
            social_curve.append(social_portfolio.equity(prices))
        if config.effective_options_sleeve_enabled():
            from modules.options_sleeve import run_options_backtest_day
            from modules.risk_parity_sleeve import pod_entries_allowed

            if pod_entries_allowed(executor, "options"):
                _, opt_meta = run_options_backtest_day(
                    portfolio,
                    prices,
                    bar_i=i,
                    state=options_state,
                    volatility=vol,
                    vol_score=vol_score,
                    ts=data.index[i],
                    market_open=True,
                )
            else:
                opt_meta = {}
            if opt_meta.get("calm"):
                options_state["calm_days"] = int(options_state.get("calm_days", 0)) + 1
            if opt_meta.get("premium", 0) > 0:
                options_state["premium_days"] = int(options_state.get("premium_days", 0)) + 1
        if config.effective_vol_trading_enabled() and not paper_vol_live_parity:
            from modules.volatility_sleeve import run_volatility_backtest_day
            from modules.risk_parity_sleeve import pod_entries_allowed

            equity_peak = max(equity_peak, eq)
            if pod_entries_allowed(executor, "vol"):
                _, vol_meta = run_volatility_backtest_day(
                    portfolio,
                    prices,
                    bar_i=i,
                    state=vol_state,
                    volatility=vol,
                    vol_score=vol_score,
                    ts=data.index[i],
                    market_open=True,
                    portfolio_peak=equity_peak,
                )
            else:
                vol_meta = {}
            if vol_meta.get("premium", 0) > 0:
                vol_state["premium_days"] = int(vol_state.get("premium_days", 0)) + 1
        total_orders += len(executor.orders)
        trade_days += 1

        if track_metrics:
            invested = eq - portfolio.cash
            exposure_samples.append(invested / eq if eq > 0 else 0.0)
            if eq > 0:
                spy_exposure_samples.append(executor.spy_sleeve_value() / eq)
                nyse_exposure_samples.append(executor.nyse_sleeve_value() / eq)
            crypto_val = executor.crypto_sleeve_value()
            crypto_exposure_samples.append(crypto_val / eq if eq > 0 else 0.0)
            crypto_pnl_contribution += crypto_val - prev_crypto_value
            prev_crypto_value = crypto_val

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

    bench = _benchmark_return(data, MIN_HISTORY)
    perf = compute_performance_metrics(
        equity_curve,
        initial_capital=portfolio.initial_capital,
        benchmark_return_pct=bench,
        total_orders=total_orders,
        equity_index=[data.index[i].isoformat() for i in range(MIN_HISTORY, len(data))],
    )
    total_ret = perf["total_return_pct"]
    sharpe = perf["sharpe"]
    sortino = perf["sortino"]
    calmar = perf["calmar"]
    max_dd = perf["max_drawdown_pct"]
    win_rate_pct = perf["win_rate_pct"]
    rolling_sharpe_mean = perf["rolling_sharpe_mean"]
    profit_factor = perf["profit_factor"]

    pair_pnl_corr = None
    curve = pd.Series(equity_curve)
    returns = curve.pct_change().dropna()
    corr_n = min(len(pair_daily_pnl), len(returns))
    if corr_n > 2:
        pair_s = pd.Series(pair_daily_pnl[-corr_n:], index=returns.index[-corr_n:])
        aligned = pd.concat([returns.iloc[-corr_n:], pair_s], axis=1).dropna()
        if len(aligned) > 2 and aligned.iloc[:, 1].std() > 0:
            pair_pnl_corr = round(float(aligned.iloc[:, 0].corr(aligned.iloc[:, 1])), 3)

    result = {
        "start_date": str(start_date.date()),
        "end_date": str(end_date.date()),
        "sim_days": sim_days,
        "final_equity": perf["final_equity"],
        "total_return_pct": total_ret,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "max_drawdown_pct": max_dd,
        "win_rate_pct": win_rate_pct,
        "rolling_sharpe_mean": rolling_sharpe_mean,
        "profit_factor": profit_factor,
        "avg_trade_return_pct": perf.get("avg_trade_return_pct", 0.0),
        "rolling_sharpe_series": perf.get("rolling_sharpe_series"),
        "drawdown_series": perf.get("drawdown_series"),
        "benchmark_return_pct": perf.get("benchmark_return_pct"),
        "spy_signals": total_spy,
        "crypto_signals": total_crypto,
        "nyse_signals": total_equity,
        "international_signals": total_international,
        "bond_signals": total_bond,
        "total_orders": total_orders,
        "execution_cost_pct": round(
            100.0 * portfolio.execution_cost_usd / max(portfolio.initial_capital, 1.0), 3
        ),
        "regime_counts": regime_counts,
        "spy_fill": spy_fill,
        "halt_events": risk_manager.halt_events,
        "resume_events": risk_manager.resume_events,
        "pause_days": pause_days,
        "halt_liquidations": halt_liquidations,
        "vti_core_pct": round(float(np.mean(vti_core_samples)), 4)
        if vti_core_samples
        else fixed_vti_core_pct,
        "avg_active_exposure_pct": round(float(np.mean(active_exposure_samples)) * 100, 2)
        if active_exposure_samples
        else round((1.0 - fixed_vti_core_pct) * 100, 2),
        "avg_tech_exposure_pct": round(float(np.mean(tech_exposure_samples)) * 100, 2)
        if tech_exposure_samples
        else None,
        "paper_aggressive": paper_aggressive,
        "strict_pit": config.backtest_strict_pit_context(),
        "execution_costs": {
            "equity_slippage_bps": RUN_OPTIONS.equity_slippage_bps,
            "crypto_slippage_bps": RUN_OPTIONS.crypto_slippage_bps,
            "equity_commission_bps": RUN_OPTIONS.equity_commission_bps,
        },
        "paper_dynamic_vti": config.PAPER_DYNAMIC_VTI_ENABLED if paper_aggressive else False,
        "paper_dynamic_universe": config.PAPER_DYNAMIC_UNIVERSE_ENABLED if paper_aggressive else False,
        "equity_universe_size": len(config.nyse_momentum_universe(data.columns)),
        "paper_sleeve_features": config.get_paper_feature_flags() if paper_aggressive else {},
        "cofire_pct": round(100 * cofire_days / trade_days, 1) if trade_days else 0.0,
        "cofire_days": cofire_days,
        "small_account": small_account,
        "cap_scale": cap_scale,
        "equity_index": [data.index[i].isoformat() for i in range(MIN_HISTORY, len(data))],
        "equity_values": [round(v, 2) for v in equity_curve],
        "pairs_traded": pairs_traded,
        "pair_pnl_correlation": pair_pnl_corr,
    }
    ipo_stats = getattr(last_executor, "ipo_stats", None) if last_executor else None
    if ipo_stats:
        result["ipo_safety"] = dict(ipo_stats)
    pt_stats = getattr(last_executor, "profit_target_stats", None) if last_executor else None
    if pt_stats:
        result["profit_target"] = dict(pt_stats)
    sc_stats = getattr(getattr(last_executor, "portfolio", None), "scaling_strategy_stats", None)
    if sc_stats is None and last_executor is not None:
        sc_stats = getattr(last_executor, "scaling_strategy_stats", None)
    if sc_stats:
        result["scaling_strategy"] = dict(sc_stats)
    pa_stats = getattr(
        getattr(last_executor, "portfolio", None), "pattern_awareness_stats", None
    )
    if pa_stats is None and last_executor is not None:
        pa_stats = getattr(last_executor, "pattern_awareness_stats", None)
    if pa_stats:
        by_pat = pa_stats.get("by_pattern") or {}
        if hasattr(by_pat, "items") and not isinstance(by_pat, dict):
            by_pat = dict(by_pat)
        result["pattern_awareness"] = {
            **{k: v for k, v in pa_stats.items() if k != "by_pattern"},
            "by_pattern": by_pat,
        }
    vs_stats = getattr(
        getattr(last_executor, "portfolio", None), "vol_position_sizing_stats", None
    )
    if vs_stats:
        result["vol_position_sizing"] = dict(vs_stats)
    lc_stats = getattr(
        getattr(last_executor, "portfolio", None), "loss_cutting_stats", None
    )
    if lc_stats:
        result["loss_cutting"] = dict(lc_stats)
    if last_executor is not None:
        from modules.international_sleeve import international_stats_summary

        intl_stats = international_stats_summary(last_executor)
        if intl_stats.get("trades") or intl_stats.get("active_bars"):
            result["international_stats"] = intl_stats
        from modules.bond_sleeve import bond_stats_summary

        bond_stats = bond_stats_summary(last_executor)
        if bond_stats.get("trades") or bond_stats.get("active_bars"):
            result["bond_stats"] = bond_stats
    elif portfolio is not None and getattr(portfolio, "international_stats", None):
        from modules.international_sleeve import international_stats_summary

        class _PortfolioRef:
            portfolio = portfolio

        intl_stats = international_stats_summary(_PortfolioRef())
        if intl_stats.get("trades") or intl_stats.get("active_bars"):
            result["international_stats"] = intl_stats
    if macro_portfolio is not None and macro_curve:
        macro_init = macro_portfolio.initial_capital
        macro_final = round(macro_curve[-1], 2)
        result["macro_regime_sleeve"] = {
            "enabled": True,
            "initial_capital": macro_init,
            "final_equity": macro_final,
            "return_pct": round((macro_final / macro_init - 1) * 100, 2),
            "trades": total_social,
            "regime_shift_pct": round(
                100 * regime_shift_days / macro_sim_days, 1
            )
            if macro_sim_days
            else 0.0,
            "gld_target_pct": round(100 * macro_gld_days / macro_sim_days, 1)
            if macro_sim_days
            else 0.0,
            "energy_target_pct": round(100 * macro_energy_days / macro_sim_days, 1)
            if macro_sim_days
            else 0.0,
        }
    elif social_portfolio is not None and social_curve:
        social_init = social_portfolio.initial_capital
        social_final = round(social_curve[-1], 2)
        result["social_sleeve"] = {
            "enabled": True,
            "cap_pct": config.effective_social_sleeve_cap_pct(),
            "initial_capital": social_init,
            "final_equity": social_final,
            "return_pct": round((social_final / social_init - 1) * 100, 2),
            "trades": total_social,
            "gld_target_pct": round(
                100 * gld_target_days / social_sim_days, 1
            )
            if social_sim_days
            else 0.0,
            "gld_target_days": gld_target_days,
        }
    if paper_aggressive and risk_samples:
        result["dynamic_risk"] = {
            "enabled": bool(paper_dynamic_risk if paper_dynamic_risk is not None else config.PAPER_DYNAMIC_RISK_ENABLED),
            "avg_risk_pct": round(float(np.mean(risk_samples)) * 100, 2),
            "high_risk_days": high_risk_days,
            "high_risk_pct": round(100 * high_risk_days / len(risk_samples), 1),
        }
    if paper_aggressive:
        result["vol_sleeve"] = {
            "enabled": bool(
                paper_vol_trading
                if paper_vol_trading is not None
                else config.PAPER_VOL_TRADING_ENABLED
            ),
            "trades": int(vol_state.get("trades", 0)),
            "premium_collected": round(float(vol_state.get("premium_collected", 0)), 2),
            "protection_pnl": round(float(vol_state.get("protection_pnl", 0)), 2),
            "net_pnl": round(float(vol_state.get("cum_pnl", 0)), 2),
        }
    if paper_options_sleeve or options_state.get("total_premium", 0) > 0:
        result["options_sleeve"] = {
            "enabled": bool(paper_options_sleeve),
            "total_premium": round(float(options_state.get("total_premium", 0)), 2),
            "assignment_drag": round(float(options_state.get("assignment_drag", 0)), 2),
            "rolls": int(options_state.get("rolls", 0)),
            "calm_pct": round(
                100 * int(options_state.get("calm_days", 0)) / trade_days, 1
            )
            if trade_days
            else 0.0,
            "net_income": round(
                float(options_state.get("total_premium", 0))
                - float(options_state.get("assignment_drag", 0)),
                2,
            ),
        }
    if track_metrics:
        init = portfolio.initial_capital
        curve_s = pd.Series(equity_curve)
        ret_s = curve_s.pct_change().dropna()
        peak_s = curve_s.cummax()
        in_dd = curve_s.iloc[1:] < peak_s.iloc[1:]
        dd_ret = ret_s[in_dd.values] if in_dd.any() else pd.Series(dtype=float)
        result.update(
            {
                "spy_entry_signals": total_spy_entries,
                "spy_exit_signals": total_spy_exits,
                "dd_days_pct": round(100 * in_dd.sum() / len(in_dd), 1) if len(in_dd) else 0,
                "dd_avg_daily_return_bps": round(dd_ret.mean() * 10000, 2)
                if len(dd_ret) > 0
                else 0.0,
                "dd_cumulative_return_pct": round(((1 + dd_ret).prod() - 1) * 100, 2)
                if len(dd_ret) > 0
                else 0.0,
                "avg_exposure_pct": round(
                    100 * (sum(exposure_samples) / len(exposure_samples))
                    if exposure_samples
                    else 0.0,
                    1,
                ),
                "avg_crypto_exposure_pct": round(
                    100
                    * (sum(crypto_exposure_samples) / len(crypto_exposure_samples))
                    if crypto_exposure_samples
                    else 0.0,
                    1,
                ),
                "avg_spy_exposure_pct": round(
                    100
                    * (sum(spy_exposure_samples) / len(spy_exposure_samples))
                    if spy_exposure_samples
                    else 0.0,
                    1,
                ),
                "avg_nyse_exposure_pct": round(
                    100
                    * (sum(nyse_exposure_samples) / len(nyse_exposure_samples))
                    if nyse_exposure_samples
                    else 0.0,
                    1,
                ),
                "crypto_contribution_pct": round(
                    100 * crypto_pnl_contribution / init if init else 0.0, 2
                ),
            }
        )
    if thinking_cache["events"]:
        vti_drops = [
            e["vti_before"] - e["vti_after"]
            for e in thinking_cache["events"]
            if e.get("vti_before") is not None and e.get("vti_after") is not None
        ]
        tilt_stats = {
            "events": thinking_cache["events"],
            "tilt_event_count": len(thinking_cache["events"]),
            "max_vti_drop_pp": round(max(vti_drops) * 100, 2) if vti_drops else 0.0,
        }
        if simulate_live_thinking and small_account:
            result["live_thinking_sim"] = {
                **tilt_stats,
                "tilt_cap_pp": round(config.LIVE_THINKING_MAX_SLEEVE_DELTA * 100, 1),
            }
        elif paper_aggressive and config.PAPER_THINKING_ENGINE_ENABLED:
            result["thinking_tilt"] = {
                **tilt_stats,
                "tilt_cap_pp": round(
                    config.effective_thinking_max_sleeve_delta() * 100, 1
                ),
            }
    if config.effective_crypto_v2_enabled() and last_executor is not None:
        from modules.crypto_dual_sleeve import summarize_crypto_v2_trades_from_executor

        result["crypto_v2"] = summarize_crypto_v2_trades_from_executor(last_executor)
    config.set_paper_aggressive_context(saved_paper_ctx)
    config.set_backtest_paper_sleeves_context(saved_backtest_paper_sleeves)
    config.set_live_thinking_sim_context(saved_live_thinking_ctx)
    config.set_backtest_small_account_context(saved_small_ctx)
    config.SOCIAL_SLEEVE_ENABLED = saved_social
    config.PAPER_DYNAMIC_VTI_ENABLED = saved_dynamic_vti
    config.apply_paper_sleeve_flags(saved_paper_sleeve_flags)
    config.SOCIAL_MACRO_OVERRIDES_ENABLED = saved_macro_overrides
    config.PAPER_SOCIAL_MACRO_BOOST_ENABLED = saved_macro_boost
    config.PAPER_MACRO_REGIME_ADAPTOR_ENABLED = saved_paper_macro
    config.PAPER_OPTIONS_SLEEVE_ENABLED = saved_paper_options
    config.PAPER_DYNAMIC_RISK_ENABLED = saved_paper_dynamic_risk
    config.PAPER_MARKET_NEUTRAL_PAIRS = saved_paper_market_neutral_pairs
    config.PAPER_STAT_ARB_ENABLED = saved_paper_stat_arb
    config.PAPER_STAT_ARB_OPTIMIZED = saved_paper_stat_arb_opt
    config.PAPER_THINKING_ENGINE_ENABLED = saved_paper_thinking
    config.PAPER_CRYPTO_V2_ENABLED = saved_paper_crypto_v2
    config.PAPER_CRYPTO_UNIVERSE_EXPANDED = saved_paper_crypto_expanded
    config.PAPER_RISK_PARITY_ENABLED = saved_paper_risk_parity
    config.PAPER_VOL_TRADING_ENABLED = saved_paper_vol_trading
    config.PAPER_DYNAMIC_UNIVERSE_ENABLED = saved_paper_dynamic_univ
    config.PAPER_DYNAMIC_UNIVERSE_STRICT = saved_paper_dynamic_univ_strict
    config.PAPER_IPO_SAFETY_ENABLED = saved_paper_ipo_safety
    config.PAPER_INTERNATIONAL_SLEEVE_ENABLED = saved_paper_international
    config.PAPER_BOND_SLEEVE_ENABLED = saved_paper_bond
    config.PAPER_PROFIT_TARGET_ENABLED = saved_paper_profit_target
    config.PAPER_SCALING_STRATEGY_ENABLED = saved_paper_scaling
    config.PAPER_SECTOR_ROTATION_ENABLED = saved_paper_sector_rotation
    config.PAPER_PATTERN_AWARENESS_ENABLED = saved_paper_pattern_awareness
    config.PAPER_PATTERN_BEARISH_ONLY = saved_paper_pattern_bearish_only
    config.PAPER_VOL_POSITION_SIZING_ENABLED = saved_paper_vol_position_sizing
    config.PAPER_LOSS_CUTTING_ENABLED = saved_paper_loss_cutting
    set_vol_sizing_conservative(saved_top1_vol_conservative)
    set_loss_cutting_conservative(saved_top1_loss_conservative)
    set_sector_rotation_conservative(saved_top1_sector_rotation_conservative)
    config.SECTOR_ROTATION_HYBRID_MODE = saved_sector_rotation_hybrid
    config.SECTOR_ROTATION_CONSERVATIVE_MODE = saved_sector_rotation_conservative_cfg
    config.set_backtest_strict_pit_context(saved_strict_pit)
    from modules.pit_replay import clear_pit_bar_context, reset_pit_caches

    clear_pit_bar_context()
    if not saved_strict_pit:
        reset_pit_caches()
    config.SECTOR_ROTATION_ENABLED = saved_sector_rotation
    config.PAPER_TECH_GUARD_ENABLED = saved_paper_tech_guard
    config.set_backtest_international_prefetch(saved_intl_prefetch)
    config.set_backtest_bond_prefetch(saved_bond_prefetch)
    store_last_result(result)
    return result


def run_options_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare paper aggressive with vs without options income sleeve."""
    from modules.options_sleeve import ensure_vix_daily

    ensure_vix_daily()
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    configs = [
        (
            "Paper (no options sleeve)",
            {
                "paper_aggressive": True,
                "paper_sleeve_features": True,
                "paper_dynamic_vti": True,
                "paper_options_sleeve": False,
            },
        ),
        (
            "Paper (+ Options Income Sleeve)",
            {
                "paper_aggressive": True,
                "paper_sleeve_features": True,
                "paper_dynamic_vti": True,
                "paper_options_sleeve": True,
            },
        ),
    ]
    print("--- OPTIONS INCOME SLEEVE A/B (covered calls on VTI/SPY, calm regime) ---")
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        f"{'Config':<34} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Premium':>9} {'NetInc':>8} {'Rolls':>6}"
    )
    print("-" * 88)

    for label, kwargs in configs:
        result = run_backtest(data, track_active_exposure=True, track_metrics=True, **kwargs)
        opt = result.get("options_sleeve") or {}
        prem = f"${opt.get('total_premium', 0):,.0f}" if opt else "—"
        net = f"${opt.get('net_income', 0):,.0f}" if opt else "—"
        rolls = opt.get("rolls", 0) if opt else 0
        print(
            f"{label:<34} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{prem:>9} "
            f"{net:>8} "
            f"{rolls:>6}"
        )
    print("-" * 88)


def run_dynamic_risk_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare paper aggressive fixed 2%% risk vs dynamic vol/regime/stress scaling."""
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    configs = [
        (
            "Paper (fixed risk)",
            {
                "paper_aggressive": True,
                "paper_sleeve_features": True,
                "paper_dynamic_vti": True,
                "paper_dynamic_risk": False,
            },
        ),
        (
            "Paper (+ Dynamic Risk)",
            {
                "paper_aggressive": True,
                "paper_sleeve_features": True,
                "paper_dynamic_vti": True,
                "paper_dynamic_risk": True,
            },
        ),
    ]
    print("--- DYNAMIC RISK SCALING A/B (paper aggressive; cap 3%% calm bull) ---")
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        f"{'Config':<28} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'AvgRisk':>8} {'HiRisk%':>8} {'HiDays':>7}"
    )
    print("-" * 82)

    for label, kwargs in configs:
        result = run_backtest(data, track_active_exposure=True, track_metrics=True, **kwargs)
        dr = result.get("dynamic_risk") or {}
        avg_r = f"{dr.get('avg_risk_pct', config.RISK_PER_TRADE * 100):.2f}%"
        hi_pct = f"{dr.get('high_risk_pct', 0):.1f}%"
        hi_days = dr.get("high_risk_days", 0)
        print(
            f"{label:<28} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{avg_r:>8} "
            f"{hi_pct:>8} "
            f"{hi_days:>7}"
        )
    print("-" * 82)


def run_vol_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare paper aggressive with vs without volatility trading overlay."""
    from modules.options_sleeve import ensure_vix_daily

    ensure_vix_daily()
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    base_kwargs = {
        "paper_aggressive": True,
        "paper_sleeve_features": True,
        "paper_dynamic_vti": True,
        "paper_dynamic_risk": True,
        "paper_market_neutral_pairs": True,
    }
    configs = [
        ("Paper (no vol overlay)", {**base_kwargs, "paper_vol_trading": False}),
        ("Paper (+ Vol Overlay)", {**base_kwargs, "paper_vol_trading": True}),
    ]
    print("--- VOLATILITY OVERLAY A/B (VIX regime; paper aggressive only) ---")
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        f"{'Config':<28} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Trades':>7} {'Premium':>9} {'Protect':>9}"
    )
    print("-" * 86)

    baseline = None
    for label, kwargs in configs:
        result = run_backtest(data, track_active_exposure=True, track_metrics=True, **kwargs)
        if kwargs.get("paper_vol_trading") is False:
            baseline = result
        vs = result.get("vol_sleeve") or {}
        prem = f"${vs.get('premium_collected', 0):,.0f}"
        prot = f"${vs.get('protection_pnl', 0):,.0f}"
        print(
            f"{label:<28} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{vs.get('trades', 0):>7} "
            f"{prem:>9} "
            f"{prot:>9}"
        )
    print("-" * 86)
    if baseline:
        print(
            f"Baseline (current paper w/o vol): "
            f"{baseline['total_return_pct']:+.2f}% return, "
            f"Sharpe {baseline['sharpe']:.2f}, "
            f"MaxDD {baseline['max_drawdown_pct']:.2f}%"
        )


def run_stat_arb_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare paper aggressive with vs without statistical arbitrage sleeve."""
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    base_kwargs = {
        "paper_aggressive": True,
        "paper_sleeve_features": True,
        "paper_dynamic_vti": True,
        "paper_dynamic_risk": True,
        "paper_vol_trading": True,
    }
    configs = [
        (
            "Paper (no stat arb)",
            {
                **base_kwargs,
                "paper_stat_arb": False,
                "paper_market_neutral_pairs": False,
            },
        ),
        (
            "Paper (+ Stat Arb)",
            {**base_kwargs, "paper_stat_arb": True},
        ),
    ]
    print(
        "--- STAT ARB A/B (cointegration, corr>0.75, Z>=2.5, exit 0.5; paper aggressive) ---"
    )
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        f"{'Config':<28} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Pairs':>6} {'Corr':>6}"
    )
    print("-" * 78)

    baseline = None
    for label, kwargs in configs:
        result = run_backtest(data, track_active_exposure=True, track_metrics=True, **kwargs)
        if kwargs.get("paper_stat_arb") is False:
            baseline = result
        pairs = result.get("pairs_traded", 0)
        corr = result.get("pair_pnl_correlation")
        corr_s = f"{corr:.2f}" if corr is not None else "—"
        print(
            f"{label:<28} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{pairs:>6} "
            f"{corr_s:>6}"
        )
    print("-" * 78)
    if baseline:
        print(
            f"Baseline (current paper w/o stat arb): "
            f"{baseline['total_return_pct']:+.2f}% return, "
            f"Sharpe {baseline['sharpe']:.2f}, "
            f"MaxDD {baseline['max_drawdown_pct']:.2f}%"
        )


def run_stat_arb_optimized_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare current stat arb vs optimized stat arb (paper aggressive stack)."""
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
        label = "max"
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
        label = f"{days}d"
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    base_kwargs = {
        "paper_aggressive": True,
        "paper_sleeve_features": True,
        "paper_dynamic_vti": True,
        "paper_dynamic_risk": True,
        "paper_vol_trading": True,
        "paper_options_sleeve": True,
        "paper_macro_regime": True,
        "paper_stat_arb": True,
    }
    configs = [
        (
            "Stat Arb (current)",
            {**base_kwargs, "paper_stat_arb_optimized": False},
        ),
        (
            "Stat Arb (optimized)",
            {**base_kwargs, "paper_stat_arb_optimized": True},
        ),
    ]
    print(
        "--- STAT ARB OPTIMIZED A/B (Kalman/decay/dynamic Z/profit+time exit) ---"
    )
    print(
        f"Window ({label}): {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        f"{'Config':<26} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Pairs':>6} {'Corr':>6}"
    )
    print("-" * 78)

    current = None
    optimized = None
    for label_cfg, kwargs in configs:
        result = run_backtest(
            data, track_active_exposure=True, track_metrics=True, **kwargs
        )
        if kwargs.get("paper_stat_arb_optimized"):
            optimized = result
        else:
            current = result
        pairs = result.get("pairs_traded", 0)
        corr = result.get("pair_pnl_correlation")
        corr_s = f"{corr:.2f}" if corr is not None else "—"
        print(
            f"{label_cfg:<26} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{pairs:>6} "
            f"{corr_s:>6}"
        )
    print("-" * 78)
    if current and optimized:
        d_ret = optimized["total_return_pct"] - current["total_return_pct"]
        d_sh = optimized["sharpe"] - current["sharpe"]
        d_dd = optimized["max_drawdown_pct"] - current["max_drawdown_pct"]
        d_pairs = optimized.get("pairs_traded", 0) - current.get("pairs_traded", 0)
        c0 = current.get("pair_pnl_correlation")
        c1 = optimized.get("pair_pnl_correlation")
        d_corr = (c1 - c0) if c0 is not None and c1 is not None else None
        print(
            f"Optimized vs current: return {d_ret:+.2f}pp | Sharpe {d_sh:+.2f} | "
            f"MaxDD {d_dd:+.2f}pp | pairs {d_pairs:+d}"
            + (f" | corr {d_corr:+.2f}" if d_corr is not None else "")
        )


def run_thinking_compare(
    days=None,
    refresh=False,
    use_max=False,
    *,
    with_news: bool = False,
) -> None:
    """Compare paper aggressive with vs without thinking-engine sleeve tilts."""
    from modules.macro_regime_adaptor import ensure_macro_regime_daily

    ensure_macro_regime_daily()
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
        label = "max"
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
        label = f"{days}d"
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    base_kwargs = {
        "paper_aggressive": True,
        "paper_sleeve_features": True,
        "paper_dynamic_vti": True,
        "paper_dynamic_risk": True,
        "paper_vol_trading": True,
        "paper_options_sleeve": True,
        "paper_macro_regime": False,
        "paper_stat_arb": True,
    }
    if with_news:
        configs = [
            ("Paper (no thinking)", {**base_kwargs, "paper_thinking": False, "with_news": False}),
            (
                "Paper (+ thinking + news)",
                {**base_kwargs, "paper_thinking": True, "with_news": True},
            ),
        ]
        title = "--- THINKING + NEWS A/B (Best Paper v2.1, practical guards) ---"
    else:
        configs = [
            ("Paper (no thinking tilt)", {**base_kwargs, "paper_thinking": False}),
            ("Paper (+ thinking tilt)", {**base_kwargs, "paper_thinking": True}),
        ]
        title = (
            "--- THINKING ENGINE A/B (force-decision heuristic tilt; paper aggressive) ---"
        )
    print(title)
    print(
        f"Window ({label}): {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if with_news:
        print(
            "Guards: max 3 sleeves | news impact gates | neutral regime dampening | "
            "Why this tilt? in logs"
        )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    if with_news:
        print(
            f"{'Config':<32} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} {'Tilts':>6}"
        )
        print("-" * 70)
    else:
        print(f"{'Config':<28} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8}")
        print("-" * 58)

    baseline = None
    with_thinking = None
    for label_cfg, kwargs in configs:
        result = run_backtest(data, track_active_exposure=True, track_metrics=True, **kwargs)
        release_backtest_memory()
        if kwargs.get("paper_thinking"):
            with_thinking = result
        else:
            baseline = result
        if with_news:
            print(
                f"{label_cfg:<32} "
                f"{result['total_return_pct']:>+7.2f}% "
                f"{result['sharpe']:>7.2f} "
                f"{result['max_drawdown_pct']:>7.2f}% "
                f"{_thinking_tilt_event_count(result):>6d}"
            )
        else:
            print(
                f"{label_cfg:<28} "
                f"{result['total_return_pct']:>+7.2f}% "
                f"{result['sharpe']:>7.2f} "
                f"{result['max_drawdown_pct']:>7.2f}%"
            )
    if with_news:
        print("-" * 70)
    else:
        print("-" * 58)
    if baseline and with_thinking:
        sim = with_thinking.get("live_thinking_sim") or {}
        print(
            f"Thinking vs baseline: return "
            f"{with_thinking['total_return_pct'] - baseline['total_return_pct']:+.2f}pp | "
            f"Sharpe {with_thinking['sharpe'] - baseline['sharpe']:+.2f} | "
            f"MaxDD {with_thinking['max_drawdown_pct'] - baseline['max_drawdown_pct']:+.2f}pp"
        )
        if with_news:
            print(
                f"Tilt events: {_thinking_tilt_event_count(with_thinking)} | "
                f"avg magnitude {_avg_tilt_magnitude(with_thinking):.3f}"
            )
            if (
                with_thinking["sharpe"] >= baseline["sharpe"]
                and with_thinking["total_return_pct"] >= baseline["total_return_pct"] - 0.5
            ):
                print(
                    "Paper recommendation: enable PAPER_THINKING_ENGINE_ENABLED=true "
                    "(opt-in); keep live OFF until paper logs match."
                )
            else:
                print(
                    "Paper recommendation: keep thinking OFF by default; "
                    "re-test after further tuning."
                )

    from modules.thinking_engine import (
        apply_thinking_tilt_to_caps,
        build_backtest_thinking_result,
    )
    from modules.wisdom_sentiment import resolve_wisdom_regime

    window = data.iloc[-60:]
    wisdom = resolve_wisdom_regime(window)
    sample = build_backtest_thinking_result(
        window, wisdom["regime"], wisdom["volatility"]
    )
    base_caps = config.fund_allocation_pct()
    _, deltas, sample_log = apply_thinking_tilt_to_caps(
        base_caps,
        sample["suggested_tilt"],
        confidence=sample["confidence"],
        market_summary=sample["market_summary"],
    )
    print("\nSample reasoning (most recent bar, heuristic backtest proxy):")
    print(f"  {sample['reasoning']}")
    print(f"  Apply: {sample_log}")
    if deltas:
        shown = {k: round(v, 4) for k, v in deltas.items() if abs(v) > 0.001}
        print(f"  Deltas: {shown}")

    # If Ollama is available, attempt to fetch 2-3 real LLM reasoning examples
    try:
        from modules.thinking_engine import ollama_available, get_market_reasoning

        if ollama_available():
            print("\nAttempting to fetch up to 3 real LLM reasoning examples (may require local Ollama)...")
            # sample the last 3 non-overlapping windows (recent bars)
            examples = []
            for offset in (2, 5, 10):
                window = data.iloc[-(offset + 1) : -offset]
                if window.empty:
                    continue
                # reuse wisdom/regime resolution from above
                from modules.wisdom_sentiment import resolve_wisdom_regime

                w = resolve_wisdom_regime(window)
                try:
                    ex = get_market_reasoning(
                        {
                            "spy_trend": w.get("spy_trend", "n/a"),
                            "vix": w.get("vix", "n/a"),
                            "oil_change": w.get("oil_change", 0.0),
                            "gold_change": w.get("gold_change", 0.0),
                            "macro_sentiment": w.get("macro_sentiment", "n/a"),
                            "top_headline": w.get("top_headline", "n/a"),
                            "regime": w.get("regime"),
                        }
                    )
                except Exception as exc:
                    print(f"  LLM call failed: {exc}")
                    break
                examples.append(ex)
                if len(examples) >= 3:
                    break
            for i, ex in enumerate(examples, 1):
                print(f"\nLLM Example {i}: conf {ex.get('confidence'):.2f}")
                print(f"  Narrative: {ex.get('narrative')}")
                sample = (ex.get('reasoning') or '').splitlines()[:6]
                for ln in sample:
                    if ln.strip():
                        print(f"    {ln}")
                print(f"  Suggested tilt: {ex.get('suggested_tilt')}")
    except Exception:
        pass


def run_sector_rotation_compare(
    days=None,
    refresh=False,
    use_max=False,
    *,
    with_news: bool = False,
) -> None:
    """Compare conservative blend vs +conservative sector rotation vs full hybrid."""
    from modules.macro_regime_adaptor import ensure_macro_regime_daily

    ensure_macro_regime_daily()
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
        label = "max"
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
        label = f"{days}d"
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    blend_base = {
        **BLENDED_CONSERVATIVE_COMPARE_KWARGS,
        "paper_vol_position_sizing": True,
        "paper_loss_cutting": True,
        "top1_vol_conservative": True,
        "top1_loss_conservative": True,
        "paper_sector_rotation": False,
        "strict_pit": True,
        "paper_thinking": True,
        "with_news": True,
    }
    configs = [
        (
            "Conservative blend (no rotation)",
            dict(blend_base),
        ),
        (
            "Blend + conservative rotation",
            {
                **blend_base,
                "paper_sector_rotation": True,
                "top1_sector_rotation_conservative": True,
                "paper_sector_rotation_hybrid": False,
            },
        ),
        (
            "Blend + full hybrid rotation",
            {
                **blend_base,
                "paper_sector_rotation": True,
                "top1_sector_rotation_conservative": False,
                "paper_sector_rotation_hybrid": True,
            },
        ),
    ]

    print("--- SECTOR ROTATION A/B (conservative blend + strict PIT + thinking + news) ---")
    print(
        f"Window ({label}): {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        "Conservative rotation: validated macro regimes (defense/space, oil, AI, rate cuts) | "
        f"boost {config.SECTOR_ROTATION_CONSERVATIVE_BOOST:.0%} / "
        f"trim {config.SECTOR_ROTATION_CONSERVATIVE_TRIM:.0%} | "
        f"max {config.SECTOR_ROTATION_CONSERVATIVE_MAX_SECTORS} sector | "
        f"sleeve delta cap {config.SECTOR_ROTATION_CONSERVATIVE_MAX_DELTA:.0%} | "
        "no tech trim on defense unless lagging"
    )
    print(
        "Full hybrid: rules + thinking bias | "
        f"boost {config.SECTOR_ROTATION_SCORE_BOOST:.0%} / "
        f"trim {config.SECTOR_ROTATION_SCORE_TRIM:.0%} | "
        f"max {config.SECTOR_ROTATION_MAX_ACTIVE_SECTORS} sectors"
    )
    print(
        f"{'Config':<32} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Trades':>7} {'Tilts':>6}"
    )
    print("-" * 76)

    results: list[tuple[str, dict]] = []
    for label_cfg, kwargs in configs:
        result = run_backtest(data, track_metrics=True, **kwargs)
        results.append((label_cfg, result))
        release_backtest_memory()
        print(
            f"{label_cfg:<32} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{result.get('nyse_signals', 0):>7} "
            f"{_thinking_tilt_event_count(result):>6d}"
        )

    print("-" * 76)
    if len(results) >= 2:
        base_label, base_r = results[0]
        by_label = {lbl: res for lbl, res in results}
        cons_r = by_label.get("Blend + conservative rotation")
        hybrid_r = by_label.get("Blend + full hybrid rotation")

        print("\nDelta vs conservative blend (no rotation):")
        for label_cfg, res in results[1:]:
            print(
                f"  {label_cfg}: "
                f"return {res['total_return_pct'] - base_r['total_return_pct']:+.2f}pp | "
                f"Sharpe {res['sharpe'] - base_r['sharpe']:+.2f} | "
                f"MaxDD {res['max_drawdown_pct'] - base_r['max_drawdown_pct']:+.2f}pp | "
                f"Trades {res.get('nyse_signals', 0) - base_r.get('nyse_signals', 0):+d}"
            )

        if cons_r:
            beats_base = (
                cons_r["total_return_pct"] >= base_r["total_return_pct"] - 0.5
                and cons_r["sharpe"] >= base_r["sharpe"] - 0.02
            )
            if beats_base and (
                not hybrid_r
                or cons_r["sharpe"] >= hybrid_r["sharpe"] - 0.02
            ):
                print(
                    "\nPaper recommendation: ENABLE conservative sector rotation "
                    "(PAPER_SECTOR_ROTATION_ENABLED=true + "
                    "SECTOR_ROTATION_CONSERVATIVE_MODE=true) on top of Top1 conservative blend. "
                    "Keep full hybrid OFF."
                )
            elif cons_r and hybrid_r and hybrid_r["total_return_pct"] > cons_r["total_return_pct"] + 1:
                print(
                    "\nPaper recommendation: conservative rotation mixed; full hybrid led return "
                    "but may over-trade — validate conservative mode on paper first."
                )
            else:
                print(
                    "\nPaper recommendation: KEEP sector rotation OFF on paper — "
                    "no clear edge vs conservative blend on this window."
                )

    try:
        from modules.sector_rotation import build_rotation_narrative, demo_spacex_rotation

        spacex = demo_spacex_rotation()
        print("\n--- Macro narrative example (defense/space theme) ---")
        print(f"  {build_rotation_narrative(spacex)}")
        print(f"  Favored: {', '.join(sorted(spacex.favored)) or 'none'}")
        print(f"  Trimmed: {', '.join(sorted(spacex.trimmed)) or 'none'}")
    except Exception:
        pass
    print("-" * 76)


def run_thinking_impact_compare(
    days=None,
    refresh=False,
    use_max=False,
) -> None:
    """Isolate Thinking Engine + news impact on conservative blend (strict PIT)."""
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
        win_label = "max"
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
        win_label = f"{days}d"
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    blend_base = dict(CONSERVATIVE_BLEND_KWARGS)
    configs = [
        (
            "Blend + thinking + news ON",
            {**blend_base, "paper_thinking": True, "with_news": True},
        ),
        (
            "Blend + thinking + news OFF",
            {**blend_base, "paper_thinking": False, "with_news": False},
        ),
    ]

    print("--- THINKING ENGINE IMPACT A/B (conservative blend + strict PIT) ---")
    print(
        f"Window ({win_label}): {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    print(
        "Prior best (pre-quality pass): +59.60% return | 2.05 Sharpe | 134 tilt events"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        "Held constant: Top1 conservative vol sizing (spec 0.5% + mild ATR) | "
        "spec -4% stop only | sector rotation OFF | patterns OFF"
    )
    print(
        "Variable: Thinking Engine sleeve tilts + synthetic news digest "
        "(PAPER_THINKING_ENGINE_ENABLED / with_news)"
    )
    print(
        f"{'Config':<32} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Trades':>7} {'Tilts':>6} {'TiltMag':>8}"
    )
    print("-" * 84)

    results: list[tuple[str, dict]] = []
    for label_cfg, kwargs in configs:
        result = run_backtest(data, track_metrics=True, **kwargs)
        results.append((label_cfg, result))
        release_backtest_memory()
        print(
            f"{label_cfg:<32} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{result.get('nyse_signals', 0):>7} "
            f"{_thinking_tilt_event_count(result):>6d} "
            f"{_avg_tilt_magnitude(result):>8.3f}"
        )

    print("-" * 84)
    if len(results) == 2:
        (_, on_r), (_, off_r) = results
        print(
            "\nDelta (thinking ON - OFF): "
            f"return {on_r['total_return_pct'] - off_r['total_return_pct']:+.2f}pp | "
            f"Sharpe {on_r['sharpe'] - off_r['sharpe']:+.2f} | "
            f"MaxDD {on_r['max_drawdown_pct'] - off_r['max_drawdown_pct']:+.2f}pp | "
            f"Trades {on_r.get('nyse_signals', 0) - off_r.get('nyse_signals', 0):+d} | "
            f"tilts {_thinking_tilt_event_count(on_r) - _thinking_tilt_event_count(off_r):+d}"
        )
        thinking_helps = (
            on_r["total_return_pct"] >= off_r["total_return_pct"] - 0.5
            and on_r["sharpe"] >= off_r["sharpe"] - 0.02
        )
        thinking_hurts = (
            on_r["total_return_pct"] < off_r["total_return_pct"] - 2.0
            or on_r["sharpe"] < off_r["sharpe"] - 0.05
        )
        if thinking_helps and not thinking_hurts:
            print(
                "\nRecommendation: KEEP Thinking + News ON for paper conservative blend "
                "(PAPER_THINKING_ENGINE_ENABLED=true). Tilts add edge or match heuristic-only."
            )
        elif thinking_hurts:
            print(
                "\nRecommendation: DISABLE Thinking on paper for conservative blend "
                "(PAPER_THINKING_ENGINE_ENABLED=false) — sleeve tilts hurt vs heuristic-only."
            )
        else:
            print(
                "\nRecommendation: OPTIONAL — Thinking impact is modest on this window; "
                "keep ON if you value macro narrative / tilt audit logs."
            )
    print("-" * 84)


def run_tech_guard_compare(
    days=None,
    refresh=False,
    use_max=False,
    *,
    with_news: bool = False,
) -> None:
    """Compare paper aggressive with vs without tech concentration guard."""
    from modules.macro_regime_adaptor import ensure_macro_regime_daily

    ensure_macro_regime_daily()
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
        label = "max"
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
        label = f"{days}d"
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    base_kwargs = {
        "paper_aggressive": True,
        "paper_sleeve_features": True,
        "paper_dynamic_vti": True,
        "paper_dynamic_risk": True,
        "paper_vol_trading": True,
        "paper_options_sleeve": True,
        "paper_macro_regime": False,
        "paper_stat_arb": True,
        "paper_sector_rotation": False,
    }
    if with_news:
        base_kwargs["paper_thinking"] = True
        base_kwargs["with_news"] = True
        title = "--- TECH GUARD A/B (paper + thinking + news) ---"
    else:
        base_kwargs["paper_thinking"] = True
        title = "--- TECH GUARD A/B (paper aggressive + thinking) ---"
    configs = [
        (
            "Paper (no tech guard)",
            {**base_kwargs, "paper_tech_guard": False},
        ),
        (
            "Paper (+ tech guard)",
            {**base_kwargs, "paper_tech_guard": True},
        ),
    ]
    print(title)
    print(
        f"Window ({label}): {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    print(
        f"Guard: limit {config.TECH_CONCENTRATION_LIMIT:.0%} tech exposure | "
        f"max +{config.TECH_GUARD_MAX_SPY_TILT:.0%} SPY tilt when heavy | "
        f"skip tech NYSE buys + boost non-tech"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        f"{'Config':<28} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'AvgTech%':>9} {'Tilts':>6}"
    )
    print("-" * 72)

    baseline = None
    with_guard = None
    for label_cfg, kwargs in configs:
        from modules.tech_concentration_guard import guard_stats, reset_guard_stats

        reset_guard_stats()
        result = run_backtest(data, track_active_exposure=True, track_metrics=True, **kwargs)
        stats = guard_stats()
        result["tech_guard_stats"] = stats
        release_backtest_memory()
        if kwargs.get("paper_tech_guard"):
            with_guard = result
        else:
            baseline = result
        tech_avg = result.get("avg_tech_exposure_pct")
        tech_s = f"{tech_avg:.1f}%" if tech_avg is not None else "—"
        print(
            f"{label_cfg:<28} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{tech_s:>9} "
            f"{_thinking_tilt_event_count(result):>6d}"
        )
    print("-" * 72)
    if baseline and with_guard:
        b_tech = baseline.get("avg_tech_exposure_pct")
        g_tech = with_guard.get("avg_tech_exposure_pct")
        print(
            f"Tech guard vs baseline: return "
            f"{with_guard['total_return_pct'] - baseline['total_return_pct']:+.2f}pp | "
            f"Sharpe {with_guard['sharpe'] - baseline['sharpe']:+.2f} | "
            f"MaxDD {with_guard['max_drawdown_pct'] - baseline['max_drawdown_pct']:+.2f}pp"
        )
        if b_tech is not None and g_tech is not None:
            print(f"Avg tech exposure: {b_tech:.1f}% -> {g_tech:.1f}% ({g_tech - b_tech:+.1f}pp)")
        gs = with_guard.get("tech_guard_stats") or {}
        if gs:
            print(
                f"Guard actions (with guard): rank reorders {gs.get('rank_reorders', 0)} | "
                f"NYSE tech skips {gs.get('nyse_skips', 0)} | tilt clamps {gs.get('tilt_clamps', 0)}"
            )
        delta_ret = with_guard["total_return_pct"] - baseline["total_return_pct"]
        if abs(delta_ret) < 0.01 and not any(gs.values()):
            print(
                "Note: guard did not activate in this window (tech exposure stayed below 45% limits)."
            )
        elif (
            with_guard["sharpe"] >= baseline["sharpe"] - 0.05
            and with_guard["total_return_pct"] >= baseline["total_return_pct"] - 2.0
        ):
            print(
                "Paper recommendation: keep PAPER_TECH_GUARD_ENABLED=true — "
                "reduces tech concentration without large return drag."
            )
        else:
            print(
                "Paper recommendation: tech guard helps risk mix; evaluate return trade-off "
                "before live. Live $300: keep OFF (TECH_CONCENTRATION_LIVE_ENABLED=false)."
            )


def _crypto_sleeve_stats(result: dict) -> dict:
    v2 = result.get("crypto_v2") or {}
    if v2.get("trade_count"):
        return {
            "label": "crypto_v2",
            "trades": v2["trade_count"],
            "win_rate_pct": v2.get("win_rate_pct", 0.0),
            "avg_pnl_pct": v2.get("avg_pnl_pct", 0.0),
            "mean_reversion": v2.get("mean_reversion_trades", 0),
            "breakout": v2.get("breakout_trades", 0),
            "samples": v2.get("samples") or [],
        }
    return {
        "label": "stat_arb",
        "trades": result.get("crypto_signals", 0),
        "pairs_traded": result.get("pairs_traded", 0),
        "win_rate_pct": None,
        "avg_pnl_pct": None,
        "mean_reversion": None,
        "breakout": None,
        "samples": [],
    }


def run_compare_crypto_v2(days=None, refresh=False, use_max=False) -> None:
    """Compare current stat-arb crypto sleeve vs dual-entry crypto v2 (paper aggressive)."""
    from modules.macro_regime_adaptor import ensure_macro_regime_daily

    ensure_macro_regime_daily()
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
        label = "max"
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
        label = f"{days}d"
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    from modules.crypto_dual_sleeve import crypto_v2_universe

    uni = crypto_v2_universe(data.columns)
    base_kwargs = {
        "paper_aggressive": True,
        "paper_sleeve_features": True,
        "paper_dynamic_vti": True,
        "paper_dynamic_risk": True,
        "paper_stat_arb": True,
        "paper_vol_trading": True,
        "paper_options_sleeve": True,
        "paper_macro_regime": False,
    }
    configs = [
        ("Current (stat-arb crypto)", {**base_kwargs, "paper_crypto_v2": False}),
        ("Crypto v2 (MR + breakout)", {**base_kwargs, "paper_crypto_v2": True, "paper_stat_arb": True}),
    ]
    print("--- CRYPTO SLEEVE V2 A/B (paper aggressive; live unchanged) ---")
    print(
        f"Window ({label}): {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars) | v2 universe in data: {len(uni)} symbols"
    )
    print(
        f"{'Config':<30} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Trades':>7} {'Win%':>6} {'AvgPnL':>7}"
    )
    print("-" * 86)

    saved_social = config.SOCIAL_SLEEVE_ENABLED
    config.SOCIAL_SLEEVE_ENABLED = False
    results: list[tuple[str, dict, dict]] = []
    try:
        for label_cfg, kwargs in configs:
            result = run_backtest(
                data, track_active_exposure=True, track_metrics=True, **kwargs
            )
            stats = _crypto_sleeve_stats(result)
            results.append((label_cfg, result, stats))
            win = (
                f"{stats['win_rate_pct']:>5.1f}%"
                if stats["win_rate_pct"] is not None
                else "   n/a"
            )
            avg = (
                f"{stats['avg_pnl_pct']:>+6.2f}%"
                if stats["avg_pnl_pct"] is not None
                else "    n/a"
            )
            print(
                f"{label_cfg:<30} "
                f"{result['total_return_pct']:>+7.2f}% "
                f"{result['sharpe']:>7.2f} "
                f"{result['max_drawdown_pct']:>7.2f}% "
                f"{stats['trades']:>7d} "
                f"{win:>6} "
                f"{avg:>7}"
            )
    finally:
        config.SOCIAL_SLEEVE_ENABLED = saved_social
    print("-" * 86)

    if len(results) == 2:
        _base_label, base, base_stats = results[0]
        v2_label, v2, v2_stats = results[1]
        print(
            f"v2 vs current: return {v2['total_return_pct'] - base['total_return_pct']:+.2f}pp | "
            f"Sharpe {v2['sharpe'] - base['sharpe']:+.2f} | "
            f"MaxDD {v2['max_drawdown_pct'] - base['max_drawdown_pct']:+.2f}pp"
        )
        if v2_stats.get("mean_reversion") is not None:
            print(
                f"Crypto v2 entries: mean-reversion {v2_stats['mean_reversion']} | "
                f"breakout {v2_stats['breakout']}"
            )
        samples = v2_stats.get("samples") or []
        if samples:
            print("\nSample crypto v2 trades:")
            for t in samples[:4]:
                kind = t.get("entry_type", "?")
                sym = t.get("symbol", "?")
                pnl = t.get("pnl_pct", 0)
                reason = t.get("exit_reason", "")
                extra = ""
                if kind == "mean_reversion" and "rsi" in t:
                    extra = f" RSI={t['rsi']}"
                elif kind == "breakout" and "range_spike" in t:
                    extra = f" spike={t['range_spike']}x"
                print(f"  [{kind}] {sym} {pnl:+.2f}% ({reason}){extra}")


def run_compare_crypto_universe(days=None, refresh=False, use_max=False) -> None:
    """Compare base (24-pair) vs expanded Alpaca crypto universe (paper aggressive)."""
    from modules.crypto_universe import crypto_trading_columns, prefetch_expanded_crypto_history
    from modules.macro_regime_adaptor import ensure_macro_regime_daily

    ensure_macro_regime_daily()
    days = days or config.BACKTEST_DAYS

    saved_exp = config.PAPER_CRYPTO_UNIVERSE_EXPANDED
    saved_prefetch = config.backtest_crypto_expanded_prefetch()
    config.PAPER_CRYPTO_UNIVERSE_EXPANDED = True
    config.set_backtest_crypto_expanded_prefetch(True)
    try:
        prefetch_expanded_crypto_history(days=days, refresh=refresh, use_max=use_max)
        data = _ensure_daily_data(days, refresh=refresh, use_max=use_max)
    finally:
        config.PAPER_CRYPTO_UNIVERSE_EXPANDED = saved_exp
        config.set_backtest_crypto_expanded_prefetch(saved_prefetch)

    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    if use_max:
        label = "max"
    else:
        label = f"{days}d"

    base_n = len(crypto_trading_columns(data, expanded=False))
    exp_n = len(crypto_trading_columns(data, expanded=True))
    base_kwargs = {
        "paper_aggressive": True,
        "paper_sleeve_features": True,
        "paper_dynamic_vti": True,
        "paper_dynamic_risk": True,
        "paper_stat_arb": True,
        "paper_vol_trading": True,
        "paper_macro_regime": False,
        "paper_crypto_v2": False,
        "track_metrics": True,
    }
    configs = [
        (f"Base crypto ({base_n} symbols)", {**base_kwargs, "paper_crypto_universe_expanded": False}),
        (
            f"Expanded Alpaca ({exp_n} symbols)",
            {**base_kwargs, "paper_crypto_universe_expanded": True},
        ),
    ]

    print("--- CRYPTO UNIVERSE EXPANDED A/B (paper aggressive; live unchanged) ---")
    print(
        f"Window ({label}): {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars) | vol gate + stat-arb MR unchanged"
    )
    print(
        f"{'Config':<32} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Crypto':>7} {'Pairs':>6}"
    )
    print("-" * 78)

    saved_social = config.SOCIAL_SLEEVE_ENABLED
    config.SOCIAL_SLEEVE_ENABLED = False
    results: list[tuple[str, dict]] = []
    try:
        for label_cfg, kwargs in configs:
            result = run_backtest(data, track_active_exposure=True, **kwargs)
            results.append((label_cfg, result))
            print(
                f"{label_cfg:<32} "
                f"{result['total_return_pct']:>+7.2f}% "
                f"{result['sharpe']:>7.2f} "
                f"{result['max_drawdown_pct']:>7.2f}% "
                f"{result.get('crypto_signals', 0):>7d} "
                f"{result.get('pairs_traded', 0):>6d}"
            )
            release_backtest_memory()
    finally:
        config.SOCIAL_SLEEVE_ENABLED = saved_social
    print("-" * 78)

    if len(results) == 2:
        base_label, base = results[0]
        exp_label, exp = results[1]
        base_trades = int(base.get("crypto_signals") or 0)
        exp_trades = int(exp.get("crypto_signals") or 0)
        freq_delta = exp_trades - base_trades
        print(
            f"Expanded vs base: return {exp['total_return_pct'] - base['total_return_pct']:+.2f}pp | "
            f"Sharpe {exp['sharpe'] - base['sharpe']:+.2f} | "
            f"MaxDD {exp['max_drawdown_pct'] - base['max_drawdown_pct']:+.2f}pp | "
            f"crypto trades {freq_delta:+d} ({base_trades} -> {exp_trades})"
        )
        print("\n--- Live $300 Profile A recommendation ---")
        if exp_trades > base_trades and exp["sharpe"] >= base["sharpe"] - 0.05:
            print(
                "OPTIONAL on paper only — keep PAPER_CRYPTO_UNIVERSE_EXPANDED=true on Profile B. "
                "Do NOT enable on live $300: crypto sleeve is disabled on Profile A; "
                "expanded pairs add fee/slippage surface without live execution path."
            )
        elif exp["sharpe"] > base["sharpe"] + 0.1 and exp["max_drawdown_pct"] >= base["max_drawdown_pct"] - 1.0:
            print(
                "Paper-only benefit — enable PAPER_CRYPTO_UNIVERSE_EXPANDED=true on Best Paper v2.1. "
                "Live $300: leave OFF (crypto disabled on Alpaca live Profile A)."
            )
        else:
            print(
                "Keep expanded universe OFF for now — insufficient Sharpe/DD improvement vs base 24 pairs. "
                "Live $300: unchanged (crypto sleeve off on Profile A)."
            )


def _equity_underperformance_stats(baseline: dict, with_thinking: dict) -> dict:
    """Compare equity curves: worst gap vs no-thinking baseline."""
    b_eq = baseline.get("equity_values") or []
    t_eq = with_thinking.get("equity_values") or []
    n = min(len(b_eq), len(t_eq))
    if n < 2:
        return {"worst_gap_usd": 0.0, "worst_gap_pct": 0.0, "worst_gap_idx": 0}
    gaps = [t_eq[i] - b_eq[i] for i in range(n)]
    worst_i = min(range(n), key=lambda i: gaps[i])
    b_val = b_eq[worst_i]
    worst_pct = (gaps[worst_i] / b_val * 100) if b_val > 0 else 0.0
    return {
        "worst_gap_usd": round(gaps[worst_i], 2),
        "worst_gap_pct": round(worst_pct, 2),
        "worst_gap_idx": worst_i,
    }


def _post_tilt_drawdown_pp(result: dict, *, horizon: int = 5) -> float:
    """Max forward drawdown (pp) within `horizon` bars after each tilt event."""
    sim = result.get("live_thinking_sim") or result.get("thinking_tilt") or {}
    events = sim.get("events") or []
    eq = result.get("equity_values") or []
    if not events or not eq:
        return 0.0
    date_to_idx = {
        d: i for i, d in enumerate(result.get("equity_index") or [])
    }
    worst = 0.0
    for ev in events:
        idx = date_to_idx.get(ev.get("date"))
        if idx is None:
            continue
        start = eq[idx]
        if start <= 0:
            continue
        end = min(len(eq), idx + horizon + 1)
        window = eq[idx:end]
        peak = start
        for val in window:
            peak = max(peak, val)
            dd = (val / peak - 1.0) * 100
            worst = min(worst, dd)
    return round(abs(worst), 2)


def _volatile_thinking_samples(result: dict, *, max_samples: int = 5) -> list[dict]:
    events = (result.get("live_thinking_sim") or {}).get("events") or []
    volatile = []
    for ev in events:
        vix = ev.get("vix")
        vol = str(ev.get("vol") or "")
        narrative = str(ev.get("narrative") or "").lower()
        is_volatile = (
            (isinstance(vix, (int, float)) and float(vix) >= 20)
            or vol not in ("Low", "low", "")
            or "vol" in narrative
            or "liquidity" in narrative
            or "stress" in narrative
        )
        if is_volatile:
            volatile.append(ev)
    volatile.sort(
        key=lambda e: float(e.get("vix") or 0),
        reverse=True,
    )
    return volatile[:max_samples]


VTI_LEVELS_COMPARE = [
    (0.90, "90% VTI (live-like)"),
    (0.80, "80% VTI"),
    (0.75, "75% VTI"),
    (0.70, "70% VTI"),
]


def _thinking_tilt_event_count(result: dict) -> int:
    sim = result.get("live_thinking_sim") or result.get("thinking_tilt") or {}
    return int(sim.get("tilt_event_count") or 0)


def _vti_level_result_row(label: str, vti_pct: float, result: dict) -> dict:
    post_dd = _post_tilt_drawdown_pp(result)
    return {
        "label": label,
        "vti_pct": vti_pct,
        "return_pct": result["total_return_pct"],
        "sharpe": result["sharpe"],
        "max_dd_pct": result["max_drawdown_pct"],
        "avg_active_exposure_pct": result.get("avg_active_exposure_pct", 0.0),
        "worst_post_tilt_dd_pp": post_dd,
        "tilt_events": _thinking_tilt_event_count(result),
        "avg_vti_pct": round(float(result.get("vti_core_pct") or vti_pct) * 100, 1),
    }


def _print_vti_levels_table(
    title: str,
    window_line: str,
    rows: list[dict],
    *,
    note: str = "",
) -> None:
    print(title)
    print(window_line)
    if note:
        print(note)
    print(
        f"{'VTI level':<28} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'AvgAct':>7} {'PostTilt':>8} {'Tilts':>6}"
    )
    print("-" * 82)
    for row in rows:
        print(
            f"{row['label']:<28} "
            f"{row['return_pct']:>+7.2f}% "
            f"{row['sharpe']:>7.2f} "
            f"{row['max_dd_pct']:>7.2f}% "
            f"{row['avg_active_exposure_pct']:>6.1f}% "
            f"{row['worst_post_tilt_dd_pp']:>7.2f}pp "
            f"{row['tilt_events']:>6d}"
        )
    print("-" * 82)
    best_sharpe = max(rows, key=lambda r: r["sharpe"])
    best_return = max(rows, key=lambda r: r["return_pct"])
    shallowest = max(rows, key=lambda r: r["max_dd_pct"])
    print(
        f"Best Sharpe: {best_sharpe['label']} ({best_sharpe['sharpe']:.2f}) | "
        f"Best return: {best_return['label']} ({best_return['return_pct']:+.2f}%) | "
        f"Shallowest MaxDD: {shallowest['label']} ({shallowest['max_dd_pct']:.2f}%)"
    )


def run_compare_vti_levels(days=None, refresh=False, use_max=False) -> None:
    """Best Paper Bot + Thinking Engine at fixed VTI core levels (90/80/70/60%)."""
    from modules.macro_regime_adaptor import ensure_macro_regime_daily

    ensure_macro_regime_daily()
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
        label = "max"
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
        label = f"{days}d"
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    rows: list[dict] = []
    saved_social = config.SOCIAL_SLEEVE_ENABLED
    config.SOCIAL_SLEEVE_ENABLED = False
    try:
        for vti_pct, level_label in VTI_LEVELS_COMPARE:
            kwargs = {
                "paper_aggressive": True,
                "paper_sleeve_features": True,
                "paper_dynamic_vti": False,
                "paper_dynamic_risk": True,
                "paper_stat_arb": True,
                "paper_vol_trading": True,
                "paper_options_sleeve": True,
                "paper_macro_regime": False,
                "vti_core_pct": vti_pct,
                "paper_thinking": True,
            }
            result = run_backtest(
                data,
                track_active_exposure=True,
                track_metrics=True,
                **kwargs,
            )
            rows.append(_vti_level_result_row(level_label, vti_pct, result))
    finally:
        config.SOCIAL_SLEEVE_ENABLED = saved_social

    cap_pp = round(config.effective_thinking_max_sleeve_delta() * 100, 0)
    _print_vti_levels_table(
        "--- BEST PAPER BOT + THINKING @ FIXED VTI LEVELS ---",
        (
            f"Window ({label}): {data.index[MIN_HISTORY].date()} -> "
            f"{data.index[-1].date()} ({len(data) - MIN_HISTORY} sim bars) | "
            f"VTI B&H: {bench:+.2f}% | tilt cap ±{cap_pp:.0f}%"
        ),
        rows,
        note=(
            "Stack: stat arb + vol + options + overlap/chunk/cofire + thinking heuristic. "
            "AvgAct = average non-VTI exposure. PostTilt = worst 5d forward DD after a tilt."
        ),
    )


def _avg_tilt_magnitude(result: dict) -> float:
    events = (result.get("live_thinking_sim") or result.get("thinking_tilt") or {}).get(
        "events"
    ) or []
    if not events:
        return 0.0
    mags = [sum(abs(float(v)) for v in (e.get("deltas") or {}).values()) for e in events]
    return round(sum(mags) / len(mags), 4)


def _high_impact_news_samples(result: dict, *, max_samples: int = 5) -> list[dict]:
    events = (result.get("live_thinking_sim") or {}).get("events") or []
    ranked = sorted(
        events,
        key=lambda e: float(e.get("news_impact_score") or 0.0),
        reverse=True,
    )
    return [e for e in ranked if float(e.get("news_impact_score") or 0.0) >= 0.35][
        :max_samples
    ]


def run_simulate_live_vti_levels_compare(
    days=None,
    refresh=False,
    use_max=False,
    *,
    start_equity: float | None = None,
    thinking: bool = False,
) -> None:
    """Live Profile A ($300): sweep fixed VTI core levels with crypto OFF, optional thinking."""
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
        label = "max"
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
        label = f"{days}d"
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    eq_start = start_equity or 300.0
    bench = _benchmark_return(data, MIN_HISTORY)
    rows: list[dict] = []
    for vti_pct, level_label in VTI_LEVELS_COMPARE:
        kwargs = {
            "small_account": True,
            "vti_core_pct": vti_pct,
            "live_thinking_start_equity": eq_start,
            "simulate_live_thinking": True,
            "paper_thinking": bool(thinking),
            "track_metrics": True,
        }
        result = run_backtest(data, track_active_exposure=True, **kwargs)
        rows.append(
            {
                "label": level_label,
                "vti_pct": vti_pct,
                "return_pct": result["total_return_pct"],
                "sharpe": result["sharpe"],
                "max_dd_pct": result["max_drawdown_pct"],
                "vs_vti": round(result["total_return_pct"] - bench, 2),
            }
        )
        release_backtest_memory()

    thinking_note = "thinking ON" if thinking else "thinking OFF"
    print("--- LIVE PROFILE A: VTI ALLOCATION SWEEP ---")
    print(
        f"Window ({label}): {data.index[MIN_HISTORY].date()} -> "
        f"{data.index[-1].date()} ({len(data) - MIN_HISTORY} sim bars) | "
        f"start ${eq_start:,.0f} | crypto OFF | {thinking_note} | "
        f"1% risk | ${config.SMALL_ACCOUNT_MAX_NOTIONAL:.0f} max order | "
        f"VTI B&H: {bench:+.2f}%"
    )
    print(f"{'VTI level':<28} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} {'vsVTI':>7}")
    print("-" * 62)
    for row in rows:
        print(
            f"{row['label']:<28} "
            f"{row['return_pct']:>+7.2f}% "
            f"{row['sharpe']:>7.2f} "
            f"{row['max_dd_pct']:>7.2f}% "
            f"{row['vs_vti']:>+6.2f}pp"
        )
    print("-" * 62)
    best_sharpe = max(rows, key=lambda r: r["sharpe"])
    best_return = max(rows, key=lambda r: r["return_pct"])
    shallowest = max(rows, key=lambda r: r["max_dd_pct"])
    print(
        f"Best Sharpe: {best_sharpe['label']} ({best_sharpe['sharpe']:.2f}) | "
        f"Best return: {best_return['label']} ({best_return['return_pct']:+.2f}%) | "
        f"Shallowest MaxDD: {shallowest['label']} ({shallowest['max_dd_pct']:.2f}%)"
    )
    print("\n--- Live $300–$1000 recommendation ---")
    print(
        f"For real small live accounts, {best_sharpe['label']} leads on risk-adjusted return "
        f"(Sharpe {best_sharpe['sharpe']:.2f}, {best_sharpe['return_pct']:+.2f}%, "
        f"MaxDD {best_sharpe['max_dd_pct']:.2f}%). "
        f"Shallowest drawdown: {shallowest['label']} ({shallowest['max_dd_pct']:.2f}%). "
        f"Keep thinking OFF until paper logs match baseline."
    )


def run_simulate_live_thinking_compare(
    days=None,
    refresh=False,
    use_max=False,
    *,
    start_equity: float | None = None,
    vti_levels: bool = False,
    with_news: bool = False,
) -> None:
    """Small-account live profile + capped thinking tilts (heuristic proxy, not Ollama per bar)."""
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
        label = "max"
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
        label = f"{days}d"
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    eq_start = start_equity or (
        300.0 if with_news else config.SMALL_ACCOUNT_BACKTEST_EQUITY
    )
    cap_pp = round(config.LIVE_THINKING_MAX_SLEEVE_DELTA * 100, 0)

    if vti_levels:
        rows: list[dict] = []
        for vti_pct, level_label in VTI_LEVELS_COMPARE:
            kwargs = {
                "small_account": True,
                "vti_core_pct": vti_pct,
                "live_thinking_start_equity": eq_start,
                "simulate_live_thinking": True,
                "paper_thinking": True,
                "track_metrics": True,
            }
            result = run_backtest(data, track_active_exposure=True, **kwargs)
            rows.append(_vti_level_result_row(level_label, vti_pct, result))
            release_backtest_memory()
        _print_vti_levels_table(
            "--- LIVE SMALL-ACCOUNT + THINKING @ VTI LEVELS ---",
            (
                f"Window ({label}): {data.index[MIN_HISTORY].date()} -> "
                f"{data.index[-1].date()} ({len(data) - MIN_HISTORY} sim bars) | "
                f"start ${eq_start:,.0f} | 1% risk | "
                f"${config.SMALL_ACCOUNT_MAX_NOTIONAL:.0f} max order | ±{cap_pp:.0f}% tilt cap"
            ),
            rows,
            note=(
                "Heuristic thinking proxy on regime change (not Ollama per bar). "
                "PostTilt = worst 5d forward DD after a tilt event."
            ),
        )
        print(
            "\n--- Live $300–$1000 note ---"
        )
        best = max(rows, key=lambda r: (r["sharpe"], r["return_pct"]))
        shallow = max(rows, key=lambda r: r["max_dd_pct"])
        print(
            f"For small live accounts, {best['label']} leads on risk-adjusted return "
            f"(Sharpe {best['sharpe']:.2f}, {best['return_pct']:+.2f}%). "
            f"Shallowest MaxDD: {shallow['label']} ({shallow['max_dd_pct']:.2f}%)."
        )
        return

    base_kwargs = {
        "small_account": True,
        "vti_core_pct": config.SMALL_ACCOUNT_VTI_CORE_PCT,
        "live_thinking_start_equity": eq_start,
        "simulate_live_thinking": True,
        "track_metrics": True,
    }
    if with_news:
        configs = [
            ("No thinking / no news", {**base_kwargs, "paper_thinking": False, "with_news": False}),
            (
                "Thinking + news (8 AM digest)",
                {**base_kwargs, "paper_thinking": True, "with_news": True},
            ),
        ]
        title = "--- LIVE $300 SIM: NO THINKING vs THINKING + NEWS ---"
    else:
        configs = [
            ("Small account (no thinking)", {**base_kwargs, "paper_thinking": False}),
            ("Small account (+ thinking tilts)", {**base_kwargs, "paper_thinking": True}),
        ]
        title = "--- LIVE SMALL-ACCOUNT + THINKING SIM ---"

    print(title)
    print(
        f"Profile: 90% VTI core | 1% risk/trade | ${config.SMALL_ACCOUNT_MAX_NOTIONAL:.0f} max order | "
        f"±{cap_pp:.0f}% sleeve cap | max 3 sleeves | daily loss breaker ON"
    )
    print(
        f"Window ({label}): {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars) | start equity ${eq_start:,.0f}"
    )
    if with_news:
        print(
            "News: synthetic 8 AM premarket digest per bar (theme + news_impact_score); "
            "heuristic proxy (not Ollama per bar)."
        )
    else:
        print(
            "Note: uses heuristic thinking proxy on regime change (same as paper backtest), "
            "not live Ollama per bar."
        )
    if with_news:
        print(
            f"{'Config':<32} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
            f"{'Tilts':>6} {'AvgTilt':>8} {'PostDD':>7}"
        )
        print("-" * 86)
    else:
        print(f"{'Config':<34} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8}")
        print("-" * 62)

    baseline = None
    with_thinking = None
    for label_cfg, kwargs in configs:
        result = run_backtest(data, track_active_exposure=True, **kwargs)
        if kwargs.get("paper_thinking"):
            with_thinking = result
        else:
            baseline = result
        release_backtest_memory()
        if with_news:
            print(
                f"{label_cfg:<32} "
                f"{result['total_return_pct']:>+7.2f}% "
                f"{result['sharpe']:>7.2f} "
                f"{result['max_drawdown_pct']:>7.2f}% "
                f"{_thinking_tilt_event_count(result):>6d} "
                f"{_avg_tilt_magnitude(result):>7.3f} "
                f"{_post_tilt_drawdown_pp(result):>6.2f}pp"
            )
        else:
            print(
                f"{label_cfg:<34} "
                f"{result['total_return_pct']:>+7.2f}% "
                f"{result['sharpe']:>7.2f} "
                f"{result['max_drawdown_pct']:>7.2f}%"
            )
    if with_news:
        print("-" * 86)
    else:
        print("-" * 62)

    if baseline and with_thinking:
        under = _equity_underperformance_stats(baseline, with_thinking)
        post_dd = _post_tilt_drawdown_pp(with_thinking)
        sim = with_thinking.get("live_thinking_sim") or {}
        print(
            f"Thinking vs baseline: return "
            f"{with_thinking['total_return_pct'] - baseline['total_return_pct']:+.2f}pp | "
            f"Sharpe {with_thinking['sharpe'] - baseline['sharpe']:+.2f} | "
            f"MaxDD {with_thinking['max_drawdown_pct'] - baseline['max_drawdown_pct']:+.2f}pp"
        )
        print(
            f"Worst tilt drag vs baseline: ${under['worst_gap_usd']:+.2f} "
            f"({under['worst_gap_pct']:+.2f}% of equity at worst gap)"
        )
        print(
            f"Max VTI core cut on tilt refresh: {sim.get('max_vti_drop_pp', 0):.2f}pp | "
            f"tilt events: {sim.get('tilt_event_count', 0)} | "
            f"avg tilt magnitude: {_avg_tilt_magnitude(with_thinking):.3f} | "
            f"max {post_dd:.2f}pp forward DD within 5d after a tilt"
        )

        if with_news:
            samples = _high_impact_news_samples(with_thinking, max_samples=5)
            if samples:
                print("\nSample high-impact news reactions (news_impact_score >= 0.35):")
                for ev in samples:
                    deltas = {
                        k: round(v, 4)
                        for k, v in (ev.get("deltas") or {}).items()
                        if abs(v) > 0.001
                    }
                    impact = float(ev.get("news_impact_score") or 0.0)
                    print(
                        f"  {ev.get('date')} | impact {impact:.2f} | VIX {ev.get('vix')} | "
                        f"VTI {ev.get('vti_before', 0):.0%}->{ev.get('vti_after', 0):.0%}"
                    )
                    theme = str(ev.get("news_theme_summary") or "")[:100]
                    if theme:
                        print(f"    themes: {theme}")
                    print(f"    {ev.get('narrative', '')[:120]}")
                    if deltas:
                        print(f"    deltas: {deltas}")
        else:
            samples = _volatile_thinking_samples(with_thinking, max_samples=5)
            if samples:
                print("\nSample tilts during volatile periods:")
                for ev in samples:
                    deltas = {
                        k: round(v, 4)
                        for k, v in (ev.get("deltas") or {}).items()
                        if abs(v) > 0.001
                    }
                    print(
                        f"  {ev.get('date')} | VIX {ev.get('vix')} | {ev.get('vol')} | "
                        f"VTI {ev.get('vti_before', 0):.0%}->{ev.get('vti_after', 0):.0%}"
                    )
                    print(f"    {ev.get('narrative', '')[:120]}")
                    if deltas:
                        print(f"    deltas: {deltas}")

        print("\n--- Risk summary if enabled on live ~$300 ---")
        scale = 300.0 / eq_start if eq_start > 0 else 3.0
        worst_usd_300 = under["worst_gap_usd"] * scale
        print(
            f"At $300 equity (scaled from ${eq_start:.0f} sim): worst-case tilt drag "
            f"~${worst_usd_300:.2f} vs no-thinking path; max sleeve nudge capped at "
            f"±{config.LIVE_THINKING_MAX_SLEEVE_DELTA:.0%} per sleeve (~±${300 * config.LIVE_THINKING_MAX_SLEEVE_DELTA:.0f} "
            f"notional shift on a single sleeve at full deploy)."
        )
        if with_news:
            print("\n--- Live $300 recommendation ---")
            ret_delta = with_thinking["total_return_pct"] - baseline["total_return_pct"]
            sharpe_delta = with_thinking["sharpe"] - baseline["sharpe"]
            dd_delta = with_thinking["max_drawdown_pct"] - baseline["max_drawdown_pct"]
            enable = (
                sharpe_delta >= -0.05
                and ret_delta >= -2.0
                and worst_usd_300 >= -15.0
            )
            if enable and sharpe_delta > 0.05:
                verdict = (
                    "ENABLE Thinking + News on live $300 — news-aware tilts improve or match "
                    "risk-adjusted returns with acceptable drag."
                )
            elif enable:
                verdict = (
                    "OPTIONAL — marginal benefit; keep news ON in paper, monitor 2 weeks before live."
                )
            else:
                verdict = (
                    "DO NOT enable on live $300 yet — news tilts hurt Sharpe/return or drag exceeds "
                    "comfort on this 365d window. Keep paper-only scheduled news."
                )
            print(verdict)
            print(
                f"  Return {ret_delta:+.2f}pp | Sharpe {sharpe_delta:+.2f} | "
                f"MaxDD {dd_delta:+.2f}pp | tilts {sim.get('tilt_event_count', 0)} | "
                f"worst drag ~${worst_usd_300:.2f}"
            )
            return

        if with_thinking["max_drawdown_pct"] > baseline["max_drawdown_pct"]:
            print(
                "In this window thinking slightly improved MaxDD — still heuristic-only; "
                "live Ollama may be less stable."
            )
        elif with_thinking["max_drawdown_pct"] < baseline["max_drawdown_pct"]:
            dd_worse = baseline["max_drawdown_pct"] - with_thinking["max_drawdown_pct"]
            print(
                f"Thinking deepened MaxDD by {abs(dd_worse):.2f}pp here — "
                "not recommended live until Ollama calibration improves."
            )
        else:
            print("MaxDD unchanged in this window; tilt impact on live $300 likely small in dollar terms.")


def run_risk_parity_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare paper aggressive with vs without risk parity + pod drawdown limits."""
    from modules.macro_regime_adaptor import ensure_macro_regime_daily

    ensure_macro_regime_daily()
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
        label = "max"
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
        label = f"{days}d"
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    base_kwargs = {
        "paper_aggressive": True,
        "paper_sleeve_features": True,
        "paper_dynamic_vti": True,
        "paper_dynamic_risk": True,
        "paper_vol_trading": True,
        "paper_options_sleeve": True,
        "paper_macro_regime": True,
        "paper_stat_arb": True,
    }
    configs = [
        ("Paper (no risk parity)", {**base_kwargs, "paper_risk_parity": False}),
        ("Paper (+ risk parity)", {**base_kwargs, "paper_risk_parity": True}),
    ]
    print(
        "--- RISK PARITY A/B (All Weather caps + pod DD limits; paper aggressive) ---"
    )
    print(
        f"Window ({label}): {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(f"{'Config':<28} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8}")
    print("-" * 58)

    baseline = None
    with_rp = None
    for label_cfg, kwargs in configs:
        result = run_backtest(data, track_active_exposure=True, track_metrics=True, **kwargs)
        if kwargs.get("paper_risk_parity"):
            with_rp = result
        else:
            baseline = result
        print(
            f"{label_cfg:<28} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}%"
        )
    print("-" * 58)
    if baseline and with_rp:
        print(
            f"Risk parity vs baseline: return "
            f"{with_rp['total_return_pct'] - baseline['total_return_pct']:+.2f}pp | "
            f"Sharpe {with_rp['sharpe'] - baseline['sharpe']:+.2f} | "
            f"MaxDD {with_rp['max_drawdown_pct'] - baseline['max_drawdown_pct']:+.2f}pp"
        )

    from modules.risk_parity_sleeve import (
        detect_economic_regime,
        format_risk_parity_log,
        risk_parity_allocation,
    )
    from modules.wisdom_sentiment import resolve_wisdom_regime

    window = data.iloc[-60:]
    wisdom = resolve_wisdom_regime(window)
    econ = detect_economic_regime(
        window, wisdom["regime"], wisdom["volatility"], macro_stress=False
    )
    alloc = risk_parity_allocation(econ, window)
    print("\nSample regime-based allocation (most recent bar):")
    print(f"  {format_risk_parity_log(econ, alloc)}")


FINAL_PAPER_BOT_KWARGS = {
    "paper_aggressive": True,
    "paper_sleeve_features": True,
    "paper_dynamic_vti": True,
    "paper_dynamic_risk": True,
    "paper_stat_arb": True,
    "paper_vol_trading": True,
    "paper_options_sleeve": True,
    "paper_macro_regime": False,
}

LEGACY_PAPER_KWARGS = {
    "paper_aggressive": True,
    "paper_sleeve_features": True,
    "paper_dynamic_vti": True,
    "paper_dynamic_risk": False,
    "paper_stat_arb": False,
    "paper_vol_trading": False,
    "paper_options_sleeve": False,
    "paper_market_neutral_pairs": False,
    "paper_macro_regime": False,
}

FINAL_REPORT_DEFAULT = Path("scripts/analysis/final_paper_bot_backtest.md")


def _result_row(label: str, result: dict, *, bench: float | None = None) -> dict:
    dr = result.get("dynamic_risk") or {}
    vs = result.get("vol_sleeve") or {}
    opt = result.get("options_sleeve") or {}
    halt_raw = result.get("halt_events")
    halt_n = halt_raw if isinstance(halt_raw, int) else len(halt_raw or [])
    return {
        "label": label,
        "return_pct": result["total_return_pct"],
        "sharpe": result["sharpe"],
        "max_dd_pct": result["max_drawdown_pct"],
        "sortino": result.get("sortino"),
        "calmar": result.get("calmar"),
        "win_rate_pct": result.get("win_rate_pct"),
        "rolling_sharpe_mean": result.get("rolling_sharpe_mean"),
        "profit_factor": result.get("profit_factor"),
        "avg_trade_return_pct": result.get("avg_trade_return_pct"),
        "final_equity": result.get("final_equity"),
        "total_orders": result.get("total_orders"),
        "halt_events": halt_n,
        "execution_cost_pct": result.get("execution_cost_pct"),
        "pairs_traded": result.get("pairs_traded", 0),
        "avg_risk_pct": dr.get("avg_risk_pct"),
        "vol_trades": vs.get("trades", 0),
        "options_premium": opt.get("total_premium", 0),
        "pair_corr": result.get("pair_pnl_correlation"),
        "vs_vti": round(result["total_return_pct"] - bench, 2) if bench is not None else None,
    }


def _run_final_window(data, *, window_label: str) -> dict:
    """Run all compare arms for one data window (parallel when enabled)."""
    bench = _benchmark_return(data, MIN_HISTORY)
    arm_specs: list[tuple[str, dict, dict]] = [
        ("Best Paper Bot (current)", FINAL_PAPER_BOT_KWARGS, {}),
        (
            "Best Paper (live vol parity)",
            FINAL_PAPER_BOT_KWARGS,
            {"paper_vol_live_parity": True},
        ),
        ("Legacy paper (pre-sleeve stack)", LEGACY_PAPER_KWARGS, {}),
        (
            "Live small-account sim",
            {},
            {
                "small_account": True,
                "vti_core_pct": config.SMALL_ACCOUNT_VTI_CORE_PCT,
            },
        ),
    ]
    saved_social = config.SOCIAL_SLEEVE_ENABLED
    config.SOCIAL_SLEEVE_ENABLED = False
    rows: list[dict] = []
    arm_results: list[dict] = []
    try:
        if RUN_OPTIONS.parallel_arms and len(arm_specs) > 1:
            tasks = [
                (data.copy(), {**base_kw, **extra}) for _, base_kw, extra in arm_specs
            ]
            arm_results = parallel_map_backtests(tasks, _parallel_backtest_worker)
        else:
            for _, base_kw, extra in arm_specs:
                arm_results.append(
                    run_backtest(
                        data,
                        track_active_exposure=True,
                        track_metrics=True,
                        **{**base_kw, **extra},
                    )
                )
        for (label, _, _), result in zip(arm_specs, arm_results):
            rows.append(_result_row(label, result, bench=bench))
    finally:
        config.SOCIAL_SLEEVE_ENABLED = saved_social

    final = arm_results[0]
    legacy = arm_results[2]

    if bench is not None:
        rows.append(
            {
                "label": "VTI buy & hold",
                "return_pct": round(bench, 2),
                "sharpe": None,
                "max_dd_pct": None,
                "sortino": None,
                "final_equity": None,
                "pairs_traded": None,
                "avg_risk_pct": None,
                "vol_trades": None,
                "options_premium": None,
                "pair_corr": None,
                "vs_vti": 0.0,
            }
        )

    return {
        "window": window_label,
        "start": str(data.index[MIN_HISTORY].date()),
        "end": str(data.index[-1].date()),
        "sim_bars": len(data) - MIN_HISTORY,
        "vti_benchmark_pct": bench,
        "rows": rows,
        "final": final,
        "legacy": legacy,
    }


def _format_final_table(rows: list[dict]) -> str:
    table = format_enhanced_final_table(rows)
    return (
        table
        + "\nNote: vol overlay PnL is synthetic in backtest; live/cloud logs only "
        "(see 'live vol parity' row)."
    )


def _build_final_verdict(windows: list[dict]) -> str:
    lines = ["## Verdict", ""]
    sharpes_final = [w["final"]["sharpe"] for w in windows]
    sharpes_legacy = [w["legacy"]["sharpe"] for w in windows]
    avg_delta = sum(f - l for f, l in zip(sharpes_final, sharpes_legacy)) / len(windows)
    lines.append(
        f"- **Sharpe vs legacy paper:** current stack improves Sharpe by "
        f"**{avg_delta:+.2f}** on average across windows "
        f"({', '.join(f'{w['window']} {w['final']['sharpe']:.2f} vs {w['legacy']['sharpe']:.2f}' for w in windows)})."
    )
    for w in windows:
        f, b = w["final"], w["vti_benchmark_pct"]
        if b is not None:
            lines.append(
                f"- **{w['window']} return vs VTI:** "
                f"{f['total_return_pct']:+.2f}% vs VTI {b:+.2f}% "
                f"({f['total_return_pct'] - b:+.2f} pp)."
            )
        lines.append(
            f"- **{w['window']} risk:** Max DD {f['max_drawdown_pct']:.2f}% | "
            f"Sortino {f.get('sortino', 0):.2f} | "
            f"avg risk {((f.get('dynamic_risk') or {}).get('avg_risk_pct') or 0):.2f}%."
        )
    lines.extend(
        [
            "",
            "### Ready as default Best Paper Bot?",
            "",
        ]
    )
    best_365 = next((w for w in windows if w["window"] == "365d"), windows[0])
    sharpe_365 = best_365["final"]["sharpe"]
    lines.append(
        f"**Mutual-fund benchmark:** typical active funds ~0.4–0.7 Sharpe; "
        f"Best Paper **365d Sharpe {sharpe_365:.2f}**."
    )
    if sharpe_365 >= 0.85:
        lines.append(
            "**Locked as default** — `config.get_best_paper_bot_stack()` matches this profile. "
            "Beats legacy on 365d; monitor 1000d Max DD. Keep **social/SPY-exit OFF**."
        )
    else:
        lines.append(
            "**Review** — 365d Sharpe below target; tune sleeves via `.env` before locking."
        )
    lines.append("")
    lines.append(
        "**Laptop policy:** keep this profile; add only lightweight tweaks here. "
        "Heavy compute → `cloud_bot/` (see `README_CLOUD.md`)."
    )
    return "\n".join(lines)


def _write_final_report(windows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [
        "# Final Paper Bot — Comprehensive Backtest",
        "",
        f"Generated: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## Stack tested (Best Paper Bot)",
        "",
        "- Dynamic VTI (40–75%)",
        "- Dynamic risk (1–3%)",
        "- Statistical arbitrage (cointegration, both legs)",
        "- Volatility overlay (VIX regime)",
        "- Options income (covered calls)",
        "- Advanced flags: overlap, adaptive chunk, co-fire",
        "- Thinking engine: opt-in (Ollama, default off)",
        "- Disabled: macro regime, risk parity, stat arb optimized, social, SPY MA exit",
        "",
        "## Comparisons",
        "",
        "- **Legacy paper** — dynamic VTI + sleeve flags only (no stat arb, vol, dynamic risk, options, macro)",
        "- **Live small-account sim** — 90% VTI, 1% risk, $100 start",
        "- **VTI buy & hold** — passive benchmark",
        "",
    ]
    for w in windows:
        parts.extend(
            [
                f"### {w['window']} ({w['start']} → {w['end']}, {w['sim_bars']} bars)",
                "",
                _format_final_table(w["rows"]),
                "",
            ]
        )
    parts.append(_build_final_verdict(windows))
    path.write_text("\n".join(parts), encoding="utf-8")


def run_final_compare(
    days=None,
    refresh=False,
    use_max=False,
    *,
    report_path: Path | None = None,
    all_windows: bool = False,
) -> list[dict]:
    """Comprehensive paper bot comparison vs legacy, VTI, and small-account sim."""
    window_specs: list[tuple[str, int | None, bool]] = []
    if all_windows:
        window_specs = [("365d", 365, False), ("1000d", 1000, False), ("max", None, True)]
    elif use_max:
        window_specs = [("max", None, True)]
    else:
        d = days or config.BACKTEST_DAYS
        window_specs = [(f"{d}d", d, False)]

    if refresh and window_specs:
        _ensure_daily_data(
            0 if window_specs[0][2] else (window_specs[0][1] or config.BACKTEST_DAYS),
            refresh=True,
            use_max=window_specs[0][2],
        )

    results: list[dict] = []
    print("--- FINAL PAPER BOT COMPARISON (comprehensive) ---")
    for label, d, umax in window_specs:
        if umax:
            data = _ensure_daily_data(0, refresh=False, use_max=True)
        else:
            data = _ensure_daily_data(d, refresh=False, use_max=False)
        if len(data) < MIN_HISTORY:
            print(f"{label}: need {MIN_HISTORY} bars; got {len(data)}.")
            continue
        w = _run_final_window(data, window_label=label)
        results.append(w)
        print_table(_format_final_table(w["rows"]), title=f"=== {label.upper()} ===")
        folds = RUN_OPTIONS.walk_forward_folds
        if folds >= 2 and label == window_specs[0][0]:
            wf = walk_forward_purged(
                data,
                min_history=MIN_HISTORY,
                n_folds=folds,
                run_fn=lambda d, _tb, _te: run_backtest(
                    d, track_active_exposure=True, track_metrics=True, **FINAL_PAPER_BOT_KWARGS
                ),
            )
            if wf:
                print_table(format_walk_forward_table(wf, purged=True), title="Purged walk-forward (Best Paper Bot)")

    if report_path and results:
        _write_final_report(results, report_path)
        print(f"\nReport saved: {report_path}")

    if RUN_OPTIONS.report_html and results:
        primary = results[0]
        final = primary["final"]
        html_path = generate_html_report(
            final,
            RUN_OPTIONS.report_html,
            title="Best Paper Bot — Compare Final",
            header_label=(
                f"Header stats: Best Paper Bot (current) | "
                f"{final.get('total_return_pct', 0):+.2f}% return | "
                f"Sharpe {final.get('sharpe', 0):.2f}"
            ),
            compare_rows=primary["rows"],
        )
        print(f"HTML report: {html_path.resolve()}")

    return results


def run_pairs_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare paper aggressive with vs without market-neutral pair trades."""
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    base_kwargs = {
        "paper_aggressive": True,
        "paper_sleeve_features": True,
        "paper_dynamic_vti": True,
        "paper_dynamic_risk": True,
    }
    configs = [
        (
            "Paper (no pair sleeve)",
            {**base_kwargs, "paper_market_neutral_pairs": False},
        ),
        (
            "Paper (+ Market-Neutral Pairs)",
            {**base_kwargs, "paper_market_neutral_pairs": True},
        ),
    ]
    print("--- MARKET-NEUTRAL PAIRS A/B (corr>0.7, Z>=2.5, both legs; paper aggressive) ---")
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        f"{'Config':<32} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Pairs':>6} {'Corr':>6}"
    )
    print("-" * 78)

    baseline = None
    for label, kwargs in configs:
        result = run_backtest(data, track_active_exposure=True, track_metrics=True, **kwargs)
        if kwargs.get("paper_market_neutral_pairs") is False:
            baseline = result
        pairs = result.get("pairs_traded", 0)
        corr = result.get("pair_pnl_correlation")
        corr_s = f"{corr:.2f}" if corr is not None else "—"
        print(
            f"{label:<32} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{pairs:>6} "
            f"{corr_s:>6}"
        )
    print("-" * 78)
    if baseline:
        print(
            f"Baseline (current paper w/o pairs): "
            f"{baseline['total_return_pct']:+.2f}% return, "
            f"Sharpe {baseline['sharpe']:.2f}, "
            f"MaxDD {baseline['max_drawdown_pct']:.2f}%"
        )


def run_macro_regime_compare(days=None, refresh=False, use_max=False) -> None:
    """Back-compat alias."""
    run_regime_shift_compare(days=days, refresh=refresh, use_max=use_max)


def run_regime_shift_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare paper aggressive with vs without Regime Shift Detector."""
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    configs = [
        (
            "Paper (no regime detector)",
            {
                "paper_aggressive": True,
                "paper_sleeve_features": True,
                "paper_dynamic_vti": True,
                "paper_macro_regime": False,
            },
        ),
        (
            "Paper (+ Regime Shift Detector)",
            {
                "paper_aggressive": True,
                "paper_sleeve_features": True,
                "paper_dynamic_vti": True,
                "paper_macro_regime": True,
            },
        ),
    ]
    print("--- REGIME SHIFT DETECTOR A/B (paper aggressive, dynamic VTI + sleeve flags) ---")
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        f"{'Config':<32} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'AvgAct':>7} {'Regime':>7} {'Macro':>8}"
    )
    print("-" * 82)

    for label, kwargs in configs:
        result = run_backtest(data, track_active_exposure=True, track_metrics=True, **kwargs)
        macro = result.get("macro_regime_sleeve") or {}
        macro_ret = f"{macro.get('return_pct', 0):+.1f}%" if macro else "—"
        regime_pct = macro.get("regime_shift_pct", 0.0) if macro else 0.0
        print(
            f"{label:<32} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{result['avg_active_exposure_pct']:>6.1f}% "
            f"{regime_pct:>6.1f}% "
            f"{macro_ret:>8}"
        )
    print("-" * 82)


def run_felix_social_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare paper aggressive social sleeve: legacy vs enhanced Felix macro detection."""
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    configs = [
        (
            "Paper social (legacy)",
            {
                "paper_aggressive": True,
                "paper_sleeve_features": True,
                "paper_dynamic_vti": True,
                "paper_social_enhanced": False,
            },
        ),
        (
            "Paper social (enhanced Felix)",
            {
                "paper_aggressive": True,
                "paper_sleeve_features": True,
                "paper_dynamic_vti": True,
                "paper_social_enhanced": True,
            },
        ),
    ]
    print("--- FELIX / SOCIAL SLEEVE A/B (paper aggressive, dynamic VTI + sleeve flags) ---")
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        f"{'Config':<30} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'AvgAct':>7} {'GLD%':>6} {'Social':>8}"
    )
    print("-" * 82)

    for label, kwargs in configs:
        result = run_backtest(data, track_active_exposure=True, track_metrics=True, **kwargs)
        social = result.get("social_sleeve") or {}
        social_ret = f"{social.get('return_pct', 0):+.1f}%" if social else "—"
        gld_pct = social.get("gld_target_pct", 0.0)
        print(
            f"{label:<30} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{result['avg_active_exposure_pct']:>6.1f}% "
            f"{gld_pct:>5.1f}% "
            f"{social_ret:>8}"
        )
    print("-" * 82)


def run_paper_sleeve_features_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare paper aggressive with vs without advanced sleeve flags."""
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    configs = [
        (
            "Paper (no sleeve flags)",
            {
                "paper_aggressive": True,
                "paper_sleeve_features": False,
                "paper_dynamic_vti": True,
            },
        ),
        (
            "Paper (+overlap/chunk/cofire/exit)",
            {
                "paper_aggressive": True,
                "paper_sleeve_features": True,
                "paper_dynamic_vti": True,
            },
        ),
    ]
    print("--- PAPER SLEEVE FEATURES A/B (dynamic VTI on both arms) ---")
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        f"{'Config':<32} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'AvgAct':>7} {'CoFire':>7}"
    )
    print("-" * 76)

    for label, kwargs in configs:
        result = run_backtest(data, track_active_exposure=True, track_metrics=True, **kwargs)
        print(
            f"{label:<32} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{result['avg_active_exposure_pct']:>6.1f}% "
            f"{result['cofire_pct']:>6.1f}%"
        )
    print("-" * 76)


def run_dynamic_universe_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare static UNIVERSE vs dynamic screener on conservative blend + strict PIT."""
    saved_dyn = config.PAPER_DYNAMIC_UNIVERSE_ENABLED
    saved_strict = config.PAPER_DYNAMIC_UNIVERSE_STRICT
    config.PAPER_DYNAMIC_UNIVERSE_ENABLED = True
    config.PAPER_DYNAMIC_UNIVERSE_STRICT = False
    config.set_paper_aggressive_context(True)
    config.set_backtest_paper_sleeves_context(True)

    sim_days = days or config.BACKTEST_DAYS
    from modules.dynamic_universe import screener_turnover_vs_prior, screener_universe_meta

    screener = _prefetch_screener_for_backtest(
        sim_days, refresh=refresh, use_max=use_max
    )

    if use_max:
        data = _ensure_daily_data(0, refresh=True, use_max=True)
        win_label = "max"
    else:
        data = _ensure_daily_data(sim_days, refresh=True, use_max=False)
        win_label = f"{sim_days}d"

    config.PAPER_DYNAMIC_UNIVERSE_ENABLED = saved_dyn
    config.PAPER_DYNAMIC_UNIVERSE_STRICT = saved_strict

    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    turnover = screener_turnover_vs_prior(screener)
    static_size = len(_static_equity_universe(data.columns))
    config.PAPER_DYNAMIC_UNIVERSE_ENABLED = True
    dyn_size = len(_dynamic_equity_universe(data.columns))
    config.PAPER_DYNAMIC_UNIVERSE_ENABLED = saved_dyn
    bench = _benchmark_return(data, MIN_HISTORY)
    blend_base = {
        **CONSERVATIVE_BLEND_KWARGS,
        "paper_thinking": True,
        "with_news": True,
    }
    configs = [
        (
            f"Static equity ({static_size} names)",
            {
                **blend_base,
                "paper_dynamic_universe": False,
                "paper_dynamic_universe_strict": False,
            },
        ),
        (
            f"Dynamic screener ({dyn_size} names)",
            {
                **blend_base,
                "paper_dynamic_universe": True,
                "paper_dynamic_universe_strict": False,
            },
        ),
    ]

    print("--- DYNAMIC UNIVERSE IMPACT A/B (conservative blend + strict PIT) ---")
    print(
        f"Window ({win_label}): {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        "Held constant: Top1 conservative vol sizing | spec -4% stop | "
        "thinking ON | sector rotation OFF"
    )
    filters = (screener_universe_meta().get("filters") or {})
    print(
        f"Screener file: {len(screener)} tickers | "
        f"static pool {static_size} | dynamic pool {dyn_size} | "
        f"min price ${filters.get('min_price', 7)} | "
        f"min $vol ${float(filters.get('min_avg_dollar_volume', 50_000_000))/1e6:.0f}M"
    )
    if turnover.get("prior_count"):
        print(
            f"Week-over-week universe churn: {turnover['changes']} ticker changes "
            f"({turnover['overlap']} retained of {turnover['prior_count']})"
        )
    samples = _universe_sample_lines(data, screener)
    if samples:
        print("Sample dynamic names in backtest window:")
        for line in samples:
            print(line)
    new_names = sorted(
        set(screener) - set(config.UNIVERSE),
        key=lambda t: screener.index(t) if t in screener else 999,
    )
    if new_names:
        print(f"New vs static UNIVERSE: {', '.join(new_names[:16])}")
    print(
        f"{'Config':<32} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Trades':>7} {'Univ':>5}"
    )
    print("-" * 82)

    import warnings

    warning_count = 0
    results: list[tuple[str, dict]] = []
    for label, kwargs in configs:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = run_backtest(data, track_metrics=True, **kwargs)
            warning_count += sum(
                1
                for w in caught
                if "screener tickers" in str(w.message).lower()
                or "dynamic universe" in str(w.message).lower()
            )
        results.append((label, result))
        release_backtest_memory()
        print(
            f"{label:<32} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{result.get('nyse_signals', 0):>7} "
            f"{result.get('equity_universe_size', 0):>5}"
        )

    print("-" * 82)
    print(f"Screener coverage warnings during runs: {warning_count}")
    if len(results) == 2:
        _, static_r = results[0]
        _, dyn_r = results[1]
        print(
            f"\nDelta (dynamic - static): "
            f"return {dyn_r['total_return_pct'] - static_r['total_return_pct']:+.2f}pp | "
            f"Sharpe {dyn_r['sharpe'] - static_r['sharpe']:+.2f} | "
            f"MaxDD {dyn_r['max_drawdown_pct'] - static_r['max_drawdown_pct']:+.2f}pp | "
            f"Trades {dyn_r.get('nyse_signals', 0) - static_r.get('nyse_signals', 0):+d} | "
            f"universe size {dyn_r.get('equity_universe_size', 0) - static_r.get('equity_universe_size', 0):+d}"
        )
        dyn_helps = (
            dyn_r["total_return_pct"] >= static_r["total_return_pct"] - 0.5
            and dyn_r["sharpe"] >= static_r["sharpe"] - 0.02
        )
        if dyn_helps:
            print(
                "\nRecommendation: KEEP dynamic universe ON for paper "
                "(PAPER_DYNAMIC_UNIVERSE_ENABLED=true)."
            )
        else:
            print(
                "\nRecommendation: Static UNIVERSE outperformed on this window — "
                "keep dyn_univ ON only if you value broader discovery."
            )
    print("-" * 82)


def run_ipo_rules_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare dynamic strict universe with vs without IPO safety rules."""
    saved_dyn = config.PAPER_DYNAMIC_UNIVERSE_ENABLED
    saved_strict = config.PAPER_DYNAMIC_UNIVERSE_STRICT
    saved_ipo = config.PAPER_IPO_SAFETY_ENABLED
    config.PAPER_DYNAMIC_UNIVERSE_ENABLED = True
    config.PAPER_DYNAMIC_UNIVERSE_STRICT = True
    config.set_paper_aggressive_context(True)
    config.set_backtest_paper_sleeves_context(True)

    sim_days = days or config.BACKTEST_DAYS
    from modules.dynamic_universe import (
        IPO_SAFETY_MAX_POSITION_PCT,
        IPO_SAFETY_TRIM_GAIN_PCT,
        IPO_SAFETY_TRIM_TARGET_PCT,
    )

    need_strict_file = True
    try:
        from modules.dynamic_universe import screener_universe_meta

        filters = (screener_universe_meta().get("filters") or {})
        need_strict_file = filters.get("strict_mode") is not True
    except ImportError:
        pass
    _prefetch_screener_for_backtest(
        sim_days, refresh=refresh or need_strict_file, use_max=use_max
    )

    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        data = _ensure_daily_data(sim_days, refresh=refresh, use_max=False)

    config.PAPER_DYNAMIC_UNIVERSE_ENABLED = saved_dyn
    config.PAPER_DYNAMIC_UNIVERSE_STRICT = saved_strict
    config.PAPER_IPO_SAFETY_ENABLED = saved_ipo

    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    base_kwargs = {
        "paper_aggressive": True,
        "paper_sleeve_features": True,
        "paper_dynamic_vti": True,
        "paper_dynamic_risk": True,
        "paper_stat_arb": True,
        "paper_dynamic_universe": True,
        "paper_dynamic_universe_strict": True,
        "track_active_exposure": True,
    }
    configs = [
        (
            "Strict dynamic (no IPO rules)",
            {**base_kwargs, "paper_ipo_safety": False},
        ),
        (
            "Strict dynamic (+ IPO rules)",
            {**base_kwargs, "paper_ipo_safety": True},
        ),
    ]

    print("--- PAPER IPO SAFETY A/B (dynamic strict universe) ---")
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        "IPO rules when ON: "
        f"max {IPO_SAFETY_MAX_POSITION_PCT:.0%} equity | "
        f"0.5x sizing | trim to {IPO_SAFETY_TRIM_TARGET_PCT:.0%} at "
        f"+{IPO_SAFETY_TRIM_GAIN_PCT:.0%} gain | IPO window 5–29 trading days"
    )
    print(
        f"{'Config':<34} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Trades':>7} {'IPO':>5} {'Trim':>5}"
    )
    print("-" * 88)

    results: list[tuple[str, dict]] = []
    for label, kwargs in configs:
        result = run_backtest(data, **kwargs)
        results.append((label, result))
        ipo = result.get("ipo_safety") or {}
        print(
            f"{label:<34} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{result.get('nyse_signals', 0):>7} "
            f"{ipo.get('buys', 0):>5} "
            f"{ipo.get('trims', 0):>5}"
        )
        release_backtest_memory()

    print("-" * 88)
    if len(results) == 2:
        _, off_r = results[0]
        _, on_r = results[1]
        ipo_on = on_r.get("ipo_safety") or {}
        print(
            f"Delta (IPO rules ON - OFF): "
            f"return {on_r['total_return_pct'] - off_r['total_return_pct']:+.2f}pp | "
            f"Sharpe {on_r['sharpe'] - off_r['sharpe']:+.2f} | "
            f"MaxDD {on_r['max_drawdown_pct'] - off_r['max_drawdown_pct']:+.2f}pp | "
            f"Trades {on_r.get('nyse_signals', 0) - off_r.get('nyse_signals', 0):+d}"
        )
        print(
            f"IPO activity (rules ON): {ipo_on.get('buys', 0)} buys | "
            f"{ipo_on.get('trims', 0)} trims | "
            f"${ipo_on.get('trim_notional', 0):,.0f} trimmed notional"
        )
        if on_r["sharpe"] >= off_r["sharpe"] and on_r["max_drawdown_pct"] >= off_r["max_drawdown_pct"]:
            print("Recommendation: ENABLE PAPER_IPO_SAFETY_ENABLED on paper (default). Live: keep OFF unless validated.")
        elif ipo_on.get("buys", 0) == 0:
            print("Recommendation: IPO rules neutral (no IPO-window trades in window). Safe to leave enabled on paper.")
        else:
            print("Recommendation: Review IPO trim impact; consider paper ON, live OFF until more data.")
    print("-" * 88)


def run_new_markets_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare Best Paper v2.1 baseline vs international ADR vs bond vs both."""
    from modules.bond_sleeve import BOND_RISK_NOTE
    from modules.international_sleeve import ADR_RISK_NOTE, INTERNATIONAL_ADR_SYMBOLS
    from modules.options_sleeve import ensure_vix_daily

    ensure_vix_daily()
    config.set_paper_aggressive_context(True)
    config.set_backtest_paper_sleeves_context(True)
    config.set_backtest_international_prefetch(True)
    config.set_backtest_bond_prefetch(True)
    config.enforce_best_paper_stack()

    sim_days = days or config.BACKTEST_DAYS
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        data = _ensure_daily_data(sim_days, refresh=refresh, use_max=False)

    adr_in_data = [s for s in INTERNATIONAL_ADR_SYMBOLS if s in data.columns]
    bond_syms = [s for s in ("TLT", "GOVT", config.BOND_SLEEVE_SYMBOL) if s in data.columns]
    if (len(adr_in_data) < 5 or not bond_syms) and not refresh:
        if use_max:
            data = _ensure_daily_data(0, refresh=True, use_max=True)
        else:
            data = _ensure_daily_data(sim_days, refresh=True, use_max=False)
        adr_in_data = [s for s in INTERNATIONAL_ADR_SYMBOLS if s in data.columns]
        bond_syms = [s for s in ("TLT", "GOVT", config.BOND_SLEEVE_SYMBOL) if s in data.columns]

    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    base_kwargs = {
        "paper_aggressive": True,
        "paper_sleeve_features": True,
        "paper_dynamic_vti": True,
        "paper_dynamic_risk": True,
        "paper_stat_arb": True,
        "paper_vol_trading": True,
        "paper_options_sleeve": True,
        "paper_dynamic_universe": True,
        "paper_ipo_safety": True,
        "track_active_exposure": True,
        "paper_international_sleeve": False,
        "paper_bond_sleeve": False,
    }
    configs = [
        ("Baseline (no new markets)", dict(base_kwargs)),
        ("+ International ADR", {**base_kwargs, "paper_international_sleeve": True}),
        ("+ Bond sleeve (TLT/GOVT)", {**base_kwargs, "paper_bond_sleeve": True}),
        (
            "+ Both (ADR + bond)",
            {**base_kwargs, "paper_international_sleeve": True, "paper_bond_sleeve": True},
        ),
    ]

    print("--- BEST PAPER v2.1: NEW MARKETS COMPARE ---")
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars) | ADRs: {len(adr_in_data)} | "
        f"Bonds: {', '.join(bond_syms) or 'n/a'}"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        f"Intl: 0-{config.INTERNATIONAL_SLEEVE_CAP_PCT:.0%} macro/thinking | "
        f"Bond: 0-{config.BOND_SLEEVE_CAP_PCT:.0%} risk-off/VIX>={config.BOND_VIX_TRIGGER_MIN:.0f}"
    )
    print(
        f"{'Config':<32} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Intl':>5} {'Bond':>5}"
    )
    print("-" * 78)

    results: list[tuple[str, dict]] = []
    baseline: dict | None = None
    for label, kwargs in configs:
        result = run_backtest(data, **kwargs)
        results.append((label, result))
        if baseline is None:
            baseline = result
        print(
            f"{label:<32} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{result.get('international_signals', 0):>5} "
            f"{result.get('bond_signals', 0):>5}"
        )
        release_backtest_memory()

    print("-" * 78)
    if baseline and len(results) >= 4:
        _, intl_r = results[1]
        _, bond_r = results[2]
        _, both_r = results[3]
        intl_stats = intl_r.get("international_stats") or {}
        bond_stats = bond_r.get("bond_stats") or {}
        both_intl = both_r.get("international_stats") or {}
        both_bond = both_r.get("bond_stats") or {}

        print(
            f"vs baseline — Intl only: "
            f"{intl_r['total_return_pct'] - baseline['total_return_pct']:+.2f}pp return, "
            f"Sharpe {intl_r['sharpe'] - baseline['sharpe']:+.2f}, "
            f"MaxDD {intl_r['max_drawdown_pct'] - baseline['max_drawdown_pct']:+.2f}pp"
        )
        print(
            f"vs baseline — Bond only: "
            f"{bond_r['total_return_pct'] - baseline['total_return_pct']:+.2f}pp return, "
            f"Sharpe {bond_r['sharpe'] - baseline['sharpe']:+.2f}, "
            f"MaxDD {bond_r['max_drawdown_pct'] - baseline['max_drawdown_pct']:+.2f}pp"
        )
        print(
            f"vs baseline — Both: "
            f"{both_r['total_return_pct'] - baseline['total_return_pct']:+.2f}pp return, "
            f"Sharpe {both_r['sharpe'] - baseline['sharpe']:+.2f}, "
            f"MaxDD {both_r['max_drawdown_pct'] - baseline['max_drawdown_pct']:+.2f}pp"
        )
        intl_top = (both_intl or intl_stats).get("top_symbols") or []
        if intl_top:
            print("Top ADRs: " + ", ".join(f"{sym}({n})" for sym, n in intl_top[:6]))
        bond_top = (both_bond or bond_stats).get("top_symbols") or []
        max_cap = (both_bond or bond_stats).get("max_cap_pct", 0)
        active_bars = (both_bond or bond_stats).get("active_bars", 0)
        if bond_top or active_bars:
            picks = ", ".join(f"{sym}({n})" for sym, n in bond_top[:3]) if bond_top else "n/a"
            print(
                f"Bond behavior: {picks} | max cap {max_cap:.1%} | "
                f"trigger bars {active_bars} | buys {(both_bond or bond_stats).get('buys', 0)} "
                f"sells {(both_bond or bond_stats).get('sells', 0)}"
            )

        best = max(results, key=lambda r: (r[1]["sharpe"], r[1]["total_return_pct"]))
        print(f"\nBest risk-adjusted: {best[0]} (Sharpe {best[1]['sharpe']:.2f})")
        print("\n--- Recommendations ---")
        intl_ok = (
            intl_r["sharpe"] >= baseline["sharpe"] - 0.01
            and intl_r["total_return_pct"] >= baseline["total_return_pct"] - 0.5
        )
        bond_ok = (
            bond_r["sharpe"] >= baseline["sharpe"] - 0.01
            and bond_r["max_drawdown_pct"] >= baseline["max_drawdown_pct"]
        )
        both_ok = (
            both_r["sharpe"] >= baseline["sharpe"]
            and both_r["total_return_pct"] >= baseline["total_return_pct"]
        )
        if intl_ok:
            print("Paper: PAPER_INTERNATIONAL_SLEEVE_ENABLED=true — ADR edge validated.")
        else:
            print("Paper: keep PAPER_INTERNATIONAL_SLEEVE_ENABLED=false unless extended validation.")
        if bond_ok:
            print("Paper: PAPER_BOND_SLEEVE_ENABLED=true — drawdown/risk-off hedge validated.")
        else:
            print("Paper: keep PAPER_BOND_SLEEVE_ENABLED=false on risk-on windows like this.")
        if both_ok:
            print("Paper combo: enable both for research book; monitor overlap on stress days.")
        else:
            print("Paper combo: prefer best single sleeve over both together.")
        print("cloud_bot: migrate only sleeves that beat baseline Sharpe; keep flags opt-in per VPS.")
        print(f"Notes: {ADR_RISK_NOTE} | {BOND_RISK_NOTE}")
    print("-" * 78)


def run_international_sleeve_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare Best Paper Bot v2.1 with vs without international ADR sleeve."""
    from modules.international_sleeve import ADR_RISK_NOTE, INTERNATIONAL_ADR_SYMBOLS

    config.set_paper_aggressive_context(True)
    config.set_backtest_paper_sleeves_context(True)
    config.set_backtest_international_prefetch(True)
    config.enforce_best_paper_stack()

    sim_days = days or config.BACKTEST_DAYS
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        data = _ensure_daily_data(sim_days, refresh=refresh, use_max=False)

    adr_in_data = [s for s in INTERNATIONAL_ADR_SYMBOLS if s in data.columns]
    if len(adr_in_data) < 5 and not refresh:
        if use_max:
            data = _ensure_daily_data(0, refresh=True, use_max=True)
        else:
            data = _ensure_daily_data(sim_days, refresh=True, use_max=False)
        adr_in_data = [s for s in INTERNATIONAL_ADR_SYMBOLS if s in data.columns]

    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    base_kwargs = {
        "paper_aggressive": True,
        "paper_sleeve_features": True,
        "paper_dynamic_vti": True,
        "paper_dynamic_risk": True,
        "paper_stat_arb": True,
        "paper_vol_trading": True,
        "paper_options_sleeve": True,
        "paper_dynamic_universe": True,
        "paper_ipo_safety": True,
        "track_active_exposure": True,
    }
    configs = [
        ("Best Paper v2.1 (no intl ADR)", {**base_kwargs, "paper_international_sleeve": False}),
        ("Best Paper v2.1 (+ intl ADR)", {**base_kwargs, "paper_international_sleeve": True}),
    ]

    print("--- PAPER INTERNATIONAL ADR SLEEVE A/B ---")
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars) | ADRs in data: {len(adr_in_data)}"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        f"Sleeve: 0–{config.INTERNATIONAL_SLEEVE_CAP_PCT:.0%} when macro/thinking triggers | "
        f"MA50 momentum | paper only | no forex"
    )
    print(f"Note: {ADR_RISK_NOTE}")
    print(
        f"{'Config':<34} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Intl':>6} {'Active':>7}"
    )
    print("-" * 82)

    results: list[tuple[str, dict]] = []
    for label, kwargs in configs:
        result = run_backtest(data, **kwargs)
        results.append((label, result))
        intl = result.get("international_stats") or {}
        print(
            f"{label:<34} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{result.get('international_signals', 0):>6} "
            f"{intl.get('active_bars', 0):>7}"
        )
        release_backtest_memory()

    print("-" * 82)
    if len(results) == 2:
        _, off_r = results[0]
        _, on_r = results[1]
        intl_on = on_r.get("international_stats") or {}
        top = intl_on.get("top_symbols") or []
        print(
            f"Delta (+ intl - baseline): "
            f"return {on_r['total_return_pct'] - off_r['total_return_pct']:+.2f}pp | "
            f"Sharpe {on_r['sharpe'] - off_r['sharpe']:+.2f} | "
            f"MaxDD {on_r['max_drawdown_pct'] - off_r['max_drawdown_pct']:+.2f}pp | "
            f"Intl trades {on_r.get('international_signals', 0)}"
        )
        if top:
            picks = ", ".join(f"{sym}({n})" for sym, n in top[:8])
            print(f"Top international picks: {picks}")
        else:
            print("Top international picks: (none — triggers or data gap)")
        if on_r["sharpe"] > off_r["sharpe"] and on_r["total_return_pct"] > off_r["total_return_pct"]:
            print(
                "Recommendation: opt-in on paper only (PAPER_INTERNATIONAL_SLEEVE_ENABLED=true); "
                "keep OFF on Live Profile A ($300)."
            )
        elif on_r["total_return_pct"] >= off_r["total_return_pct"] - 0.5:
            print(
                "Recommendation: neutral/slight benefit — paper opt-in OK for research; "
                "Live Profile A: keep OFF (ADR/FX gap risk on small account)."
            )
        else:
            print(
                "Recommendation: keep PAPER_INTERNATIONAL_SLEEVE_ENABLED=false; "
                "does not improve risk-adjusted returns on this window."
            )
    print("-" * 82)


def run_profit_target_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare paper aggressive with vs without trailing profit targets."""
    from modules.profit_target import (
        PROFIT_TARGET_ARM_GAIN_PCT,
        PROFIT_TARGET_REBUY_COOLDOWN_DAYS,
        PROFIT_TARGET_TRAIL_PCT,
    )

    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)

    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    base_kwargs = {
        "paper_aggressive": True,
        "paper_sleeve_features": True,
        "paper_dynamic_vti": True,
        "paper_dynamic_risk": True,
        "paper_stat_arb": True,
        "paper_dynamic_universe": True,
        "track_active_exposure": True,
    }
    configs = [
        ("Paper aggressive (no profit target)", {**base_kwargs, "paper_profit_target": False}),
        ("Paper aggressive (+ profit target)", {**base_kwargs, "paper_profit_target": True}),
    ]

    print("--- PAPER PROFIT TARGET A/B (NYSE + SPY, non-IPO) ---")
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        "Rules when ON: "
        f"+{PROFIT_TARGET_ARM_GAIN_PCT:.0%} arms 10% trailing stop | "
        f"{PROFIT_TARGET_REBUY_COOLDOWN_DAYS}d rebuy cooldown | IPOs excluded"
    )
    print(
        f"{'Config':<36} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Trades':>7} {'Exit':>5} {'Arm':>5}"
    )
    print("-" * 88)

    results: list[tuple[str, dict]] = []
    for label, kwargs in configs:
        result = run_backtest(data, **kwargs)
        results.append((label, result))
        pt = result.get("profit_target") or {}
        print(
            f"{label:<36} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{result.get('nyse_signals', 0):>7} "
            f"{pt.get('exits', 0):>5} "
            f"{pt.get('armed', 0):>5}"
        )
        release_backtest_memory()

    print("-" * 88)
    if len(results) == 2:
        _, off_r = results[0]
        _, on_r = results[1]
        pt_on = on_r.get("profit_target") or {}
        print(
            f"Delta (profit target ON - OFF): "
            f"return {on_r['total_return_pct'] - off_r['total_return_pct']:+.2f}pp | "
            f"Sharpe {on_r['sharpe'] - off_r['sharpe']:+.2f} | "
            f"MaxDD {on_r['max_drawdown_pct'] - off_r['max_drawdown_pct']:+.2f}pp | "
            f"Trades {on_r.get('nyse_signals', 0) - off_r.get('nyse_signals', 0):+d}"
        )
        print(
            f"Profit target activity: {pt_on.get('exits', 0)} trailing exits | "
            f"{pt_on.get('armed', 0)} positions armed | "
            f"{pt_on.get('rebuy_blocks', 0)} rebuy blocks"
        )
        if (
            on_r["sharpe"] >= off_r["sharpe"] + 0.03
            and on_r["max_drawdown_pct"] >= off_r["max_drawdown_pct"]
        ):
            print(
                "Recommendation: ENABLE PAPER_PROFIT_TARGET_ENABLED=true on paper after "
                "1–2 weeks live-paper observation. Keep OFF on live $300."
            )
        elif pt_on.get("exits", 0) == 0 and pt_on.get("armed", 0) == 0:
            print(
                "Recommendation: Rules had no effect this window (no positions reached +25%). "
                "Safe to trial on paper; keep default OFF until validated."
            )
        elif pt_on.get("exits", 0) == 0:
            print(
                f"Recommendation: {pt_on.get('armed', 0)} positions armed but no trailing exits "
                "this window — rules are wired; keep default OFF until exits prove beneficial."
            )
        elif on_r["total_return_pct"] < off_r["total_return_pct"] - 5:
            print(
                "Recommendation: KEEP PAPER_PROFIT_TARGET_ENABLED=false for now — "
                "trailing exits cut winners materially in this window."
            )
        else:
            print(
                "Recommendation: Marginal impact — optional on paper; keep OFF on live."
            )
    print("-" * 88)


BEST_PAPER_V21_KWARGS = {
    **FINAL_PAPER_BOT_KWARGS,
    "paper_dynamic_universe": True,
    "paper_ipo_safety": True,
    "paper_tech_guard": True,
    "paper_sector_rotation": False,
    "paper_profit_target": False,
    "paper_scaling_strategy": False,
    "paper_pattern_awareness": False,
    "paper_vol_position_sizing": False,
    "paper_loss_cutting": False,
    "track_active_exposure": True,
}

CONSERVATIVE_BLEND_KWARGS = {
    **BEST_PAPER_V21_KWARGS,
    "strict_pit": True,
    "paper_pattern_awareness": False,
    "paper_vol_position_sizing": True,
    "paper_loss_cutting": True,
    "top1_vol_conservative": True,
    "top1_loss_conservative": True,
    "paper_sector_rotation": False,
}


def run_scaling_strategy_compare(
    days=None,
    refresh=False,
    use_max=False,
    *,
    focus_ticker: str | None = None,
) -> None:
    """Compare Best Paper v2.1 vs smaller-gains + dip-rebuy scaling rules."""
    from modules.scaling_strategy import (
        REBUY_PULLBACK,
        TAKE_FRACTION,
        TAKE_LEVELS,
        format_symbol_report,
    )

    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
        win_label = "max"
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
        win_label = f"{days}d"
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    slippage_note = (
        f"Costs: equity slippage {RUN_OPTIONS.equity_slippage_bps:.0f} bps | "
        f"crypto slippage {RUN_OPTIONS.crypto_slippage_bps:.0f} bps | "
        f"equity commission {RUN_OPTIONS.equity_commission_bps:.0f} bps"
    )
    configs = [
        (
            "Best Paper v2.1 (current)",
            dict(BEST_PAPER_V21_KWARGS),
        ),
        (
            "Best Paper v2.1 (+ scaling)",
            {**BEST_PAPER_V21_KWARGS, "paper_scaling_strategy": True},
        ),
    ]

    title = "--- SCALING STRATEGY A/B (smaller gains + dip rebuy) ---"
    if focus_ticker:
        title += f" [focus {focus_ticker.upper()}]"
    print(title)
    print(
        f"Window ({win_label}): {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(slippage_note)
    print(
        "Rules when ON: "
        f"sell {TAKE_FRACTION:.0%} at {', '.join(f'+{x:.0%}' for x in TAKE_LEVELS)} | "
        f"rebuy after {REBUY_PULLBACK:.0%} pullback (limit-style fill) | "
        f"max {int(__import__('os').getenv('SCALING_MAX_ROUND_TRIPS_WEEK', '3'))} round trips/ticker/week | "
        f"speculative size 50% on SPCX-class names"
    )
    print(
        f"{'Config':<32} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Trades':>7} {'Scale':>6} {'Rebuy':>6}"
    )
    print("-" * 88)

    results: list[tuple[str, dict]] = []
    for label, kwargs in configs:
        result = run_backtest(data, track_metrics=True, **kwargs)
        results.append((label, result))
        sc = result.get("scaling_strategy") or {}
        print(
            f"{label:<32} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{result.get('nyse_signals', 0):>7} "
            f"{sc.get('partial_sells', 0):>6} "
            f"{sc.get('rebuys', 0):>6}"
        )
        if sc:
            print(
                f"  scaling: partial sells {sc.get('partial_sells', 0)} | "
                f"rebuys {sc.get('rebuys', 0)} | "
                f"round trips {sc.get('round_trips', 0)} | "
                f"blocked buys {sc.get('blocked_buys', 0)} | "
                f"exec cost {result.get('execution_cost_pct', 0):.3f}%"
            )
        release_backtest_memory()

    print("-" * 88)
    if len(results) == 2:
        _, base_r = results[0]
        _, scale_r = results[1]
        sc = scale_r.get("scaling_strategy") or {}
        print(
            f"Delta (scaling ON - current): "
            f"return {scale_r['total_return_pct'] - base_r['total_return_pct']:+.2f}pp | "
            f"Sharpe {scale_r['sharpe'] - base_r['sharpe']:+.2f} | "
            f"MaxDD {scale_r['max_drawdown_pct'] - base_r['max_drawdown_pct']:+.2f}pp | "
            f"Trades {scale_r.get('nyse_signals', 0) - base_r.get('nyse_signals', 0):+d}"
        )
        tickers = []
        if focus_ticker:
            tickers.append(focus_ticker.upper())
        tickers.extend(["SPCX", "COIN", "PLTR"])
        seen: set[str] = set()
        print("\nPer-ticker scaling activity (ON arm):")
        for t in tickers:
            tu = t.upper()
            if tu in seen:
                continue
            seen.add(tu)
            print(f"  {format_symbol_report(sc, tu)}")
            if tu in data.columns:
                print(f"    (price column present in backtest window)")
            elif tu == focus_ticker:
                print(f"    (WARNING: {tu} not in price matrix — no trades possible)")

        if (
            scale_r["sharpe"] >= base_r["sharpe"] + 0.05
            and scale_r["max_drawdown_pct"] >= base_r["max_drawdown_pct"] - 0.5
            and scale_r["total_return_pct"] >= base_r["total_return_pct"] - 1.0
        ):
            print(
                "\nRecommendation: ADOPT on paper as opt-in research "
                "(PAPER_SCALING_STRATEGY_ENABLED) after 2–4 weeks observation. "
                "Keep Best Paper v2.1 Final locked defaults until validated."
            )
        elif sc.get("partial_sells", 0) == 0:
            print(
                "\nRecommendation: KEEP current logic — scaling rules never triggered "
                "(positions may not have reached +4% tiers in this window)."
            )
        elif scale_r["total_return_pct"] < base_r["total_return_pct"] - 3:
            print(
                "\nRecommendation: KEEP current Best Paper v2.1 logic — "
                "partial profit-taking + churn reduced total return materially."
            )
        else:
            print(
                "\nRecommendation: KEEP current logic as default — scaling adds turnover "
                "without clear Sharpe/return edge on this window. "
                "Optional paper experiment only."
            )
    print("-" * 88)


def run_pattern_compare(
    days=None,
    refresh=False,
    use_max=False,
) -> None:
    """Compare Best Paper v2.1 with vs without tuned chart pattern awareness."""
    from modules.chart_patterns import PATTERN_LABELS, format_pattern_stats_report

    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
        win_label = "max"
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
        win_label = f"{days}d"
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    configs = [
        (
            "Best Paper v2.1 (current)",
            dict(BEST_PAPER_V21_KWARGS),
        ),
        (
            "Best Paper v2.1 (+ patterns full)",
            {
                **BEST_PAPER_V21_KWARGS,
                "paper_pattern_awareness": True,
                "paper_pattern_bearish_only": False,
            },
        ),
        (
            "Best Paper v2.1 (+ bearish filter)",
            {
                **BEST_PAPER_V21_KWARGS,
                "paper_pattern_awareness": True,
                "paper_pattern_bearish_only": True,
            },
        ),
    ]

    print("--- CHART PATTERN AWARENESS A/B (tuned thresholds, research-only) ---")
    print(
        f"Window ({win_label}): {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        "Tuned rules: cup/handle conf>="
        f"{config.PATTERN_CUP_HANDLE_MIN_CONF:.0%}, flag conf>="
        f"{config.PATTERN_FLAG_MIN_CONF:.0%}, min price ${config.PATTERN_MIN_PRICE:.0f}, "
        f"min vol {config.PATTERN_MIN_AVG_VOLUME/1e3:.0f}k | "
        f"boost={config.PATTERN_SCORE_BOOST:.0%} trim={config.PATTERN_SCORE_TRIM:.0%}"
    )
    print(
        f"{'Config':<32} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Trades':>7} {'Det':>5} {'Bull':>5} {'Bear':>5}"
    )
    print("-" * 88)

    results: list[tuple[str, dict]] = []
    for label, kwargs in configs:
        result = run_backtest(data, track_metrics=True, **kwargs)
        results.append((label, result))
        pa = result.get("pattern_awareness") or {}
        print(
            f"{label:<32} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{result.get('nyse_signals', 0):>7} "
            f"{pa.get('detections', 0):>5} "
            f"{pa.get('bullish', 0):>5} "
            f"{pa.get('bearish', 0):>5}"
        )
        if pa:
            print(f"  patterns: {format_pattern_stats_report(pa)}")
        release_backtest_memory()

    print("-" * 88)
    if len(results) >= 2:
        _, base_r = results[0]
        best_return = base_r
        best_sharpe = base_r
        best_label_return = results[0][0]
        best_label_sharpe = results[0][0]
        pattern_results = results[1:]
        for label, res in pattern_results:
            if res["total_return_pct"] > best_return["total_return_pct"]:
                best_return = res
                best_label_return = label
            if res["sharpe"] > best_sharpe["sharpe"]:
                best_sharpe = res
                best_label_sharpe = label

        full_r = pattern_results[0][1] if len(pattern_results) >= 1 else None
        bear_r = pattern_results[1][1] if len(pattern_results) >= 2 else None
        if full_r:
            pa = full_r.get("pattern_awareness") or {}
            by_pat = pa.get("by_pattern") or {}
            if by_pat:
                top = sorted(by_pat.items(), key=lambda x: -x[1])[:6]
                print("Most common patterns detected (full mode):")
                for key, count in top:
                    pat_label = PATTERN_LABELS.get(key, (key, ""))[0]
                    print(f"  {pat_label}: {count}")

        if full_r and bear_r:
            print(
                f"Delta full vs baseline: "
                f"return {full_r['total_return_pct'] - base_r['total_return_pct']:+.2f}pp | "
                f"Sharpe {full_r['sharpe'] - base_r['sharpe']:+.2f} | "
                f"MaxDD {full_r['max_drawdown_pct'] - base_r['max_drawdown_pct']:+.2f}pp | "
                f"Trades {full_r.get('nyse_signals', 0) - base_r.get('nyse_signals', 0):+d}"
            )
            print(
                f"Delta bearish vs baseline: "
                f"return {bear_r['total_return_pct'] - base_r['total_return_pct']:+.2f}pp | "
                f"Sharpe {bear_r['sharpe'] - base_r['sharpe']:+.2f} | "
                f"MaxDD {bear_r['max_drawdown_pct'] - base_r['max_drawdown_pct']:+.2f}pp | "
                f"Trades {bear_r.get('nyse_signals', 0) - base_r.get('nyse_signals', 0):+d}"
            )

        bearish_ok = bear_r and (
            bear_r["total_return_pct"] >= base_r["total_return_pct"] - 0.5
            and bear_r["sharpe"] >= base_r["sharpe"]
        )
        full_ok = full_r and (
            full_r["total_return_pct"] >= base_r["total_return_pct"] - 0.5
            and full_r["sharpe"] >= base_r["sharpe"] + 0.02
        )
        if bearish_ok and not full_ok:
            print(
                "\nRecommendation: ENABLE as BEARISH FILTER ONLY on paper "
                "(PAPER_PATTERN_BEARISH_ONLY=true) — trims weak setups without "
                "bullish reorder drag. Keep PATTERN_AWARENESS_ENABLED=false by default."
            )
        elif full_ok:
            print(
                "\nRecommendation: OPTIONAL full mode on paper — tuned thresholds "
                "deliver neutral/positive return with Sharpe benefit. Live stays OFF."
            )
        elif bear_r and bear_r["total_return_pct"] >= base_r["total_return_pct"]:
            print(
                "\nRecommendation: BEARISH FILTER ONLY — best return preservation on "
                "this window. Skip full bullish+ bearish boosts."
            )
        else:
            print(
                "\nRecommendation: KEEP OFF by default — neither full nor bearish-only "
                "met neutral return + Sharpe goal on this window."
            )
        print(
            f"Best return arm: {best_label_return} ({best_return['total_return_pct']:+.2f}%) | "
            f"Best Sharpe arm: {best_label_sharpe} ({best_sharpe['sharpe']:.2f})"
        )
    print("-" * 88)


PIT_COMPARE_KWARGS = {
    **BEST_PAPER_V21_KWARGS,
    "paper_thinking": True,
    "with_news": True,
}


def run_pit_compare(
    days=None,
    refresh=False,
    use_max=False,
) -> None:
    """Compare relaxed (legacy) vs strict point-in-time backtest paths."""
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
        win_label = "max"
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
        win_label = f"{days}d"
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    configs = [
        (
            "Relaxed (legacy paths)",
            {**PIT_COMPARE_KWARGS, "strict_pit": False},
        ),
        (
            "Strict PIT",
            {**PIT_COMPARE_KWARGS, "strict_pit": True},
        ),
    ]

    print("--- POINT-IN-TIME BACKTEST A/B (thinking + news) ---")
    print(
        f"Window ({win_label}): {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        "Strict PIT: Wayback web only (no live headlines), macro sliced to bar, "
        "premarket thinking uses prior close; costs 8bps slip + 1bps comm equity"
    )
    print(f"{'Config':<28} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} {'Trades':>7} {'Cost%':>7}")
    print("-" * 72)

    results: list[tuple[str, dict]] = []
    for label, kwargs in configs:
        result = run_backtest(data, track_metrics=True, **kwargs)
        results.append((label, result))
        cost_pct = float(result.get("execution_cost_pct") or 0.0)
        print(
            f"{label:<28} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{result.get('nyse_signals', 0):>7} "
            f"{cost_pct:>6.3f}%"
        )
        release_backtest_memory()

    print("-" * 72)
    if len(results) == 2:
        _, base_r = results[0]
        _, pit_r = results[1]
        print(
            f"Delta (strict - relaxed): "
            f"return {pit_r['total_return_pct'] - base_r['total_return_pct']:+.2f}pp | "
            f"Sharpe {pit_r['sharpe'] - base_r['sharpe']:+.2f} | "
            f"MaxDD {pit_r['max_drawdown_pct'] - base_r['max_drawdown_pct']:+.2f}pp | "
            f"Trades {pit_r.get('nyse_signals', 0) - base_r.get('nyse_signals', 0):+d}"
        )
        if pit_r["total_return_pct"] <= base_r["total_return_pct"]:
            print(
                "\nRecommendation: Use --strict-pit for research validation; "
                "relaxed mode overstates edge when live web/macro leak into history."
            )
        else:
            print(
                "\nRecommendation: Strict PIT is the default for --paper-aggressive; "
                "relaxed mode is legacy comparison only."
            )
    print("-" * 72)


POSITION_SIZING_COMPARE_KWARGS = {
    **BEST_PAPER_V21_KWARGS,
    "strict_pit": True,
    "paper_thinking": True,
    "with_news": True,
}


def run_position_sizing_compare(
    days=None,
    refresh=False,
    use_max=False,
) -> None:
    """Compare baseline vs Top1 vol-based asymmetric position sizing (strict PIT)."""
    from modules.vol_position_sizing import format_vol_sizing_report

    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
        win_label = "max"
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
        win_label = f"{days}d"
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    configs = [
        (
            "v2.1 baseline sizing",
            {**POSITION_SIZING_COMPARE_KWARGS, "paper_vol_position_sizing": False},
        ),
        (
            "Top1 vol + asymmetric",
            {
                **POSITION_SIZING_COMPARE_KWARGS,
                "paper_vol_position_sizing": True,
                "top1_vol_conservative": False,
            },
        ),
    ]

    print("--- TOP1 VOL POSITION SIZING A/B (strict PIT, thinking + news) ---")
    print(
        f"Window ({win_label}): {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        "Rules when ON: 1% base risk x ATR vol scale x conviction (max 2x) | "
        "speculative max 0.5% | portfolio heat cap 7%"
    )
    print(
        f"{'Config':<28} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Trades':>7} {'Sized':>6}"
    )
    print("-" * 72)

    results: list[tuple[str, dict]] = []
    for label, kwargs in configs:
        result = run_backtest(data, track_metrics=True, **kwargs)
        results.append((label, result))
        vs = result.get("vol_position_sizing") or {}
        print(
            f"{label:<28} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{result.get('nyse_signals', 0):>7} "
            f"{vs.get('sized_buys', 0):>6}"
        )
        if vs:
            print(f"  sizing: {format_vol_sizing_report(vs)}")
        release_backtest_memory()

    print("-" * 72)
    if len(results) == 2:
        _, base_r = results[0]
        _, top_r = results[1]
        vs = top_r.get("vol_position_sizing") or {}
        by_sym = vs.get("by_symbol") or {}
        print(
            f"Delta (Top1 - baseline): "
            f"return {top_r['total_return_pct'] - base_r['total_return_pct']:+.2f}pp | "
            f"Sharpe {top_r['sharpe'] - base_r['sharpe']:+.2f} | "
            f"MaxDD {top_r['max_drawdown_pct'] - base_r['max_drawdown_pct']:+.2f}pp | "
            f"Trades {top_r.get('nyse_signals', 0) - base_r.get('nyse_signals', 0):+d}"
        )
        if "SPCX" in by_sym:
            s = by_sym["SPCX"]
            print(
                f"SPCX handling: {s.get('buys', 0)} sized buys, "
                f"avg risk {float(s.get('avg_risk_pct', 0))*100:.2f}%, "
                f"speculative caps {s.get('speculative', 0)}"
            )
        elif "SPCX" not in data.columns:
            print(
                "SPCX handling: no price column in backtest window "
                "(screener-only / not traded)"
            )
        else:
            print("SPCX handling: in universe but no Top1-sized buys this window")
        if (
            top_r["sharpe"] >= base_r["sharpe"]
            and top_r["max_drawdown_pct"] >= base_r["max_drawdown_pct"] - 0.5
        ):
            print(
                "\nRecommendation: ENABLE on paper research "
                "(PAPER_VOL_POSITION_SIZING_ENABLED=true) under strict PIT; "
                "live $300 account - trial with 0.5% speculative cap only after 2 weeks paper."
            )
        elif top_r["total_return_pct"] < base_r["total_return_pct"] - 2:
            print(
                "\nRecommendation: KEEP OFF — Top1 sizing reduced return on this window; "
                "heat cap or speculative trims may be too tight for paper aggressive."
            )
        else:
            print(
                "\nRecommendation: OPTIONAL on paper — marginal impact; keep OFF on live $300 "
                "until paper validates conviction boosts without return drag."
            )
    print("-" * 72)


LOSS_CUTTING_COMPARE_KWARGS = {
    **BEST_PAPER_V21_KWARGS,
    "strict_pit": True,
    "paper_thinking": True,
    "with_news": True,
}


def run_loss_cutting_compare(
    days=None,
    refresh=False,
    use_max=False,
) -> None:
    """Compare baseline vs Top1 asymmetric loss cutting (strict PIT)."""
    from modules.loss_cutting import format_loss_cutting_report

    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
        win_label = "max"
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
        win_label = f"{days}d"
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    configs = [
        (
            "v2.1 baseline exits",
            {**LOSS_CUTTING_COMPARE_KWARGS, "paper_loss_cutting": False},
        ),
        (
            "Top1 loss cutting",
            {
                **LOSS_CUTTING_COMPARE_KWARGS,
                "paper_loss_cutting": True,
                "top1_loss_conservative": False,
            },
        ),
    ]

    print("--- TOP1 LOSS CUTTING A/B (strict PIT, thinking + news) ---")
    print(
        f"Window ({win_label}): {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        "Rules when ON: stop -7% normal / -4% speculative | "
        "tighten to -5%/-3% on thinking/sector headwind | "
        "scale out 30/30/40 at +6/+12/+20% | "
        "conf>=0.7 uses 10% trailing stop after +6%"
    )
    print(
        f"{'Config':<28} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Trades':>7} {'Stops':>6} {'Take':>5}"
    )
    print("-" * 72)

    results: list[tuple[str, dict]] = []
    for label, kwargs in configs:
        result = run_backtest(data, track_metrics=True, **kwargs)
        results.append((label, result))
        lc = result.get("loss_cutting") or {}
        print(
            f"{label:<28} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{result.get('nyse_signals', 0):>7} "
            f"{lc.get('hard_stops', 0):>6} "
            f"{lc.get('partial_takes', 0):>5}"
        )
        if lc:
            print(f"  exits: {format_loss_cutting_report(lc)}")
        release_backtest_memory()

    print("-" * 72)
    if len(results) == 2:
        _, base_r = results[0]
        _, cut_r = results[1]
        lc = cut_r.get("loss_cutting") or {}
        by_sym = lc.get("by_symbol") or {}
        print(
            f"Delta (loss cutting - baseline): "
            f"return {cut_r['total_return_pct'] - base_r['total_return_pct']:+.2f}pp | "
            f"Sharpe {cut_r['sharpe'] - base_r['sharpe']:+.2f} | "
            f"MaxDD {cut_r['max_drawdown_pct'] - base_r['max_drawdown_pct']:+.2f}pp | "
            f"Trades {cut_r.get('nyse_signals', 0) - base_r.get('nyse_signals', 0):+d}"
        )
        spec_syms = ("SPCX", "SMCI", "COIN", "PLTR", "KTOS")
        spec_lines = []
        for s in spec_syms:
            row = by_sym.get(s)
            if row:
                spec_lines.append(
                    f"{s}: stops {row.get('hard_stops', 0)} "
                    f"partials {row.get('partial_takes', 0)} "
                    f"trails {row.get('trail_exits', 0)}"
                )
        if spec_lines:
            print("Speculative-name exits: " + "; ".join(spec_lines))
        elif "SPCX" not in data.columns:
            print(
                "SPCX-style impact: no SPCX price data in window; "
                f"speculative rules applied to {lc.get('hard_stops', 0)} hard stops total"
            )
        else:
            print("SPCX-style impact: no SPCX positions opened this window")
        if (
            cut_r["max_drawdown_pct"] >= base_r["max_drawdown_pct"]
            and cut_r["sharpe"] >= base_r["sharpe"] - 0.02
        ):
            print(
                "\nRecommendation: ENABLE on paper (PAPER_LOSS_CUTTING_ENABLED=true) "
                "under strict PIT - capital protection improved or matched with "
                "acceptable return tradeoff. Live $300: enable speculative -4% stop only "
                "after paper validation."
            )
        elif cut_r["total_return_pct"] < base_r["total_return_pct"] - 3:
            print(
                "\nRecommendation: KEEP OFF - early stops/partials cut winners on this window."
            )
        else:
            print(
                "\nRecommendation: OPTIONAL on paper - modest impact; prioritize "
                "speculative -4% cap for SPCX-class names on live $300."
            )
    print("-" * 72)


BLENDED_CONSERVATIVE_COMPARE_KWARGS = {
    **BEST_PAPER_V21_KWARGS,
    "strict_pit": True,
    "paper_thinking": True,
    "with_news": True,
    "paper_pattern_awareness": False,
}

# Prior conservative blend (spec -4%, no trailing) from 365d strict-PIT run
PREVIOUS_CONSERVATIVE_BLEND = {
    "total_return_pct": 58.62,
    "sharpe": 2.04,
    "max_drawdown_pct": -7.39,
    "nyse_signals": 15,
}


def run_blended_conservative_compare(
    days=None,
    refresh=False,
    use_max=False,
) -> None:
    """Compare v2.1 baseline vs conservative Top1 blend vs full individual features."""
    from modules.loss_cutting import format_loss_cutting_report
    from modules.vol_position_sizing import format_vol_sizing_report

    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
        win_label = "max"
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
        win_label = f"{days}d"
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    base_kwargs = {
        **BLENDED_CONSERVATIVE_COMPARE_KWARGS,
        "paper_vol_position_sizing": False,
        "paper_loss_cutting": False,
        "top1_vol_conservative": False,
        "top1_loss_conservative": False,
    }
    configs = [
        (
            "v2.1 baseline (no Top1)",
            dict(base_kwargs),
        ),
        (
            "Conservative blend",
            {
                **base_kwargs,
                "paper_vol_position_sizing": True,
                "paper_loss_cutting": True,
                "top1_vol_conservative": True,
                "top1_loss_conservative": True,
            },
        ),
        (
            "Full vol sizing only",
            {
                **base_kwargs,
                "paper_vol_position_sizing": True,
                "top1_vol_conservative": False,
            },
        ),
        (
            "Full loss cutting only",
            {
                **base_kwargs,
                "paper_loss_cutting": True,
                "top1_loss_conservative": False,
            },
        ),
        (
            "Full Top1 combined",
            {
                **base_kwargs,
                "paper_vol_position_sizing": True,
                "paper_loss_cutting": True,
                "top1_vol_conservative": False,
                "top1_loss_conservative": False,
            },
        ),
    ]

    print("--- TOP1 CONSERVATIVE BLEND COMPARE (strict PIT, thinking + news) ---")
    print(
        f"Window ({win_label}): {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        "Conservative blend: speculative 0.5% cap + mild ATR scale (0.75-1.25x) | "
        "speculative -5% stop (ATR widen) | mild trail +8% conf>=0.65 (12% trail) | "
        "NO heat cap | NO partials | patterns OFF"
    )
    print(
        "Full vol sizing: 1% base x ATR x conviction | spec 0.5% | heat cap 7%"
    )
    print(
        "Full loss cutting: -7%/-4% stops | tighten on headwind | "
        "partials +6/+12/+20 | trailing after +6%"
    )
    print(
        f"{'Config':<26} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Trades':>7} {'Sized':>6} {'Stops':>6}"
    )
    print("-" * 78)

    results: list[tuple[str, dict]] = []
    for label, kwargs in configs:
        result = run_backtest(data, track_metrics=True, **kwargs)
        results.append((label, result))
        vs = result.get("vol_position_sizing") or {}
        lc = result.get("loss_cutting") or {}
        print(
            f"{label:<26} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{result.get('nyse_signals', 0):>7} "
            f"{vs.get('sized_buys', 0):>6} "
            f"{lc.get('hard_stops', 0):>6}"
        )
        if vs:
            print(f"  sizing: {format_vol_sizing_report(vs)}")
        if lc:
            print(f"  exits: {format_loss_cutting_report(lc)}")
        release_backtest_memory()

    print("-" * 78)
    if len(results) >= 2:
        base_label, base_r = results[0]
        by_label = {label: res for label, res in results}
        cons_r = by_label.get("Conservative blend")
        full_vol_r = by_label.get("Full vol sizing only")
        full_cut_r = by_label.get("Full loss cutting only")
        full_combo_r = by_label.get("Full Top1 combined")

        print("\nDelta vs v2.1 baseline:")
        for label, res in results[1:]:
            print(
                f"  {label}: "
                f"return {res['total_return_pct'] - base_r['total_return_pct']:+.2f}pp | "
                f"Sharpe {res['sharpe'] - base_r['sharpe']:+.2f} | "
                f"MaxDD {res['max_drawdown_pct'] - base_r['max_drawdown_pct']:+.2f}pp | "
                f"Trades {res.get('nyse_signals', 0) - base_r.get('nyse_signals', 0):+d}"
            )

        if cons_r and full_vol_r and full_cut_r:
            print("\nDelta conservative blend vs full individual features:")
            print(
                f"  vs full vol sizing: "
                f"return {cons_r['total_return_pct'] - full_vol_r['total_return_pct']:+.2f}pp | "
                f"Sharpe {cons_r['sharpe'] - full_vol_r['sharpe']:+.2f} | "
                f"MaxDD {cons_r['max_drawdown_pct'] - full_vol_r['max_drawdown_pct']:+.2f}pp"
            )
            print(
                f"  vs full loss cutting: "
                f"return {cons_r['total_return_pct'] - full_cut_r['total_return_pct']:+.2f}pp | "
                f"Sharpe {cons_r['sharpe'] - full_cut_r['sharpe']:+.2f} | "
                f"MaxDD {cons_r['max_drawdown_pct'] - full_cut_r['max_drawdown_pct']:+.2f}pp"
            )
            if full_combo_r:
                print(
                    f"  vs full combined Top1: "
                    f"return {cons_r['total_return_pct'] - full_combo_r['total_return_pct']:+.2f}pp | "
                    f"Sharpe {cons_r['sharpe'] - full_combo_r['sharpe']:+.2f} | "
                    f"MaxDD {cons_r['max_drawdown_pct'] - full_combo_r['max_drawdown_pct']:+.2f}pp"
                )

        if cons_r:
            prev = PREVIOUS_CONSERVATIVE_BLEND
            print(
                "\nDelta refined conservative blend vs prior blend "
                "(spec -4% stop, no trailing):"
            )
            print(
                f"  return {cons_r['total_return_pct'] - prev['total_return_pct']:+.2f}pp | "
                f"Sharpe {cons_r['sharpe'] - prev['sharpe']:+.2f} | "
                f"MaxDD {cons_r['max_drawdown_pct'] - prev['max_drawdown_pct']:+.2f}pp | "
                f"Trades {cons_r.get('nyse_signals', 0) - prev['nyse_signals']:+d}"
            )
            better_than_prev = (
                cons_r["sharpe"] > prev["sharpe"]
                or (
                    cons_r["total_return_pct"] > prev["total_return_pct"]
                    and cons_r["max_drawdown_pct"] >= prev["max_drawdown_pct"] - 0.3
                )
            )
            better_than_base = cons_r["total_return_pct"] >= base_r["total_return_pct"] and (
                cons_r["sharpe"] >= base_r["sharpe"] - 0.02
            )
            if better_than_prev and better_than_base:
                print(
                    "\nOverall: REFINED conservative blend is BETTER than prior blend "
                    "and baseline on this window."
                )
            elif better_than_base:
                print(
                    "\nOverall: REFINED blend beats baseline but is mixed vs prior "
                    "blend (-4% / no trail); prefer refined if Sharpe holds on paper."
                )
            elif better_than_prev:
                print(
                    "\nOverall: REFINED blend improved vs prior blend but still trails "
                    "baseline; keep OFF until validated."
                )
            else:
                print(
                    "\nOverall: REFINED blend did NOT beat prior blend or baseline; "
                    "revert to prior conservative settings or keep OFF."
                )

        cons_lc = (cons_r or {}).get("loss_cutting") or {}
        cons_vs = (cons_r or {}).get("vol_position_sizing") or {}
        by_sym = cons_lc.get("by_symbol") or {}
        spec_syms = ("SPCX", "SMCI", "COIN", "PLTR", "KTOS")
        spec_lines = []
        for s in spec_syms:
            row = by_sym.get(s)
            vs_row = (cons_vs.get("by_symbol") or {}).get(s)
            if row or vs_row:
                parts = [s + ":"]
                if vs_row:
                    parts.append(
                        f"sized {vs_row.get('buys', 0)} "
                        f"spec={vs_row.get('speculative', 0)}"
                    )
                if row:
                    parts.append(f"stops {row.get('hard_stops', 0)}")
                spec_lines.append(" ".join(parts))
        if spec_lines:
            print("\nConservative blend speculative-name impact:")
            for line in spec_lines:
                print(f"  {line}")
        elif cons_r:
            print(
                f"\nConservative blend speculative impact: "
                f"{cons_vs.get('speculative_caps', 0)} spec caps | "
                f"{cons_lc.get('hard_stops', 0)} speculative stops"
            )

        if cons_r:
            beats_baseline = (
                cons_r["sharpe"] >= base_r["sharpe"]
                and cons_r["max_drawdown_pct"] >= base_r["max_drawdown_pct"] - 0.5
            )
            beats_full_vol = full_vol_r and cons_r["total_return_pct"] >= (
                full_vol_r["total_return_pct"] - 0.5
            )
            beats_full_cut = full_cut_r and cons_r["sharpe"] >= (
                full_cut_r["sharpe"] - 0.02
            )
            if beats_baseline and (beats_full_vol or beats_full_cut):
                print(
                    "\nRecommendation: ENABLE conservative blend on paper "
                    "(TOP1_VOL_SIZING_CONSERVATIVE=true + TOP1_LOSS_CUT_CONSERVATIVE=true "
                    "with PAPER_VOL_POSITION_SIZING_ENABLED and PAPER_LOSS_CUTTING_ENABLED). "
                    "Partial Top1 rules improve or match baseline with less drag than full features."
                )
            elif cons_r["total_return_pct"] >= base_r["total_return_pct"] - 1.0:
                print(
                    "\nRecommendation: OPTIONAL on paper - conservative blend is near baseline; "
                    "enable spec cap + spec -4% stop only after 2 weeks paper validation. "
                    "Keep full Top1 features OFF."
                )
            else:
                print(
                    "\nRecommendation: KEEP OFF on paper - conservative blend underperformed "
                    "baseline on this window. Revisit after screener adds more speculative names."
                )
    print("-" * 78)


def run_dynamic_vti_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare fixed 20% VTI vs dynamic 40-75% VTI on paper aggressive profile."""
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    configs = [
        (
            f"Fixed {config.PAPER_VTI_CORE_PCT:.0%} VTI (paper)",
            {"paper_aggressive": True, "paper_dynamic_vti": False},
        ),
        (
            "Dynamic VTI 40-75% (paper)",
            {"paper_aggressive": True, "paper_dynamic_vti": True},
        ),
    ]
    print("--- PAPER DYNAMIC VTI A/B (social/macro off, sleeve flags on) ---")
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        f"Paper boost: {config.PAPER_ACTIVE_SLEEVE_BOOST:.0%}x | "
        f"social {config.PAPER_SOCIAL_SLEEVE_CAP_PCT:.0%} | "
        f"PAPER_DYNAMIC_VTI default={config.PAPER_DYNAMIC_VTI_ENABLED}"
    )
    print(
        f"{'Config':<28} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'AvgAct':>7} {'VTIavg':>7}"
    )
    print("-" * 72)

    for label, kwargs in configs:
        result = run_backtest(data, track_active_exposure=True, **kwargs)
        print(
            f"{label:<28} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{result['avg_active_exposure_pct']:>6.1f}% "
            f"{result['vti_core_pct'] * 100:>6.1f}%"
        )
    print("-" * 72)


def run_paper_aggressive_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare live-like 80/20 vs paper aggressive 20/80 profile."""
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    configs = [
        ("Live-like 80/20 VTI", {"vti_core_pct": 0.80, "paper_aggressive": False}),
        (
            f"Paper aggressive {config.PAPER_VTI_CORE_PCT:.0%} VTI",
            {"vti_core_pct": 0.0, "paper_aggressive": True},
        ),
        ("Active only (no VTI)", {"vti_core_pct": 0.0, "paper_aggressive": False}),
    ]
    print("--- PAPER AGGRESSIVE A/B (paper profile, social/macro off) ---")
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        f"Paper boost: {config.PAPER_ACTIVE_SLEEVE_BOOST:.0%}x | "
        f"social {config.PAPER_SOCIAL_SLEEVE_CAP_PCT:.0%} | "
        f"crypto vol-only {config.PAPER_CRYPTO_VOL_ONLY}"
    )
    print(
        f"{'Config':<32} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Final $':>10} {'Social':>8}"
    )
    print("-" * 78)

    for label, kwargs in configs:
        result = run_backtest(data, **kwargs)
        social = result.get("social_sleeve")
        social_ret = f"{social['return_pct']:+.1f}%" if social else "—"
        print(
            f"{label:<32} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{result['final_equity']:>10,.2f} "
            f"{social_ret:>8}"
        )
    print("-" * 78)


def run_vti_core_compare(
    days=None,
    refresh=False,
    use_max=False,
    *,
    small_account: bool = False,
) -> None:
    """Run baseline vs 70/30 and 80/20 VTI core + active bot."""
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    if small_account:
        configs = [
            (
                f"Small-account live ({config.SMALL_ACCOUNT_VTI_CORE_PCT:.0%} VTI)",
                config.SMALL_ACCOUNT_VTI_CORE_PCT,
            ),
            ("80% VTI core / 20% active", 0.80),
            ("Active only (no VTI)", 0.0),
        ]
        title = (
            f"--- SMALL-ACCOUNT VTI A/B (${_small_account_start_equity():,.0f} start, "
            f"risk {config.SMALL_ACCOUNT_RISK_PER_TRADE:.0%}, "
            f"max ${config.SMALL_ACCOUNT_MAX_NOTIONAL:,.0f}/order) ---"
        )
    else:
        configs = [
            ("Active only (current)", 0.0),
            ("70% VTI core / 30% active", 0.70),
            ("80% VTI core / 20% active", 0.80),
        ]
        title = "--- VTI CORE A/B (same window, parallel social sleeve off) ---"
    print(title)
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(f"{'Config':<32} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} {'Final $':>10}")
    print("-" * 70)

    saved_social = config.SOCIAL_SLEEVE_ENABLED
    config.SOCIAL_SLEEVE_ENABLED = False
    try:
        for label, core in configs:
            result = run_backtest(
                data,
                vti_core_pct=core,
                small_account=small_account,
            )
            print(
                f"{label:<32} "
                f"{result['total_return_pct']:>+7.2f}% "
                f"{result['sharpe']:>7.2f} "
                f"{result['max_drawdown_pct']:>7.2f}% "
                f"{result['final_equity']:>10,.2f}"
            )
    finally:
        config.SOCIAL_SLEEVE_ENABLED = saved_social
    print("-" * 70)


def run_performance_test(
    days=None,
    refresh=False,
    use_max=False,
    vti_core_pct: float = 0.0,
    paper_aggressive: bool = False,
    small_account: bool = False,
):
    if use_max:
        print("--- STARTING FUND BACKTEST (max available daily history) ---")
    else:
        days = days or config.BACKTEST_DAYS
        print(f"--- STARTING FUND BACKTEST ({days} days) ---")
    if small_account:
        print(
            f"--- SMALL-ACCOUNT MODE: ${config.SMALL_ACCOUNT_BACKTEST_EQUITY:,.0f} start | "
            f"{config.SMALL_ACCOUNT_VTI_CORE_PCT:.0%} {VTI_CORE_SYMBOL} | "
            f"risk {config.SMALL_ACCOUNT_RISK_PER_TRADE:.0%} | "
            f"max ${config.SMALL_ACCOUNT_MAX_NOTIONAL:,.0f}/order ---"
        )
        if vti_core_pct <= 0:
            vti_core_pct = config.SMALL_ACCOUNT_VTI_CORE_PCT
    elif paper_aggressive:
        if config.PAPER_DYNAMIC_VTI_ENABLED:
            print(
                f"--- PAPER AGGRESSIVE: dynamic {VTI_CORE_SYMBOL} "
                f"({config.DYNAMIC_VTI_PAPER_FLOOR:.0%}-75% by vol/stress) | "
                f"boost {config.PAPER_ACTIVE_SLEEVE_BOOST:.0%}x ---"
            )
        else:
            print(
                f"--- PAPER AGGRESSIVE: {config.PAPER_VTI_CORE_PCT:.0%} {VTI_CORE_SYMBOL} | "
                f"{1 - config.PAPER_VTI_CORE_PCT:.0%} active (boost "
                f"{config.PAPER_ACTIVE_SLEEVE_BOOST:.0%}x) ---"
            )
    elif vti_core_pct > 0:
        print(
            f"--- VTI core: {vti_core_pct:.0%} passive {VTI_CORE_SYMBOL} | "
            f"{1 - vti_core_pct:.0%} active sleeves ---"
        )
    try:
        data = _ensure_daily_data(days or 0, refresh=refresh, use_max=use_max)
    except Exception as e:
        print("Database error: " + str(e))
        return
    if len(data) < MIN_HISTORY:
        sim_target = days or config.BACKTEST_DAYS
        print(
            f"Need at least {MIN_HISTORY} daily bars for SPY MA{config.SPY_MA_WINDOW} "
            f"warmup; got {len(data)}."
        )
        print(
            "Run: python fetch_data.py --daily --days "
            f"{_calendar_days_to_fetch(sim_target)}"
        )
        print("Or:  python fetch_data.py --daily --max")
        return

    start_date = data.index[MIN_HISTORY]
    end_date = data.index[-1]
    cooldown_bars = DAILY_COOLDOWN_BARS
    bar_label = "daily bars"
    sim_bars = len(data) - MIN_HISTORY
    sim_days = (end_date - start_date).days

    print(f"Loaded {len(data.columns)} tickers over {len(data)} {bar_label}.")
    print(
        f"Warmup: {MIN_HISTORY} bars (SPY MA{config.SPY_MA_WINDOW}) | "
        f"Simulation: {sim_bars} bars"
    )
    print(f"Simulation window: {start_date.date()} to {end_date.date()} ({sim_days} calendar days)")
    print(f"Cooldown: {cooldown_bars} bar(s) (~{COOLDOWN_SECONDS // 60} min live logic)")

    saved_paper_ctx = config.paper_aggressive_context()
    saved_small_ctx = config.backtest_small_account_context()
    config.set_paper_aggressive_context(paper_aggressive)
    config.set_backtest_small_account_context(small_account)
    try:
        stack_profile = "paper" if paper_aggressive else "live"
        config.print_recommended_stack_flags(profile=stack_profile)
        alloc = config.fund_allocation_pct()
    finally:
        config.set_paper_aggressive_context(saved_paper_ctx)
        config.set_backtest_small_account_context(saved_small_ctx)

    result = run_backtest(
        data,
        track_spy_fill=False,
        verbose=True,
        vti_core_pct=vti_core_pct,
        paper_aggressive=paper_aggressive,
        small_account=small_account,
        strict_pit=RUN_OPTIONS.strict_pit,
    )
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
    tag = " (paper aggressive)" if paper_aggressive else ""
    print(
        f"Sleeves{tag}:      SPY {alloc['spy']:.1%} | "
        f"crypto {alloc['crypto']:.1%} | "
        f"NYSE {alloc['nyse']:.1%} | "
        f"metal {alloc['metal']:.1%} | "
        f"cash {alloc['cash_buffer']:.1%}"
    )
    if alloc.get("vti_core"):
        print(f"VTI core:         {alloc['vti_core']:.1%}")
    print(f"Crypto vol-only:  {config.effective_crypto_vol_only()}")
    print(f"Final Equity:     ${curve_end}")
    print(f"Total Return:     {round(total_ret, 2)}%")
    if bench is not None:
        print(f"VTI Buy & Hold:   {round(bench, 2)}%")
    print(f"Sharpe Ratio:     {round(sharpe, 2)}")
    print(f"Sortino Ratio:    {round(result.get('sortino', 0), 2)}")
    print(f"Calmar Ratio:     {round(result.get('calmar', 0), 2)}")
    print(f"Win rate (daily): {round(result.get('win_rate_pct', 0), 1)}%")
    print(f"Max Drawdown:     {round(max_dd, 2)}%")
    print(f"SPY signals:      {total_spy}")
    print(f"Crypto signals:   {total_crypto}")
    print(f"NYSE signals:     {total_equity}")
    print(f"Total orders:     {total_orders}")
    social = result.get("social_sleeve")
    if social:
        print(
            f"Social sleeve:    {social['trades']} trades | "
            f"{social['cap_pct']:.0%} parallel paper book | "
            f"${social['final_equity']} ({social['return_pct']:+.2f}%)"
        )
        print("                  (XOM proxies XLE when XLE daily bars missing)")
    elif config.SOCIAL_SLEEVE_ENABLED:
        print("Social sleeve:    enabled (no trades)")
    print("Regime distribution:")
    for name, count in sorted(regime_counts.items(), key=lambda x: -x[1]):
        print(f"  {name}: {count}")
    print(f"Profit factor:    {round(result.get('profit_factor', 0), 2)}")
    print(f"Avg trade return: {round(result.get('avg_trade_return_pct', 0), 3)}%")
    if result.get("rolling_sharpe_mean") is not None:
        print(f"Rolling Sharpe:   {round(result['rolling_sharpe_mean'], 2)} (mean {ROLLING_METRIC_WINDOW}d)")

    folds = RUN_OPTIONS.walk_forward_folds
    if folds >= 2 and not RUN_OPTIONS.fast_mode:
        wf = walk_forward_purged(
            data,
            min_history=MIN_HISTORY,
            n_folds=folds,
            run_fn=lambda d, _tb, _te: run_backtest(
                d,
                track_active_exposure=True,
                track_metrics=True,
                paper_aggressive=paper_aggressive,
                small_account=small_account,
                vti_core_pct=vti_core_pct,
            ),
        )
        if wf:
            print_table(format_walk_forward_table(wf, purged=True), title="Purged walk-forward")

    if RUN_OPTIONS.slippage_sensitivity:

        def _slip_run():
            return run_backtest(
                data,
                track_active_exposure=True,
                track_metrics=True,
                vti_core_pct=vti_core_pct,
                paper_aggressive=paper_aggressive,
                small_account=small_account,
            )

        slip_rows = run_slippage_sensitivity(_slip_run)
        print_table(format_slippage_table(slip_rows), title="Slippage sensitivity")

    if RUN_OPTIONS.export_json:
        path = export_results_json(result, RUN_OPTIONS.export_json)
        print(f"JSON export: {path.resolve()}")

    if RUN_OPTIONS.export_csv:
        row = {
            "start": str(start_date.date()),
            "end": str(end_date.date()),
            "return_pct": total_ret,
            "sharpe": sharpe,
            "sortino": result.get("sortino"),
            "calmar": result.get("calmar"),
            "max_dd_pct": max_dd,
            "win_rate_pct": result.get("win_rate_pct"),
            "profit_factor": result.get("profit_factor"),
            "avg_trade_return_pct": result.get("avg_trade_return_pct"),
            "total_orders": total_orders,
        }
        path = export_results_csv([row], RUN_OPTIONS.export_csv)
        print(f"CSV export: {path.resolve()}")

    if RUN_OPTIONS.report_html:
        html_path = generate_html_report(result, RUN_OPTIONS.report_html)
        print(f"HTML report: {html_path.resolve()}")

    print("---------------------------------------------------")


if __name__ == "__main__":
    from modules.logging_utils import setup_project_logging

    setup_project_logging()
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
    parser.add_argument(
        "--vti-core",
        type=float,
        default=0.0,
        metavar="PCT",
        help="Passive VTI core fraction (e.g. 0.7 = 70%% VTI, 30%% active bot)",
    )
    parser.add_argument(
        "--compare-vti-core",
        action="store_true",
        help="Run baseline vs 70/30 and 80/20 VTI core (table output)",
    )
    parser.add_argument(
        "--paper-aggressive",
        action="store_true",
        help="Paper research profile: 20%% VTI, boosted sleeves, social 20%%, crypto all vol",
    )
    parser.add_argument(
        "--small-account",
        action="store_true",
        help=(
            "Live small-account profile: $100 start, 90%% VTI, 1%% risk, "
            "$10 max order, scaled min notional"
        ),
    )
    parser.add_argument(
        "--compare-paper-aggressive",
        action="store_true",
        help="Compare live 80/20 vs paper aggressive vs active-only (table)",
    )
    parser.add_argument(
        "--compare-dynamic-universe",
        action="store_true",
        help="Compare static UNIVERSE vs strict dynamic screener (paper aggressive)",
    )
    parser.add_argument(
        "--compare-new-markets",
        action="store_true",
        help=(
            "Compare Best Paper v2.1 baseline vs international ADR vs bond vs both "
            "(requires --paper-aggressive)"
        ),
    )
    parser.add_argument(
        "--compare-international-sleeve",
        action="store_true",
        help="Compare paper aggressive with vs without international ADR sleeve",
    )
    parser.add_argument(
        "--compare-ipo-rules",
        action="store_true",
        help="Compare dynamic strict with vs without IPO safety rules (paper aggressive)",
    )
    parser.add_argument(
        "--compare-profit-target",
        action="store_true",
        help="Compare paper aggressive with vs without trailing profit targets",
    )
    parser.add_argument(
        "--compare-scaling-strategy",
        action="store_true",
        help=(
            "Compare Best Paper v2.1 vs partial take-profits + dip rebuy "
            "(realistic slippage defaults)"
        ),
    )
    parser.add_argument(
        "--focus-ticker",
        default=None,
        metavar="SYMBOL",
        help="Highlight ticker in --compare-scaling-strategy output (e.g. SPCX)",
    )
    parser.add_argument(
        "--compare-patterns",
        action="store_true",
        help="Compare Best Paper v2.1 with vs without chart pattern awareness",
    )
    parser.add_argument(
        "--strict-pit",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Point-in-time news/social/thinking (default ON with --paper-aggressive)",
    )
    parser.add_argument(
        "--compare-pit",
        action="store_true",
        help="Compare relaxed vs strict point-in-time backtest (requires --paper-aggressive)",
    )
    parser.add_argument(
        "--compare-position-sizing",
        action="store_true",
        help="Compare baseline vs Top1 vol-based asymmetric sizing (requires --paper-aggressive)",
    )
    parser.add_argument(
        "--compare-loss-cutting",
        action="store_true",
        help="Compare baseline vs Top1 asymmetric loss cutting (requires --paper-aggressive)",
    )
    parser.add_argument(
        "--compare-blended-conservative",
        action="store_true",
        help=(
            "Compare v2.1 baseline vs conservative Top1 blend vs full vol/loss features "
            "(requires --paper-aggressive)"
        ),
    )
    parser.add_argument(
        "--compare-dynamic-vti",
        action="store_true",
        help="Compare fixed 20%% VTI vs dynamic 40-75%% VTI (paper aggressive)",
    )
    parser.add_argument(
        "--compare-paper-sleeve-features",
        action="store_true",
        help="Compare paper aggressive with vs without advanced sleeve flags",
    )
    parser.add_argument(
        "--compare-felix-social",
        action="store_true",
        help="Compare legacy vs enhanced Felix/social macro sleeve (paper aggressive)",
    )
    parser.add_argument(
        "--compare-macro-regime",
        action="store_true",
        help="Compare paper aggressive with vs without Regime Shift Detector",
    )
    parser.add_argument(
        "--compare-regime",
        action="store_true",
        help="Alias for --compare-macro-regime (Regime Shift Detector A/B)",
    )
    parser.add_argument(
        "--compare-options",
        action="store_true",
        help="Compare paper aggressive with vs without options income sleeve",
    )
    parser.add_argument(
        "--compare-dynamic-risk",
        action="store_true",
        help="Compare paper aggressive fixed vs dynamic risk per trade (vol/regime/stress)",
    )
    parser.add_argument(
        "--compare-pairs",
        action="store_true",
        help="Compare paper aggressive with vs without market-neutral pairs",
    )
    parser.add_argument(
        "--compare-vol",
        action="store_true",
        help="Compare paper aggressive with vs without VIX vol overlay sleeve",
    )
    parser.add_argument(
        "--compare-risk-parity",
        action="store_true",
        help="Compare paper aggressive with vs without risk parity + pod limits",
    )
    parser.add_argument(
        "--compare-sector-rotation",
        action="store_true",
        help=(
            "Compare paper aggressive with vs without sector rotation "
            "(use with --with-news for thinking+news A/B)"
        ),
    )
    parser.add_argument(
        "--compare-tech-guard",
        action="store_true",
        help=(
            "Compare paper aggressive with vs without tech concentration guard "
            "(use with --with-news for thinking+news A/B)"
        ),
    )
    parser.add_argument(
        "--compare-thinking",
        action="store_true",
        help=(
            "Compare paper aggressive with vs without thinking-engine sleeve tilts "
            "(use with --with-news for news-aware paper A/B)"
        ),
    )
    parser.add_argument(
        "--compare-thinking-impact",
        action="store_true",
        help=(
            "Isolate Thinking+News ON vs OFF on conservative blend "
            "(requires --paper-aggressive, uses --strict-pit)"
        ),
    )
    parser.add_argument(
        "--simulate-live-thinking",
        action="store_true",
        help="Compare small-account live sim with vs without capped thinking tilts (±6%%)",
    )
    parser.add_argument(
        "--with-news",
        action="store_true",
        help=(
            "With --compare-thinking or --simulate-live-thinking: synthetic 8 AM digest "
            "+ practical tilt guards (impact gates, max 3 sleeves)"
        ),
    )
    parser.add_argument(
        "--start-equity",
        type=float,
        default=None,
        metavar="USD",
        help="Starting equity for small-account / live-thinking sim (default: SMALL_ACCOUNT_BACKTEST_EQUITY or 300 with --with-news)",
    )
    parser.add_argument(
        "--compare-stat-arb",
        action="store_true",
        help="Compare paper aggressive with vs without statistical arbitrage sleeve",
    )
    parser.add_argument(
        "--compare-stat-arb-optimized",
        action="store_true",
        help="Compare current vs optimized statistical arbitrage sleeve",
    )
    parser.add_argument(
        "--compare-crypto-v2",
        action="store_true",
        help="Compare stat-arb crypto vs dual-entry crypto v2 sleeve (paper aggressive)",
    )
    parser.add_argument(
        "--compare-crypto-universe",
        action="store_true",
        help="Compare base vs expanded Alpaca crypto universe (paper aggressive)",
    )
    parser.add_argument(
        "--compare-vti-levels",
        action="store_true",
        help=(
            "Fixed VTI levels (90/80/75/70%%). With --simulate-live-thinking: "
            "Live Profile A sweep (crypto OFF; default thinking OFF). "
            "Alone: requires --paper-aggressive (paper bot + thinking)."
        ),
    )
    parser.add_argument(
        "--vti-levels",
        action="store_true",
        help="With --simulate-live-thinking: sweep 90/80/70/60%% VTI base levels",
    )
    parser.add_argument(
        "--compare-final",
        action="store_true",
        help="Final comprehensive paper bot vs legacy, VTI, small-account sim",
    )
    parser.add_argument(
        "--final-all-windows",
        action="store_true",
        help="With --compare-final: run 365d + 1000d + max and write full report",
    )
    parser.add_argument(
        "--final-report",
        type=Path,
        default=FINAL_REPORT_DEFAULT,
        help="Markdown report path for --compare-final",
    )
    parser.add_argument(
        "--fast-mode",
        action="store_true",
        help="Quick test: smaller ticker universe, no thinking engine",
    )
    parser.add_argument(
        "--no-thinking",
        action="store_true",
        help="Disable thinking-engine tilts for this run",
    )
    parser.add_argument(
        "--equity-slippage-bps",
        type=float,
        default=None,
        metavar="BPS",
        help=f"Equity/ETF slippage bps (default {DEFAULT_EQUITY_SLIPPAGE_BPS} with realistic costs)",
    )
    parser.add_argument(
        "--crypto-slippage-bps",
        type=float,
        default=None,
        metavar="BPS",
        help=f"Crypto slippage bps (default {DEFAULT_CRYPTO_SLIPPAGE_BPS} with realistic costs)",
    )
    parser.add_argument(
        "--equity-commission-bps",
        type=float,
        default=float(os.getenv("BACKTEST_EQUITY_COMMISSION_BPS", "0")),
        help="Extra equity commission bps per trade (Alpaca stocks $0; default 0)",
    )
    parser.add_argument(
        "--crypto-commission-bps",
        type=float,
        default=float(os.getenv("BACKTEST_CRYPTO_COMMISSION_BPS", "0")),
        help="Extra crypto commission bps on top of taker fee (default 0)",
    )
    parser.add_argument(
        "--no-realistic-costs",
        action="store_true",
        help="Disable default 5/10 bps slippage; use zero unless --equity-slippage-bps set",
    )
    parser.add_argument(
        "--walk-forward",
        type=int,
        default=0,
        metavar="FOLDS",
        help="Purged N-fold walk-forward (with --compare-final or standalone run)",
    )
    parser.add_argument(
        "--report-html",
        nargs="?",
        const=str(DEFAULT_HTML_REPORT),
        default=None,
        metavar="PATH",
        help="Write HTML report with equity, rolling Sharpe, drawdown charts",
    )
    parser.add_argument(
        "--export-json",
        nargs="?",
        const=str(DEFAULT_EXPORT_JSON),
        default=None,
        metavar="PATH",
        help="Export last run metrics to JSON",
    )
    parser.add_argument(
        "--export-csv",
        nargs="?",
        const=str(DEFAULT_EXPORT_CSV),
        default=None,
        metavar="PATH",
        help="Export last run summary row to CSV",
    )
    parser.add_argument(
        "--no-parallel",
        action="store_true",
        help="Disable parallel --compare-final arms (sequential fallback)",
    )
    parser.add_argument(
        "--slippage-sensitivity",
        action="store_true",
        help="Run slippage sweep (0/5/10/25 bps) after main backtest",
    )
    args = parser.parse_args()

    RUN_OPTIONS.fast_mode = bool(args.fast_mode)
    RUN_OPTIONS.no_thinking = bool(args.no_thinking)
    RUN_OPTIONS.realistic_costs = not args.no_realistic_costs
    if args.strict_pit is None:
        RUN_OPTIONS.strict_pit = bool(args.paper_aggressive)
    else:
        RUN_OPTIONS.strict_pit = bool(args.strict_pit)
    if args.equity_slippage_bps is not None:
        RUN_OPTIONS.equity_slippage_bps = max(0.0, float(args.equity_slippage_bps))
    if args.crypto_slippage_bps is not None:
        RUN_OPTIONS.crypto_slippage_bps = max(0.0, float(args.crypto_slippage_bps))
    RUN_OPTIONS.equity_commission_bps = max(0.0, float(args.equity_commission_bps))
    RUN_OPTIONS.crypto_commission_bps = max(0.0, float(args.crypto_commission_bps))
    apply_default_execution_costs()
    if RUN_OPTIONS.strict_pit:
        from modules.pit_replay import apply_strict_pit_execution_costs

        apply_strict_pit_execution_costs(RUN_OPTIONS)
    RUN_OPTIONS.walk_forward_folds = max(0, int(args.walk_forward))
    RUN_OPTIONS.full_accuracy = not RUN_OPTIONS.fast_mode
    RUN_OPTIONS.parallel_arms = not args.no_parallel
    RUN_OPTIONS.slippage_sensitivity = bool(args.slippage_sensitivity)
    if args.report_html is not None:
        RUN_OPTIONS.report_html = Path(args.report_html)
    if args.export_json is not None:
        RUN_OPTIONS.export_json = Path(args.export_json)
    if args.export_csv is not None:
        RUN_OPTIONS.export_csv = Path(args.export_csv)
    if args.refresh:
        reset_caches()
    if RUN_OPTIONS.fast_mode:
        print(
            f"--- FAST MODE: ~{FAST_MODE_MAX_TICKERS} tickers, thinking/stat-arb/vol/options off "
            "(use full run for final numbers) ---"
        )
    if RUN_OPTIONS.parallel_arms and args.compare_final:
        print(f"--- Parallel compare arms (max {RUN_OPTIONS.max_workers} workers) ---")
    if args.compare_vti_core:
        run_vti_core_compare(
            days=args.days,
            refresh=args.refresh,
            use_max=args.max,
            small_account=args.small_account,
        )
    elif args.compare_paper_aggressive:
        run_paper_aggressive_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_dynamic_universe:
        if not args.paper_aggressive:
            print("--compare-dynamic-universe requires --paper-aggressive")
            sys.exit(1)
        run_dynamic_universe_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_new_markets:
        if not args.paper_aggressive:
            print("--compare-new-markets requires --paper-aggressive")
            sys.exit(1)
        run_new_markets_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_international_sleeve:
        if not args.paper_aggressive:
            print("--compare-international-sleeve requires --paper-aggressive")
            sys.exit(1)
        run_international_sleeve_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_ipo_rules:
        if not args.paper_aggressive:
            print("--compare-ipo-rules requires --paper-aggressive")
            sys.exit(1)
        run_ipo_rules_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_profit_target:
        if not args.paper_aggressive:
            print("--compare-profit-target requires --paper-aggressive")
            sys.exit(1)
        run_profit_target_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_pit:
        if not args.paper_aggressive:
            print("--compare-pit requires --paper-aggressive")
            sys.exit(1)
        run_pit_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_position_sizing:
        if not args.paper_aggressive:
            print("--compare-position-sizing requires --paper-aggressive")
            sys.exit(1)
        run_position_sizing_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_loss_cutting:
        if not args.paper_aggressive:
            print("--compare-loss-cutting requires --paper-aggressive")
            sys.exit(1)
        run_loss_cutting_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_blended_conservative:
        if not args.paper_aggressive:
            print("--compare-blended-conservative requires --paper-aggressive")
            sys.exit(1)
        run_blended_conservative_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_patterns:
        if not args.paper_aggressive:
            print("--compare-patterns requires --paper-aggressive")
            sys.exit(1)
        run_pattern_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_scaling_strategy:
        if not args.paper_aggressive:
            print("--compare-scaling-strategy requires --paper-aggressive")
            sys.exit(1)
        run_scaling_strategy_compare(
            days=args.days,
            refresh=args.refresh,
            use_max=args.max,
            focus_ticker=args.focus_ticker,
        )
    elif args.compare_dynamic_vti:
        run_dynamic_vti_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_paper_sleeve_features:
        run_paper_sleeve_features_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_felix_social:
        run_felix_social_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_macro_regime or args.compare_regime:
        run_regime_shift_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_options:
        run_options_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_dynamic_risk:
        run_dynamic_risk_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_pairs:
        run_pairs_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_vol:
        run_vol_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_stat_arb:
        run_stat_arb_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_stat_arb_optimized:
        run_stat_arb_optimized_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_risk_parity:
        run_risk_parity_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_sector_rotation:
        if not args.paper_aggressive:
            print("--compare-sector-rotation requires --paper-aggressive")
            sys.exit(1)
        run_sector_rotation_compare(
            days=args.days,
            refresh=args.refresh,
            use_max=args.max,
            with_news=True,
        )
    elif args.compare_tech_guard:
        if not args.paper_aggressive:
            print("--compare-tech-guard requires --paper-aggressive")
            sys.exit(1)
        run_tech_guard_compare(
            days=args.days,
            refresh=args.refresh,
            use_max=args.max,
            with_news=bool(args.with_news),
        )
    elif args.compare_thinking_impact:
        if not args.paper_aggressive:
            print("--compare-thinking-impact requires --paper-aggressive")
            sys.exit(1)
        run_thinking_impact_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_thinking:
        if not args.paper_aggressive:
            print("--compare-thinking requires --paper-aggressive")
            sys.exit(1)
        run_thinking_compare(
            days=args.days,
            refresh=args.refresh,
            use_max=args.max,
            with_news=bool(args.with_news),
        )
    elif args.compare_crypto_v2:
        if not args.paper_aggressive:
            print("--compare-crypto-v2 requires --paper-aggressive")
            sys.exit(1)
        run_compare_crypto_v2(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_crypto_universe:
        if not args.paper_aggressive:
            print("--compare-crypto-universe requires --paper-aggressive")
            sys.exit(1)
        run_compare_crypto_universe(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_vti_levels and args.simulate_live_thinking:
        eq = args.start_equity or 300.0
        run_simulate_live_vti_levels_compare(
            days=args.days,
            refresh=args.refresh,
            use_max=args.max,
            start_equity=eq,
            thinking=False,
        )
    elif args.compare_vti_levels:
        if not args.paper_aggressive:
            print(
                "--compare-vti-levels requires --paper-aggressive "
                "(or use with --simulate-live-thinking for Live Profile A)"
            )
            sys.exit(1)
        run_compare_vti_levels(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.simulate_live_thinking:
        eq = args.start_equity
        if eq is None and args.with_news:
            eq = 300.0
        run_simulate_live_thinking_compare(
            days=args.days,
            refresh=args.refresh,
            use_max=args.max,
            vti_levels=args.vti_levels,
            with_news=bool(args.with_news),
            start_equity=eq,
        )
    elif args.compare_final:
        run_final_compare(
            days=args.days,
            refresh=args.refresh,
            use_max=args.max,
            report_path=args.final_report,
            all_windows=args.final_all_windows,
        )
    else:
        vti_core = max(0.0, min(1.0, args.vti_core))
        if args.small_account and vti_core <= 0:
            vti_core = config.SMALL_ACCOUNT_VTI_CORE_PCT
        run_performance_test(
            days=args.days,
            refresh=args.refresh,
            use_max=args.max,
            vti_core_pct=vti_core,
            paper_aggressive=args.paper_aggressive,
            small_account=args.small_account,
        )
        if args.paper_aggressive:
            print(f"Paper sleeve flags: {config.get_paper_feature_flags()}")
