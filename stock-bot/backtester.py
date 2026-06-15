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
        return True

    def crypto_sleeve_value(self):
        return self._sleeve_exposure(self._is_crypto_position)

    def nyse_sleeve_value(self):
        return self._sleeve_exposure(self._is_nyse_sleeve_position)

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
            def __init__(self, sym, qty, price):
                self.symbol = sym
                self.qty = qty
                self.current_price = price
                self.avg_entry_price = price

        for sym, qty in self.portfolio.positions.items():
            if config.normalize_symbol(sym) == target:
                price = self.prices.get(sym) or 0.0
                return _Pos(sym, qty, float(price) if price is not None else 0.0)
        return None

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
            self.positions[symbol] = self.positions.get(symbol, 0) + qty
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
            self.positions[symbol] = qty - sell_qty
            if self.positions[symbol] < 1e-9:
                del self.positions[symbol]
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
    from modules.dynamic_universe import maybe_refresh_screener_universe

    maybe_refresh_screener_universe(force=refresh)
    screener = config.load_screener_universe_tickers() or []
    extra = [t for t in screener if t not in config.UNIVERSE]
    if extra:
        fetch_days = _calendar_days_to_fetch(days or config.BACKTEST_DAYS)
        fetch_daily_history_for_tickers(
            extra,
            days=fetch_days if not use_max else None,
            use_max=use_max,
        )
    return screener


def _static_equity_universe(data_columns) -> list[str]:
    return [c for c in data_columns if config._nyse_eligible_symbol(c)]


def _dynamic_equity_universe(data_columns) -> list[str]:
    return config.nyse_momentum_universe(data_columns)


def _universe_sample_lines(data, screener: list[str]) -> list[str]:
    """Highlight NASDAQ / IPO names present in the dynamic pool."""
    from modules.dynamic_universe import load_screener_ticker_meta

    meta = load_screener_ticker_meta()
    watch = {"NVDA", "TSLA", "AMD", "AAPL", "SPCX", "META", "GOOGL", "AMZN", "MSFT"}
    lines: list[str] = []
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
    return lines


def _prefetch_screener_for_backtest(days, *, refresh=False, use_max=False) -> list[str]:
    """Ensure screener tickers exist in SQLite for dynamic-universe backtests."""
    from modules.dynamic_universe import maybe_refresh_screener_universe

    maybe_refresh_screener_universe(force=refresh)
    screener = config.load_screener_universe_tickers() or []
    extra = [t for t in screener if t not in config.UNIVERSE]
    if extra:
        fetch_days = _calendar_days_to_fetch(days or config.BACKTEST_DAYS)
        fetch_daily_history_for_tickers(
            extra,
            days=fetch_days if not use_max else None,
            use_max=use_max,
        )
    return screener


def _static_equity_universe(data_columns) -> list[str]:
    return [c for c in data_columns if config._nyse_eligible_symbol(c)]


def _dynamic_equity_universe(data_columns) -> list[str]:
    return config.nyse_momentum_universe(data_columns)


def _universe_sample_lines(data, screener: list[str]) -> list[str]:
    """Highlight NASDAQ / IPO names present in the dynamic pool."""
    from modules.dynamic_universe import load_screener_ticker_meta

    meta = load_screener_ticker_meta()
    watch = {"NVDA", "TSLA", "AMD", "AAPL", "SPCX", "META", "GOOGL", "AMZN", "MSFT"}
    lines: list[str] = []
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
    paper_risk_parity: bool | None = None,
    paper_vol_trading: bool | None = None,
    paper_vol_live_parity: bool = False,
    paper_dynamic_universe: bool | None = None,
    track_active_exposure: bool = False,
    simulate_live_thinking: bool = False,
    live_thinking_start_equity: float | None = None,
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
    saved_paper_risk_parity = config.PAPER_RISK_PARITY_ENABLED
    saved_paper_vol_trading = config.PAPER_VOL_TRADING_ENABLED
    saved_paper_dynamic_univ = config.PAPER_DYNAMIC_UNIVERSE_ENABLED
    saved_backtest_paper_sleeves = config.backtest_paper_sleeves_context()
    saved_live_thinking_ctx = config.live_thinking_sim_context()
    config.set_paper_aggressive_context(paper_aggressive)
    config.set_backtest_paper_sleeves_context(paper_aggressive)
    config.set_backtest_small_account_context(small_account)
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
    if paper_risk_parity is not None:
        config.PAPER_RISK_PARITY_ENABLED = bool(paper_risk_parity)
    if paper_vol_trading is not None:
        config.PAPER_VOL_TRADING_ENABLED = bool(paper_vol_trading)
    if paper_dynamic_universe is not None:
        config.PAPER_DYNAMIC_UNIVERSE_ENABLED = bool(paper_dynamic_universe)
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
    pair_cooldown = {}
    risk_manager = RiskManager(max_drawdown_pct=config.MAX_DRAWDOWN_PCT)
    equity_curve = []
    regime_counts = {}
    total_crypto = 0
    total_equity = 0
    total_spy = 0
    total_spy_entries = 0
    total_spy_exits = 0
    total_orders = 0
    pause_days = 0
    halt_liquidations = 0
    exposure_samples = []
    vti_core_samples = []
    active_exposure_samples = []
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
        if thinking_on:
            from modules.thinking_engine import (
                apply_thinking_tilt_to_caps,
                build_backtest_thinking_result,
                executor_scales_from_caps,
            )

            tilt_max_delta = (
                config.LIVE_THINKING_MAX_SLEEVE_DELTA if live_thinking else None
            )
            refresh = thinking_cache["regime"] != regime
            if refresh:
                vti_before = vti_core_pct
                thinking = build_backtest_thinking_result(window, regime, vol)
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
                )
                thinking_cache["regime"] = regime
                thinking_cache["vti_pct"] = merged["vti_core"]
                thinking_cache["scales"] = {
                    "spy_scale": executor_scales_from_caps(base_caps, merged).get(
                        "spy", 1.0
                    ),
                    "nyse_scale": executor_scales_from_caps(base_caps, merged).get(
                        "nyse", 1.0
                    ),
                    "crypto_scale": executor_scales_from_caps(base_caps, merged).get(
                        "crypto", 1.0
                    ),
                }
                if any(abs(v) > 0.001 for v in deltas.values()):
                    summary = thinking.get("market_summary") or {}
                    vix = summary.get("vix")
                    thinking_cache["events"].append(
                        {
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
                        }
                    )
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
            total_equity += run_equity_strategy(
                window,
                executor,
                regime,
                i,
                pair_cooldown,
                cooldown_bars=cooldown_bars,
                yield_gated=yield_gated,
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
        "paper_aggressive": paper_aggressive,
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
    config.PAPER_RISK_PARITY_ENABLED = saved_paper_risk_parity
    config.PAPER_VOL_TRADING_ENABLED = saved_paper_vol_trading
    config.PAPER_DYNAMIC_UNIVERSE_ENABLED = saved_paper_dynamic_univ
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


def run_thinking_compare(days=None, refresh=False, use_max=False) -> None:
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
    configs = [
        ("Paper (no thinking tilt)", {**base_kwargs, "paper_thinking": False}),
        ("Paper (+ thinking tilt)", {**base_kwargs, "paper_thinking": True}),
    ]
    print(
        "--- THINKING ENGINE A/B (force-decision heuristic tilt; gold-momentum gate; paper aggressive) ---"
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
    with_thinking = None
    for label_cfg, kwargs in configs:
        result = run_backtest(data, track_active_exposure=True, track_metrics=True, **kwargs)
        if kwargs.get("paper_thinking"):
            with_thinking = result
        else:
            baseline = result
        print(
            f"{label_cfg:<28} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}%"
        )
    print("-" * 58)
    if baseline and with_thinking:
        print(
            f"Thinking vs baseline: return "
            f"{with_thinking['total_return_pct'] - baseline['total_return_pct']:+.2f}pp | "
            f"Sharpe {with_thinking['sharpe'] - baseline['sharpe']:+.2f} | "
            f"MaxDD {with_thinking['max_drawdown_pct'] - baseline['max_drawdown_pct']:+.2f}pp"
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


def run_simulate_live_thinking_compare(
    days=None,
    refresh=False,
    use_max=False,
    *,
    start_equity: float | None = None,
    vti_levels: bool = False,
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

    eq_start = start_equity or config.SMALL_ACCOUNT_BACKTEST_EQUITY
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
    configs = [
        ("Small account (no thinking)", {**base_kwargs, "paper_thinking": False}),
        ("Small account (+ thinking tilts)", {**base_kwargs, "paper_thinking": True}),
    ]

    print(
        "--- LIVE SMALL-ACCOUNT + THINKING SIM "
        f"(90% VTI base, 1% risk, ${config.SMALL_ACCOUNT_MAX_NOTIONAL:.0f} max order, "
        f"±{config.LIVE_THINKING_MAX_SLEEVE_DELTA:.0%} tilt cap) ---"
    )
    print(
        f"Window ({label}): {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars) | start equity ${eq_start:,.0f}"
    )
    print(
        "Note: uses heuristic thinking proxy on regime change (same as paper backtest), "
        "not live Ollama per bar."
    )
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
        print(
            f"{label_cfg:<34} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}%"
        )
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
            f"max {post_dd:.2f}pp forward DD within 5d after a tilt"
        )

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
    """Compare static UNIVERSE vs exchange-agnostic dynamic screener (paper aggressive)."""
    saved_dyn = config.PAPER_DYNAMIC_UNIVERSE_ENABLED
    config.PAPER_DYNAMIC_UNIVERSE_ENABLED = True
    config.set_paper_aggressive_context(True)
    config.set_backtest_paper_sleeves_context(True)

    sim_days = days or config.BACKTEST_DAYS
    screener = _prefetch_screener_for_backtest(
        sim_days, refresh=refresh, use_max=use_max
    )

    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        data = _ensure_daily_data(sim_days, refresh=refresh, use_max=False)

    config.PAPER_DYNAMIC_UNIVERSE_ENABLED = saved_dyn

    if len(data) < MIN_HISTORY:
        print(f"Need at least {MIN_HISTORY} daily bars; got {len(data)}.")
        return

    static_size = len(_static_equity_universe(data.columns))
    dyn_size = len(_dynamic_equity_universe(data.columns))
    bench = _benchmark_return(data, MIN_HISTORY)
    base_kwargs = {
        "paper_aggressive": True,
        "paper_sleeve_features": True,
        "paper_dynamic_vti": True,
        "paper_dynamic_risk": True,
        "paper_stat_arb": True,
        "track_active_exposure": True,
    }
    configs = [
        (
            f"Static equity ({static_size} names)",
            {**base_kwargs, "paper_dynamic_universe": False},
        ),
        (
            f"Dynamic screener ({dyn_size} names)",
            {**base_kwargs, "paper_dynamic_universe": True},
        ),
    ]

    print("--- PAPER DYNAMIC UNIVERSE A/B (NYSE+NASDAQ, paper only) ---")
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        f"Screener file: {len(screener)} tickers | "
        f"static pool {static_size} | dynamic pool {dyn_size} | "
        f"filter: price>$5, avg daily $vol>$50M"
    )
    samples = _universe_sample_lines(data, screener)
    if samples:
        print("Sample dynamic names in backtest window:")
        for line in samples:
            print(line)
    print(
        f"{'Config':<32} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'NYSE':>6} {'Pairs':>6} {'Univ':>5}"
    )
    print("-" * 82)

    results: list[tuple[str, dict]] = []
    for label, kwargs in configs:
        result = run_backtest(data, **kwargs)
        results.append((label, result))
        print(
            f"{label:<32} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{result.get('nyse_signals', 0):>6} "
            f"{result.get('pairs_traded', 0):>6} "
            f"{result.get('equity_universe_size', 0):>5}"
        )

    print("-" * 82)
    if len(results) == 2:
        _, static_r = results[0]
        _, dyn_r = results[1]
        print(
            f"Delta (dynamic - static): "
            f"return {dyn_r['total_return_pct'] - static_r['total_return_pct']:+.2f}pp | "
            f"Sharpe {dyn_r['sharpe'] - static_r['sharpe']:+.2f} | "
            f"MaxDD {dyn_r['max_drawdown_pct'] - static_r['max_drawdown_pct']:+.2f}pp | "
            f"NYSE signals {dyn_r.get('nyse_signals', 0) - static_r.get('nyse_signals', 0):+d} | "
            f"pairs {dyn_r.get('pairs_traded', 0) - static_r.get('pairs_traded', 0):+d}"
        )
    print("-" * 82)


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
        help="Compare static UNIVERSE vs dynamic NYSE+NASDAQ screener (paper aggressive)",
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
        "--compare-thinking",
        action="store_true",
        help="Compare paper aggressive with vs without thinking-engine sleeve tilts",
    )
    parser.add_argument(
        "--simulate-live-thinking",
        action="store_true",
        help="Compare small-account live sim with vs without capped thinking tilts (±8%%)",
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
        "--compare-vti-levels",
        action="store_true",
        help=(
            "Best Paper Bot + thinking at fixed VTI levels "
            "(90/80/70/60%%; requires --paper-aggressive)"
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
    if args.equity_slippage_bps is not None:
        RUN_OPTIONS.equity_slippage_bps = max(0.0, float(args.equity_slippage_bps))
    if args.crypto_slippage_bps is not None:
        RUN_OPTIONS.crypto_slippage_bps = max(0.0, float(args.crypto_slippage_bps))
    RUN_OPTIONS.equity_commission_bps = max(0.0, float(args.equity_commission_bps))
    RUN_OPTIONS.crypto_commission_bps = max(0.0, float(args.crypto_commission_bps))
    apply_default_execution_costs()
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
    elif args.compare_thinking:
        run_thinking_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_crypto_v2:
        if not args.paper_aggressive:
            print("--compare-crypto-v2 requires --paper-aggressive")
            sys.exit(1)
        run_compare_crypto_v2(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_vti_levels:
        if not args.paper_aggressive:
            print("--compare-vti-levels requires --paper-aggressive")
            sys.exit(1)
        run_compare_vti_levels(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.simulate_live_thinking:
        run_simulate_live_thinking_compare(
            days=args.days,
            refresh=args.refresh,
            use_max=args.max,
            vti_levels=args.vti_levels,
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
