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
       python backtester.py --days 365 --deep-history --max-years 20
       python backtester.py --days 365 --deep-history --deep-history-indicators-only
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
from modules.data_loader import (
    clear_deep_history_cache,
    deep_history_symbol_set,
    fetch_deep_history,
    load_close_matrix,
    load_deep_history_matrix,
)
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
    summarize_entry_skip_reason,
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
    compute_regime_breakdown,
    effective_execution,
    format_regime_breakdown_table,
    regime_matches,
    resolve_regime_name,
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


def simulation_warmup_bars(n_bars: int) -> int:
    """Warmup bars for run_backtest (respects effective SPY MA for paper/live)."""
    ma = config.effective_spy_ma_window()
    return min(max(50, ma), max(0, n_bars - 5))


def _backtest_indicator_window(trade_data, bar_i: int, indicator_context=None):
    """Price history through bar_i; uses deep indicator context when provided."""
    if indicator_context is None or getattr(indicator_context, "empty", True):
        return trade_data.iloc[: bar_i + 1]
    ts = pd.Timestamp(trade_data.index[bar_i])
    ctx_index = indicator_context.index
    if getattr(ctx_index, "tz", None) is not None and ts.tzinfo is None:
        ts = ts.tz_localize(ctx_index.tz)
    elif getattr(ctx_index, "tz", None) is None and ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return indicator_context.loc[:ts]


def _print_indicator_context_summary(indicator_context, trade_data) -> None:
    ctx = indicator_context.dropna(how="all") if indicator_context is not None else None
    trade = trade_data.dropna(how="all")
    if ctx is None or ctx.empty:
        print("Indicator context: (empty) | Trading window: unavailable")
        return
    print(
        f"Indicator context: {ctx.index[0].date()} -> {ctx.index[-1].date()} "
        f"({len(ctx):,} bars) | "
        f"Trading window: {trade.index[0].date()} -> {trade.index[-1].date()} "
        f"({len(trade):,} bars)"
    )


def _build_indicator_context(
    trade_data,
    *,
    max_years: int = 20,
    refresh: bool = False,
):
    symbols = deep_history_symbol_set(trade_data.columns)
    print(
        f"Loading deep indicator history for {len(symbols)} symbols "
        f"(max {max_years}y, cached as *_deep.pkl)..."
    )
    return load_deep_history_matrix(symbols, max_years=max_years, refresh=refresh)
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
        self._current_regime = ""
        book = getattr(portfolio, "_stat_arb_open", None)
        if book is None:
            book = {}
            portfolio._stat_arb_open = book
        self._stat_arb_open = book

    def set_regime_sleeve_scales(self, *, spy_scale: float = 1.0, nyse_scale: float = 1.0) -> None:
        self._regime_spy_scale = float(spy_scale)
        self._regime_nyse_scale = float(nyse_scale)

    def set_current_regime(self, regime: str) -> None:
        self._current_regime = str(regime or "")

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
        hedge = getattr(self, "_short_long_hedge_mult", 1.0)
        if hedge < 0.999:
            mult *= hedge
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

    def stat_arb_sleeve_value(self):
        from modules.stat_arb_sleeve import stat_arb_sleeve_gross_value

        return stat_arb_sleeve_gross_value(self, self.prices)

    def nyse_momentum_sleeve_value(self):
        """NYSE long exposure excluding stat-arb pair legs when dedicated cap is on."""
        total = self.nyse_sleeve_value()
        if not config.effective_stat_arb_sleeve_cap_enabled():
            return total
        from modules.stat_arb_sleeve import stat_arb_pair_symbols

        pair_syms = stat_arb_pair_symbols(self)
        if not pair_syms:
            return total
        excluded = 0.0
        for symbol, qty in self.portfolio.positions.items():
            sym = config.normalize_symbol(symbol)
            if sym not in pair_syms or float(qty) <= 0:
                continue
            price = self.prices.get(symbol)
            excluded += self._position_market_value(qty, price)
        return max(0.0, total - excluded)

    def crypto_sleeve_value(self):
        return self._sleeve_exposure(self._is_crypto_position)

    def nyse_sleeve_value(self):
        return self._sleeve_exposure(self._is_nyse_sleeve_position)

    def spy_sleeve_value(self):
        return self._sleeve_exposure(self._is_spy_position)

    def _scaled_cap_pct(self, sleeve_cap_pct: float, *, sleeve: str | None = None) -> float:
        if config.live_conservative_profile_active() and sleeve in (
            "spy",
            "nyse",
            "crypto",
        ):
            return config.effective_sleeve_cap(sleeve_cap_pct, sleeve=sleeve)
        scale = self._cap_scale
        if sleeve == "spy":
            scale *= getattr(self, "_regime_spy_scale", 1.0)
            scale *= getattr(self, "_thinking_spy_scale", 1.0)
        elif sleeve == "nyse":
            scale *= getattr(self, "_regime_nyse_scale", 1.0)
            scale *= getattr(self, "_thinking_nyse_scale", 1.0)
        elif sleeve == "stat_arb":
            scale *= getattr(self, "_regime_nyse_scale", 1.0)
            scale *= self.pod_risk_scale("stat_arb")
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
            self._scaled_cap_pct(
                sleeve_cap_pct,
                sleeve=sleeve_key
                if sleeve_key in ("spy", "nyse", "crypto", "stat_arb")
                else None,
            ),
            sleeve_value,
            sleeve_key or "",
            self._cofire_notionals,
            regime=getattr(self, "_current_regime", None) or None,
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
        sleeve_val = (
            self.nyse_momentum_sleeve_value()
            if config.effective_stat_arb_sleeve_cap_enabled()
            else self.nyse_sleeve_value()
        )
        return self._apply_wisdom_multiplier(
            deployment_sizing.resolve_sleeve_notional(
                equity,
                cash,
                self._scaled_cap_pct(config.NYSE_SLEEVE_CAP_PCT, sleeve="nyse"),
                sleeve_val,
                "nyse",
                self._cofire_notionals,
                regime=getattr(self, "_current_regime", None) or None,
            )
        )

    def compute_stat_arb_notional(self):
        """Dedicated stat-arb sleeve cap (independent of NYSE momentum when enabled)."""
        equity = self.portfolio.equity(self.prices)
        min_n = config.effective_min_notional(equity)
        if config.effective_stat_arb_sleeve_cap_enabled():
            raw = self._compute_capped_notional(
                config.STAT_ARB_SLEEVE_CAP_PCT,
                self.stat_arb_sleeve_value(),
                "stat_arb",
            )
        else:
            raw = self.compute_nyse_notional()
        if raw is None:
            return None
        if float(raw) < min_n * 2:
            return None
        return raw

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
                regime=getattr(self, "_current_regime", None) or None,
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
        if pos is None:
            return None
        price = self.prices.get(pos.symbol)
        if price is None or not np.isfinite(price) or price <= 0:
            return None
        qty = float(pos.qty)
        if qty > 0:
            return self.execute_order(
                pos.symbol, "sell", notional=round(qty * price, 2), **kwargs
            )
        if qty < 0:
            return self.execute_order(
                pos.symbol,
                "buy",
                notional=round(abs(qty) * price, 2),
                pair_cover=True,
                **kwargs,
            )
        return None

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
        if side.lower() == "buy" and notional is not None:
            from modules.paper_risk_controls import cap_per_name_buy_notional

            equity = self.portfolio.equity(self.prices)
            notional = cap_per_name_buy_notional(
                symbol=symbol,
                side=side,
                notional=notional,
                equity=equity,
                prices=self.prices,
                positions=self.portfolio.positions,
            )
            if notional is None:
                return None
        order = self.portfolio.trade(
            symbol,
            side.lower(),
            price,
            tx_cost=_tx_cost_for_symbol(symbol),
            notional=notional,
            allow_naked_short=bool(
                kwargs.get("naked_short")
                or kwargs.get("strategy") == "opportunistic_short"
            ),
        )
        if order:
            for k in (
                "reason",
                "sleeve",
                "strategy",
                "pair_key",
                "naked_short",
                "naked_cover",
                "pair_cover",
            ):
                if k in kwargs:
                    order[k] = kwargs[k]
            self.orders.append(order)
            if config.paper_aggressive_context():
                from modules.paper_risk_controls import update_position_meta_on_fill

                update_position_meta_on_fill(
                    self.portfolio,
                    symbol,
                    side.lower(),
                    float(price),
                    bar_index=getattr(self, "_bar_index", None),
                    order=order,
                )
            att = getattr(self, "_attribution", None)
            if att:
                att.on_fill(order, self.prices, self.portfolio, **kwargs)
        return order


class BacktestPortfolio:
    def __init__(self, initial_capital=10000.0):
        self.cash = initial_capital
        self.initial_capital = initial_capital
        self.positions = {}
        self.position_meta = {}
        self.short_position_meta = {}
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

    def trade(self, symbol, side, price, tx_cost=TX_COST, notional=None, *, allow_naked_short=False):
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
                    "pair_cover": not allow_naked_short,
                    "naked_cover": allow_naked_short,
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
                        or allow_naked_short
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
                        "pair_short": not allow_naked_short,
                        "naked_short": allow_naked_short,
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


def _benchmark_return(data, start_idx=None):
    if BENCHMARK not in data.columns:
        return None
    if start_idx is None or start_idx >= len(data) or start_idx < 0:
        n = len(data)
        start_idx = min(MIN_HISTORY, max(0, n - 5))
    col = data[BENCHMARK].iloc[start_idx:].dropna()
    if len(col) < 2 or col.iloc[0] <= 0:
        return None
    return (col.iloc[-1] / col.iloc[0] - 1) * 100


def _trim_baseline_backtest_data(data, *, target_sim_bars: int):
    """Warmup prefix + simulation tail (baseline backtest shape)."""
    n_bars = len(data)
    warmup = min(MIN_HISTORY, max(0, n_bars - 5))
    desired_total = warmup + target_sim_bars
    if len(data) > desired_total:
        return data.iloc[-desired_total:].copy()
    return data.copy()


def _print_deep_history_mode_banner(
    *,
    deep_history: bool,
    deep_history_indicators_only: bool,
    max_years: int,
) -> None:
    if not deep_history:
        return
    if deep_history_indicators_only:
        print(
            f"Deep History: Indicators only ({max_years}y context) | "
            "Allocator on sim window"
        )
    else:
        print(
            f"Deep History: Full ({max_years}y context) | "
            "Allocator on trade window"
        )


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
    """Ensure screener + sector expansion tickers exist in SQLite for backtests."""
    from modules.dynamic_universe import maybe_refresh_screener_universe

    maybe_refresh_screener_universe(force=refresh)
    screener = config.load_screener_universe_tickers() or []
    prefetch: set[str] = set(screener)
    if config.effective_dynamic_sector_screener() or config.DYNAMIC_SECTOR_SCREENER_ENABLED:
        try:
            from modules.sector_screener import sector_expansion_prefetch_tickers

            prefetch.update(sector_expansion_prefetch_tickers())
        except ImportError:
            pass
    extra = [t for t in sorted(prefetch) if t not in config.UNIVERSE]
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
    if config.effective_stat_arb_sleeve_cap_enabled():
        long_sum += config.STAT_ARB_SLEEVE_CAP_PCT
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
    if config.effective_dynamic_core_enabled() or config.effective_core_allocator_locked():
        from modules.core_allocator import effective_vti_core_pct

        pct = effective_vti_core_pct(
            equity,
            vol_score=vol_score,
            macro_stress=macro_stress_flag,
            volatility=volatility,
        )
        if pct is not None:
            return pct
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
    paper_crypto_enabled: bool | None = None,
    paper_crypto_v2: bool | None = None,
    paper_crypto_universe_expanded: bool | None = None,
    paper_risk_parity: bool | None = None,
    paper_vol_trading: bool | None = None,
    paper_vol_live_parity: bool = False,
    paper_dynamic_universe: bool | None = None,
    paper_dynamic_universe_strict: bool | None = None,
    nyse_conditional_on_spy: bool | None = None,
    track_active_exposure: bool = False,
    simulate_live_thinking: bool = False,
    live_thinking_start_equity: float | None = None,
    with_news: bool = False,
    stat_arb_report: bool | None = None,
    regime_filter: str | None = None,
    indicator_context=None,
    max_years: int = 20,
    allocator_data=None,
    deep_history_indicators_only: bool = False,
):
    """Run fund pipeline on daily data; return performance + optional SPY fill metrics."""
    if stat_arb_report is None:
        stat_arb_report = bool(paper_aggressive)
    saved_deep_history = config.DEEP_HISTORY_ENABLED
    saved_deep_indicators_only = config.DEEP_HISTORY_INDICATORS_ONLY
    config.DEEP_HISTORY_ENABLED = indicator_context is not None
    config.DEEP_HISTORY_INDICATORS_ONLY = bool(deep_history_indicators_only)
    apply_run_options_to_config()
    apply_default_execution_costs()
    if RUN_OPTIONS.fast_mode:
        data = apply_fast_mode_data(data)
    indicator_frame = indicator_context
    if indicator_frame is not None and not getattr(indicator_frame, "empty", True):
        if RUN_OPTIONS.fast_mode:
            indicator_frame = apply_fast_mode_data(indicator_frame)
        prepare_indicator_cache(
            indicator_frame, spy_ma_window=config.effective_spy_ma_window()
        )
    else:
        indicator_frame = None
        prepare_indicator_cache(data, spy_ma_window=config.effective_spy_ma_window())
    saved_paper_ctx = config.paper_aggressive_context()
    saved_small_ctx = config.backtest_small_account_context()
    saved_live_conservative_ctx = config.backtest_live_conservative_context()
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
    saved_paper_crypto = config.PAPER_CRYPTO_ENABLED
    saved_paper_crypto_v2 = config.PAPER_CRYPTO_V2_ENABLED
    saved_paper_crypto_expanded = config.PAPER_CRYPTO_UNIVERSE_EXPANDED
    saved_paper_risk_parity = config.PAPER_RISK_PARITY_ENABLED
    saved_paper_vol_trading = config.PAPER_VOL_TRADING_ENABLED
    saved_paper_dynamic_univ = config.PAPER_DYNAMIC_UNIVERSE_ENABLED
    saved_paper_dynamic_univ_strict = config.PAPER_DYNAMIC_UNIVERSE_STRICT
    saved_backtest_paper_sleeves = config.backtest_paper_sleeves_context()
    saved_backtest_vti_ceiling = config.backtest_vti_ceiling()
    saved_live_thinking_ctx = config.live_thinking_sim_context()
    config.set_paper_aggressive_context(paper_aggressive)
    config.set_backtest_paper_sleeves_context(paper_aggressive)
    config.set_backtest_small_account_context(small_account)
    if small_account and not paper_aggressive:
        conservative = vti_core_pct <= 0 or abs(
            vti_core_pct - config.LIVE_VTI_CORE_PCT
        ) < 0.005
        if vti_core_pct > 0 and abs(vti_core_pct - 0.90) < 0.005:
            conservative = False
        config.set_backtest_live_conservative_context(conservative)
        if conservative:
            config.enforce_live_small_account_profile()
    else:
        config.set_backtest_live_conservative_context(False)
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
    if paper_crypto_enabled is not None:
        config.PAPER_CRYPTO_ENABLED = bool(paper_crypto_enabled)
        if not paper_crypto_enabled:
            config.PAPER_CRYPTO_V2_ENABLED = False
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
    if nyse_conditional_on_spy is not None:
        config.PAPER_NYSE_CONDITIONAL_ON_SPY = bool(nyse_conditional_on_spy)
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
    if paper_aggressive and config.effective_core_allocator_locked() and vti_core_pct <= 0:
        from modules.core_allocator import effective_vti_core_pct, lock_core_allocator

        lock_core_allocator()
        fixed_vti_core_pct = float(effective_vti_core_pct() or config.PAPER_VTI_CORE_PCT)
    elif paper_aggressive and not config.PAPER_DYNAMIC_VTI_ENABLED:
        fixed_vti_core_pct = (
            vti_core_pct if vti_core_pct > 0 else config.PAPER_VTI_CORE_PCT
        )
    config.set_backtest_vti_ceiling(
        fixed_vti_core_pct
        if paper_aggressive
        and not config.PAPER_DYNAMIC_VTI_ENABLED
        and not config.effective_dynamic_core_enabled()
        and not config.effective_core_allocator_locked()
        else None
    )

    if paper_aggressive and config.effective_dynamic_core_enabled():
        from modules.core_allocator import reset_core_allocator_state

        reset_core_allocator_state()

    n_bars = len(data)
    if indicator_frame is not None:
        warmup = 0
    else:
        warmup = simulation_warmup_bars(n_bars)
    start_date = data.index[warmup]
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
    portfolio._stat_arb_open = {}
    pair_cooldown = {}
    from modules.market_context import reset_regime_hysteresis

    reset_regime_hysteresis()
    if paper_aggressive and config.effective_dynamic_core_enabled():
        from modules.core_allocator import maybe_refresh_core_allocation

        maybe_refresh_core_allocation(
            data,
            bar_index=warmup,
            force=True,
            allocator_data=allocator_data,
        )
    risk_manager = RiskManager(
        max_drawdown_pct=config.MAX_DRAWDOWN_PCT,
        halt_min_bars=config.HALT_MIN_BARS,
    )
    equity_curve = []
    regime_counts = {}
    regime_series: list[str] = []
    regime_filter_skips = 0
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
    spy_nyse_cofire_days = 0
    nyse_pick_counts: dict[str, int] = {}
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
    skip_acc: dict = {}
    from modules.backtest_attribution import BacktestAttribution

    attribution_tracker = BacktestAttribution() if stat_arb_report else None
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
            if indicator_frame is not None and not indicator_frame.empty:
                macro_daily = indicator_frame.copy()
                for col in ("TLT", "TNX"):
                    if col not in macro_daily.columns or macro_daily[col].dropna().empty:
                        extra = fetch_deep_history(
                            col, max_years=max_years, refresh=False
                        )
                        if not extra.empty:
                            macro_daily[col] = extra.reindex(macro_daily.index).ffill()
            else:
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
        "last_news_date": None,
        "scales": {"spy_scale": 1.0, "nyse_scale": 1.0, "crypto_scale": 1.0},
        "vti_pct": None,
        "events": [],
    }
    from modules.crypto_dual_sleeve import CryptoV2State

    crypto_v2_book = CryptoV2State()
    last_executor = None
    strategic_rebalancer = None
    prev_bar_date = None
    if config.REBALANCE_ENABLED:
        from modules.rebalancer import StrategicRebalancer

        strategic_rebalancer = StrategicRebalancer()

    for i in range(warmup, len(data)):
        window = _backtest_indicator_window(data, i, indicator_frame)
        prices = data.iloc[i]
        eq = portfolio.equity(prices)
        if small_account:
            config.configure_account_profile(eq)
        equity_curve.append(eq)

        from modules.market_context import set_regime_bar_index

        set_regime_bar_index(i)
        risk_manager.set_current_bar(i)

        if wisdom_mode:
            from modules.wisdom_sentiment import resolve_backtest_regime

            regime, vol, wisdom_paused, sizing_mult, classified_regime = resolve_backtest_regime(
                window,
                data.index[i],
                monthly_web,
                wisdom_mode=wisdom_mode,
            )
            sentiment = get_price_sentiment(window)
            if wisdom_paused:
                pause_days += 1
            regime_label = classified_regime
            regime_counts[classified_regime] = regime_counts.get(classified_regime, 0) + 1
        else:
            wisdom_paused = False
            sentiment = get_price_sentiment(window)
            vol = get_volatility(window)
            regime = get_market_regime(sentiment, vol, apply_hysteresis=True)
            from modules.regime_sizing import effective_regime_sizing_multiplier

            sizing_mult = effective_regime_sizing_multiplier(regime)
            if regime_entries_paused(regime, window, sentiment):
                pause_days += 1
            regime_label = regime
            regime_counts[regime] = regime_counts.get(regime, 0) + 1
        regime_series.append(regime_label)

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
            if config.effective_stat_arb_enabled() and last_executor is not None:
                from modules.stat_arb_sleeve import process_exits

                last_executor.prices = prices
                process_exits(window, last_executor, regime="", now=i)
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

        if regime_filter and not regime_matches(regime_label, regime_filter):
            regime_filter_skips += 1
            continue

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
            dd_now = risk_manager.current_drawdown(eq)
            if config.PAPER_DYNAMIC_RISK_ENABLED:
                hist = equity_curve[-max(60, int(config.PORTFOLIO_VOL_WINDOW) + 5) :]
                config.set_dynamic_risk_context(
                    vol_score=vol_score,
                    regime=regime,
                    macro_stress=macro_stress_flag,
                    drawdown=dd_now,
                    recovery_mode=risk_manager.recovery_mode,
                    equity_history=hist,
                )
                day_risk = config.effective_risk_per_trade(
                    eq,
                    vol_score=vol_score,
                    regime=regime,
                    macro_stress=macro_stress_flag,
                    drawdown=dd_now,
                    recovery_mode=risk_manager.recovery_mode,
                )
            else:
                hist = equity_curve[-max(60, int(config.PORTFOLIO_VOL_WINDOW) + 5) :]
                config.set_dynamic_risk_context(
                    drawdown=dd_now,
                    recovery_mode=risk_manager.recovery_mode,
                    equity_history=hist if config.effective_tail_risk_controls() else None,
                )
                day_risk = config.effective_risk_per_trade(
                    eq,
                    regime=regime,
                    drawdown=dd_now,
                    recovery_mode=risk_manager.recovery_mode,
                )
            risk_samples.append(day_risk)
            if day_risk >= config.PAPER_RISK_CALM_BULL_PCT - 1e-9:
                high_risk_days += 1
        if paper_aggressive and config.effective_dynamic_core_enabled():
            from modules.core_allocator import maybe_refresh_core_allocation

            maybe_refresh_core_allocation(
                data,
                bar_index=i,
                allocator_data=allocator_data,
            )
        if config.REBALANCE_ENABLED:
            from modules.operating_layer import run_operating_cycle_backtest

            op_result = run_operating_cycle_backtest(
                portfolio,
                prices,
                regime=regime,
                vol=vol,
                macro_stress=macro_stress_flag,
                vol_score=vol_score,
                bar_date=data.index[i],
                equity_curve=equity_curve,
                rebalancer=strategic_rebalancer,
                prev_bar_date=prev_bar_date,
            )
            prev_bar_date = data.index[i]
            vti_core_pct = config.clamp_paper_vti_core(
                float((op_result or {}).get("core_target", config.REBALANCE_CORE_TARGET))
            )
            if op_result and not op_result.get("skipped") and verbose:
                wisdom_rec = op_result.get("wisdom") or {}
                print(
                    f"--- Operating Layer {data.index[i].date()}: core "
                    f"{vti_core_pct:.0%} | wisdom {wisdom_rec.get('action', 'hold')} "
                    f"conv {wisdom_rec.get('conviction', 0):.2f} ---"
                )
        else:
            vti_core_pct = _resolve_backtest_vti_pct(
                eq,
                vol_score=vol_score,
                volatility=vol,
                macro_stress_flag=macro_stress_flag,
                paper_aggressive=paper_aggressive,
                fixed_vti_core_pct=fixed_vti_core_pct,
            )
            vti_core_pct = config.clamp_paper_vti_core(vti_core_pct)
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
            bar_date = data.index[i].date()
            news_digest: dict | None = None
            if with_news:
                if thinking_cache.get("last_news_date") != bar_date:
                    thinking_cache["last_news_date"] = bar_date
                    refresh = True
            if refresh:
                vti_before = vti_core_pct
                if with_news:
                    from modules.thinking_news import synthesize_backtest_news

                    news_digest = synthesize_backtest_news(
                        window,
                        regime,
                        vol,
                        slot="premarket",
                    )
                    thinking = build_backtest_thinking_result(
                        window,
                        regime,
                        vol,
                        news_headlines=news_digest.get("headlines"),
                        news_slot="premarket",
                    )
                else:
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
                thinking_cache["vti_pct"] = config.clamp_paper_vti_core(
                    float(merged["vti_core"])
                )
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
                    }
                    if news_digest:
                        event["news_slot"] = news_digest.get("slot")
                        event["news_impact_score"] = news_digest.get("news_impact_score")
                        event["news_theme_summary"] = news_digest.get("theme_summary")
                    elif with_news:
                        event["news_impact_score"] = thinking.get("news_impact_score")
                        event["news_theme_summary"] = summary.get("news_theme_summary")
                    thinking_cache["events"].append(event)
            if thinking_cache["vti_pct"] is not None:
                vti_core_pct = config.clamp_paper_vti_core(
                    float(thinking_cache["vti_pct"])
                )
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

        if not config.REBALANCE_ENABLED and vti_core_pct > 0:
            _rebalance_vti_core(portfolio, prices, vti_core_pct)
        active_fraction = max(0.0, 1.0 - vti_core_pct)
        executor = BacktestExecutor(
            portfolio,
            prices,
            active_fraction=active_fraction,
            cap_scale=cap_scale,
        )
        executor._last_regime = regime
        if config.effective_crypto_v2_enabled():
            executor._crypto_v2_book = crypto_v2_book
        if attribution_tracker is not None:
            executor._attribution = attribution_tracker
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
            new_vti = config.clamp_paper_vti_core(new_vti)
            if abs(new_vti - vti_core_pct) > 0.004:
                vti_core_pct = new_vti
                if not config.REBALANCE_ENABLED and vti_core_pct > 0:
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
        executor.set_current_regime(regime)
        executor.set_wisdom_sizing_multiplier(sizing_mult)
        executor._bar_index = i
        if paper_aggressive:
            from modules.paper_risk_controls import (
                run_paper_position_exits,
                trim_per_name_overexposure,
                trim_sleeve_overexposure,
            )

            run_paper_position_exits(portfolio, prices, i, executor)
            trim_sleeve_overexposure(executor, prices)
            trim_per_name_overexposure(executor, prices)
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
        crypto_n = 0
        if config.effective_crypto_enabled():
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
        equity_n = 0
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
            equity_n = eq_pairs
            pairs_traded += eq_pairs
        else:
            nyse_picks: list[str] = []
            from modules.pipeline_strategies import run_nyse_momentum_and_stat_arb

            equity_n = run_nyse_momentum_and_stat_arb(
                window,
                executor,
                regime,
                i,
                pair_cooldown,
                cooldown_bars=cooldown_bars,
                yield_gated=yield_gated,
                pick_log=nyse_picks,
                volatility=vol,
            )
            total_equity += equity_n
            for sym in nyse_picks:
                nyse_pick_counts[sym] = nyse_pick_counts.get(sym, 0) + 1
        if config.effective_opportunistic_short_enabled():
            from modules.opportunistic_short_sleeve import run_opportunistic_short_strategy

            run_opportunistic_short_strategy(
                window,
                executor,
                regime,
                i,
                pair_cooldown,
                cooldown_bars=cooldown_bars,
                volatility=vol,
            )
        if spy_entry_n > 0 and equity_n > 0:
            spy_nyse_cofire_days += 1
        day_new_entries = int(crypto_n) + int(spy_entry_n) + int(equity_n)
        from modules.entry_skip_tracker import accumulate_backtest

        if day_new_entries > 0:
            accumulate_backtest("traded", skip_acc)
        else:
            accumulate_backtest(
                summarize_entry_skip_reason(
                    window,
                    executor,
                    regime,
                    i,
                    pair_cooldown,
                    cooldown_bars=cooldown_bars,
                    yield_gated=yield_gated,
                    market_open=True,
                    volatility=vol,
                    wisdom_paused=bool(wisdom_paused),
                ),
                skip_acc,
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
        if attribution_tracker is not None:
            attribution_tracker.snapshot_mtm(executor, prices)

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
                spy_fill["first_signal_cycle"] = i - warmup
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
                spy_fill["cycles_to_90pct"] = i - warmup - base
                spy_fill["trades_to_90pct"] = spy_fill["spy_buys"]
                spy_fill["hours_to_90pct"] = round(
                    spy_fill["cycles_to_90pct"] * (COOLDOWN_SECONDS / 3600), 1
                )

    bench = _benchmark_return(data, warmup)
    perf = compute_performance_metrics(
        equity_curve,
        initial_capital=portfolio.initial_capital,
        benchmark_return_pct=bench,
        total_orders=total_orders,
        equity_index=[data.index[i].isoformat() for i in range(warmup, len(data))],
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
        "regime_series": regime_series,
        "regime_filter": regime_filter,
        "regime_filter_skips": regime_filter_skips,
        "regime_breakdown": compute_regime_breakdown(
            equity_curve, regime_series, initial_capital=initial_capital
        ),
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
        "spy_nyse_cofire_pct": round(100 * spy_nyse_cofire_days / trade_days, 1)
        if trade_days
        else 0.0,
        "spy_nyse_cofire_days": spy_nyse_cofire_days,
        "nyse_pick_counts": dict(sorted(nyse_pick_counts.items(), key=lambda x: -x[1])),
        "nyse_conditional_on_spy": config.effective_nyse_conditional_on_spy(),
        "small_account": small_account,
        "cap_scale": cap_scale,
        "equity_index": [data.index[i].isoformat() for i in range(warmup, len(data))],
        "equity_values": [round(v, 2) for v in equity_curve],
        "pairs_traded": pairs_traded,
        "pair_pnl_correlation": pair_pnl_corr,
    }
    from modules.entry_skip_tracker import finalize_backtest_accumulator

    result["entry_skip_breakdown"] = finalize_backtest_accumulator(skip_acc)
    if last_executor is not None and config.effective_stat_arb_enabled():
        from modules.stat_arb_sleeve import force_close_all_pairs

        last_executor.prices = data.iloc[-1]
        force_close_all_pairs(
            last_executor, data.iloc[:], regime="", now=len(data) - 1
        )
    if attribution_tracker is not None and last_executor is not None:
        att = attribution_tracker.finalize(last_executor.prices)
        att["stat_arb_enabled"] = config.effective_stat_arb_enabled()
        att["crypto_enabled"] = config.crypto_sleeve_enabled()
        result["attribution"] = att
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
    config.set_backtest_vti_ceiling(saved_backtest_vti_ceiling)
    config.set_live_thinking_sim_context(saved_live_thinking_ctx)
    config.set_backtest_small_account_context(saved_small_ctx)
    config.set_backtest_live_conservative_context(saved_live_conservative_ctx)
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
    config.PAPER_CRYPTO_ENABLED = saved_paper_crypto
    config.PAPER_CRYPTO_V2_ENABLED = saved_paper_crypto_v2
    config.PAPER_CRYPTO_UNIVERSE_EXPANDED = saved_paper_crypto_expanded
    config.PAPER_RISK_PARITY_ENABLED = saved_paper_risk_parity
    config.PAPER_VOL_TRADING_ENABLED = saved_paper_vol_trading
    config.PAPER_DYNAMIC_UNIVERSE_ENABLED = saved_paper_dynamic_univ
    config.PAPER_DYNAMIC_UNIVERSE_STRICT = saved_paper_dynamic_univ_strict
    if paper_aggressive:
        from modules.core_allocator import core_allocator_snapshot

        result["core_allocator"] = core_allocator_snapshot()
    config.DEEP_HISTORY_ENABLED = saved_deep_history
    config.DEEP_HISTORY_INDICATORS_ONLY = saved_deep_indicators_only
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
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
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
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
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
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
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


def run_opportunistic_short_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare Realistic Research v1.3 with vs without protective shorts."""
    sim_days = days or config.BACKTEST_DAYS
    _prefetch_screener_for_backtest(sim_days, refresh=refresh, use_max=use_max)
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        data = _ensure_daily_data(sim_days, refresh=refresh, use_max=False)
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    base_kwargs = {
        "paper_aggressive": True,
        "paper_sleeve_features": True,
        "stat_arb_report": True,
    }
    lo = config.effective_protective_short_min_pct()
    hi = config.effective_protective_short_max_pct()
    configs = [
        ("RR v1.3 (shorts OFF)", {**base_kwargs, "opportunistic_short": False}),
        (f"RR v1.3 (shorts {lo:.0%}–{hi:.0%})", {**base_kwargs, "opportunistic_short": True}),
    ]
    print(f"--- PROTECTIVE SHORTS A/B (Realistic Research v1.3, {lo:.0%}–{hi:.0%}) ---")
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        f"{'Config':<32} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Short PnL':>10} {'Trips':>6} {'Fires':>6} {'Win%':>5}"
    )
    print("-" * 96)

    saved_short = config.PROTECTIVE_SHORT_ENABLED
    saved_opp = config.SHORT_OPPORTUNISTIC_ENABLED
    original_enforce = config.enforce_realistic_research_profile
    results: list[tuple[str, dict]] = []

    def _skip_enforce() -> None:
        return None

    try:
        config.enforce_realistic_research_profile = _skip_enforce
        for label, kwargs in configs:
            on = kwargs.pop("opportunistic_short", True)
            config.PROTECTIVE_SHORT_ENABLED = on
            config.SHORT_OPPORTUNISTIC_ENABLED = on
            result = run_backtest(
                data,
                track_active_exposure=True,
                track_metrics=True,
                **kwargs,
            )
            att = result.get("attribution") or {}
            sleeve = (att.get("sleeves") or {}).get("opportunistic_short") or {}
            os_att = att.get("opportunistic_short") or {}
            metrics = {
                "return_pct": float(result.get("total_return_pct", 0) or 0),
                "sharpe": float(result.get("sharpe", 0) or 0),
                "max_dd": float(result.get("max_drawdown_pct", 0) or 0),
                "short_pnl": float(sleeve.get("total_pnl_usd", 0) or 0),
                "short_trips": int(sleeve.get("round_trips", 0) or 0),
                "short_wr": float(os_att.get("win_rate_pct", 0) or 0),
                "trigger_fires": int(os_att.get("trigger_fires", 0) or 0),
                "trigger_scans": int(os_att.get("trigger_scans", 0) or 0),
                "entry_fills": int(os_att.get("entry_fills", 0) or 0),
                "os_att": os_att,
            }
            results.append((label, metrics))
            print(
                f"{label:<32} "
                f"{metrics['return_pct']:>+7.2f}% "
                f"{metrics['sharpe']:>7.2f} "
                f"{metrics['max_dd']:>7.2f}% "
                f"{metrics['short_pnl']:>+10.2f} "
                f"{metrics['short_trips']:>6} "
                f"{metrics['trigger_fires']:>6} "
                f"{metrics['short_wr']:>4.0f}%"
            )
            if att:
                from modules.backtest_attribution import (
                    format_opportunistic_short_banner,
                    format_short_trigger_summary,
                )

                line = format_opportunistic_short_banner(att)
                if line:
                    print(f"  {line}")
                os_att = att.get("opportunistic_short") or {}
                trig_line = format_short_trigger_summary(os_att)
                if trig_line and on:
                    print(f"  {trig_line}")
    finally:
        config.enforce_realistic_research_profile = original_enforce
        config.PROTECTIVE_SHORT_ENABLED = saved_short
        config.SHORT_OPPORTUNISTIC_ENABLED = saved_opp
        original_enforce()

    print("-" * 96)
    if len(results) == 2:
        _, m0 = results[0]
        _, m1 = results[1]
        print(
            f"Delta (shorts ON - OFF): "
            f"return {m1['return_pct'] - m0['return_pct']:+.2f}pp | "
            f"Sharpe {m1['sharpe'] - m0['sharpe']:+.2f} | "
            f"MaxDD {m1['max_dd'] - m0['max_dd']:+.2f}pp | "
            f"short PnL ${m1['short_pnl']:+.2f} | "
            f"trips {m1['short_trips']:+d} | "
            f"trigger fires {m1['trigger_fires']} / scans {m1['trigger_scans']}"
        )
        from modules.backtest_attribution import format_short_trigger_summary

        trig_summary = format_short_trigger_summary(m1.get("os_att") or {})
        if trig_summary:
            print(trig_summary)


_LEGACY_UNIVERSE_CONFIG = {
    "BASE_UNIVERSE_SIZE": 75,
    "SECTOR_EXPANSION_SIZE": 35,
    "SECTOR_MAX_TOTAL_TICKERS": 160,
    "MAX_ACTIVE_SECTORS": 3,
    "MAX_ACTIVE_SECTORS_STRONG": 3,
    "SECTOR_FALLBACK_MOMENTUM_COUNT": 12,
}


def _universe_config_snapshot() -> dict[str, int]:
    return {
        "BASE_UNIVERSE_SIZE": int(config.BASE_UNIVERSE_SIZE),
        "SECTOR_EXPANSION_SIZE": int(config.SECTOR_EXPANSION_SIZE),
        "SECTOR_MAX_TOTAL_TICKERS": int(config.SECTOR_MAX_TOTAL_TICKERS),
        "MAX_ACTIVE_SECTORS": int(config.MAX_ACTIVE_SECTORS),
        "MAX_ACTIVE_SECTORS_STRONG": int(getattr(config, "MAX_ACTIVE_SECTORS_STRONG", 4)),
        "SECTOR_FALLBACK_MOMENTUM_COUNT": int(config.SECTOR_FALLBACK_MOMENTUM_COUNT),
    }


def _apply_universe_config(values: dict[str, int]) -> None:
    for key, val in values.items():
        setattr(config, key, val)


def _universe_compare_metrics(result: dict, *, data=None) -> dict[str, float | int]:
    att = result.get("attribution") or {}
    pick_counts = result.get("nyse_pick_counts") or {}
    traded = set(pick_counts.keys()) if pick_counts else set()
    if not traded:
        for trip in att.get("round_trips") or []:
            sym = str(trip.get("symbol") or "")
            if sym and not config.is_crypto(sym):
                traded.add(sym)
    stat = att.get("stat_arb") or {}
    skip = result.get("entry_skip_breakdown") or {}
    by_cat = skip.get("by_category") or {}
    stat_rejects = att.get("stat_arb_rejects") or {}
    pool_size = 0
    if data is not None and len(data) > 0:
        try:
            from modules.sector_screener import get_expanded_universe

            pool_size = len(get_expanded_universe(data.columns, data))
        except Exception:
            pool_size = int(result.get("equity_universe_size", 0) or 0)
    return {
        "pool_size": pool_size,
        "unique_tickers": len(traded),
        "nyse_signals": int(result.get("nyse_signals", 0) or 0),
        "stat_arb_pairs": int(stat.get("pair_entries", 0) or 0),
        "stat_arb_intents": int(stat.get("intents", 0) or 0),
        "stat_arb_no_room": int(stat_rejects.get("no_room", 0) or 0),
        "skip_no_room": int(by_cat.get("no_room", 0) or 0),
        "sharpe": float(result.get("sharpe", 0) or 0),
        "return_pct": float(result.get("total_return_pct", 0) or 0),
    }


def run_universe_size_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare legacy (75/35/160) vs expanded (110/45/180) universe sizing."""
    sim_days = days or config.BACKTEST_DAYS
    _prefetch_screener_for_backtest(sim_days, refresh=refresh, use_max=use_max)
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        data = _ensure_daily_data(sim_days, refresh=refresh, use_max=False)
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    base_kwargs = {
        "paper_aggressive": True,
        "paper_sleeve_features": True,
        "stat_arb_report": True,
    }
    configs = [
        ("Legacy (75/35/160)", _LEGACY_UNIVERSE_CONFIG),
        ("Expanded (110/45/180)", _universe_config_snapshot()),
    ]
    print("--- UNIVERSE SIZE A/B (Realistic Research v1.1c) ---")
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        f"{'Config':<24} {'Return':>8} {'Sharpe':>7} {'Pool':>6} {'Unique':>7} "
        f"{'NYSE':>6} {'SA pairs':>9} {'SA no_rm':>9} {'Skip nr':>8}"
    )
    print("-" * 88)

    saved = _universe_config_snapshot()
    results: list[tuple[str, dict, dict]] = []
    try:
        for label, universe_cfg in configs:
            _apply_universe_config(universe_cfg)
            import modules.sector_screener as sector_mod

            sector_mod._sector_pools_cache = None
            result = run_backtest(data, track_metrics=True, **base_kwargs)
            metrics = _universe_compare_metrics(result, data=data)
            results.append((label, result, metrics))
            print(
                f"{label:<24} "
                f"{metrics['return_pct']:>+7.2f}% "
                f"{metrics['sharpe']:>7.2f} "
                f"{metrics['pool_size']:>6} "
                f"{metrics['unique_tickers']:>7} "
                f"{metrics['nyse_signals']:>6} "
                f"{metrics['stat_arb_pairs']:>9} "
                f"{metrics['stat_arb_no_room']:>9} "
                f"{metrics['skip_no_room']:>8}"
            )
    finally:
        _apply_universe_config(saved)
        import modules.sector_screener as sector_mod

        sector_mod._sector_pools_cache = None

    print("-" * 88)
    if len(results) == 2:
        _legacy, _new, m0 = results[0]
        _legacy2, _new2, m1 = results[1]
        print(
            f"Delta (expanded - legacy): "
            f"return {m1['return_pct'] - m0['return_pct']:+.2f}pp | "
            f"Sharpe {m1['sharpe'] - m0['sharpe']:+.2f} | "
            f"pool {m1['pool_size'] - m0['pool_size']:+d} | "
            f"unique tickers {m1['unique_tickers'] - m0['unique_tickers']:+d} | "
            f"NYSE signals {m1['nyse_signals'] - m0['nyse_signals']:+d} | "
            f"stat-arb pairs {m1['stat_arb_pairs'] - m0['stat_arb_pairs']:+d} | "
            f"SA no_room {m1['stat_arb_no_room'] - m0['stat_arb_no_room']:+d} | "
            f"skip no_room {m1['skip_no_room'] - m0['skip_no_room']:+d}"
        )


_LEGACY_V12_REALISTIC_RESEARCH = {
    "PAPER_STAT_ARB_MIN_CORR": 0.75,
    "PAPER_STAT_ARB_MAX_PAIRS": 8,
    "PAPER_STAT_ARB_MAX_PAIRS_EXPANDED": 9,
    "PAPER_STAT_ARB_MAX_PAIRS_CEILING": 10,
    "PAPER_STAT_ARB_COINT_PVALUE": 0.10,
    "PAPER_STAT_ARB_SECTOR_NEUTRAL_PREF": False,
    "DYNAMIC_CORE_ENABLED": False,
    "CORE_ALLOCATOR_LOCKED": True,
    "PROTECTIVE_SHORT_MAX_PCT": 0.12,
    "SHORT_RHYME_E_ENABLED": False,
}


def _realistic_research_config_snapshot() -> dict[str, float | int | bool]:
    return {
        "PAPER_STAT_ARB_MIN_CORR": float(config.PAPER_STAT_ARB_MIN_CORR),
        "PAPER_STAT_ARB_MAX_PAIRS": int(config.PAPER_STAT_ARB_MAX_PAIRS),
        "PAPER_STAT_ARB_MAX_PAIRS_EXPANDED": int(config.PAPER_STAT_ARB_MAX_PAIRS_EXPANDED),
        "PAPER_STAT_ARB_MAX_PAIRS_CEILING": int(config.PAPER_STAT_ARB_MAX_PAIRS_CEILING),
        "PAPER_STAT_ARB_COINT_PVALUE": float(config.PAPER_STAT_ARB_COINT_PVALUE),
        "PAPER_STAT_ARB_SECTOR_NEUTRAL_PREF": bool(config.PAPER_STAT_ARB_SECTOR_NEUTRAL_PREF),
        "DYNAMIC_CORE_ENABLED": bool(config.DYNAMIC_CORE_ENABLED),
        "CORE_ALLOCATOR_LOCKED": bool(config.CORE_ALLOCATOR_LOCKED),
        "PROTECTIVE_SHORT_MAX_PCT": float(config.PROTECTIVE_SHORT_MAX_PCT),
        "SHORT_RHYME_E_ENABLED": bool(config.SHORT_RHYME_E_ENABLED),
    }


def _apply_realistic_research_config(values: dict[str, float | int | bool]) -> None:
    for key, val in values.items():
        setattr(config, key, val)


def _v13_validation_metrics(result: dict) -> dict[str, float | int]:
    base = _stat_arb_validation_metrics(result)
    att = result.get("attribution") or {}
    sleeves = att.get("sleeves") or {}
    short_pnl = float((sleeves.get("opportunistic_short") or {}).get("total_pnl_usd", 0.0))
    os_data = att.get("opportunistic_short") or {}
    base["short_pnl"] = short_pnl
    base["short_entries"] = int(os_data.get("entry_fills", 0) or 0)
    try:
        from modules.core_allocator import core_allocator_snapshot

        snap = core_allocator_snapshot()
        base["core_vti_pct"] = float(snap.get("vti_pct", 0.40))
        base["core_choice"] = str(snap.get("choice", "spy"))
    except Exception:
        base["core_vti_pct"] = 0.40
        base["core_choice"] = "spy"
    return base


def run_realistic_research_v13_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare Realistic Research v1.2 vs v1.3 (full upgrade package)."""
    sim_days = days or config.BACKTEST_DAYS
    _prefetch_screener_for_backtest(sim_days, refresh=refresh, use_max=use_max)
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        data = _ensure_daily_data(sim_days, refresh=refresh, use_max=False)
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    base_kwargs = {
        "paper_aggressive": True,
        "paper_sleeve_features": True,
        "stat_arb_report": True,
    }
    configs = [
        ("Realistic Research v1.2", _LEGACY_V12_REALISTIC_RESEARCH),
        ("Realistic Research v1.3", _realistic_research_config_snapshot()),
    ]
    print("--- REALISTIC RESEARCH v1.3 VALIDATION (v1.2 vs v1.3) ---")
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        f"{'Config':<28} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'SA PnL':>9} {'Pairs':>6} {'Short$':>8} {'Core%':>6} {'SA nr':>6}"
    )
    print("-" * 100)

    saved = _realistic_research_config_snapshot()
    original_enforce = config.enforce_realistic_research_profile
    results: list[tuple[str, dict]] = []

    def _skip_profile_enforce() -> None:
        """Compare arms set config explicitly; do not re-lock to v1.3 defaults."""
        return None

    try:
        config.enforce_realistic_research_profile = _skip_profile_enforce
        for label, cfg in configs:
            _apply_realistic_research_config(cfg)
            if cfg.get("CORE_ALLOCATOR_LOCKED"):
                try:
                    from modules.core_allocator import lock_core_allocator

                    lock_core_allocator("spy")
                except ImportError:
                    pass
            else:
                try:
                    from modules.core_allocator import reset_core_allocator_state

                    reset_core_allocator_state()
                except ImportError:
                    pass
            result = run_backtest(data, track_metrics=True, **base_kwargs)
            m = _v13_validation_metrics(result)
            results.append((label, m))
            print(
                f"{label:<28} "
                f"{m['return_pct']:>+7.2f}% "
                f"{m['sharpe']:>7.2f} "
                f"{m['max_dd']:>7.2f}% "
                f"{m['stat_arb_pnl']:>+9.2f} "
                f"{m['stat_arb_pairs']:>6} "
                f"{m['short_pnl']:>+8.2f} "
                f"{m['core_vti_pct']*100:>5.0f}% "
                f"{m['sa_no_room']:>6}"
            )
    finally:
        config.enforce_realistic_research_profile = original_enforce
        _apply_realistic_research_config(saved)
        original_enforce()

    print("-" * 100)
    if len(results) == 2:
        _, m0 = results[0]
        _, m1 = results[1]
        print(
            f"Delta (v1.3 - v1.2): "
            f"return {m1['return_pct'] - m0['return_pct']:+.2f}pp | "
            f"Sharpe {m1['sharpe'] - m0['sharpe']:+.2f} | "
            f"MaxDD {m1['max_dd'] - m0['max_dd']:+.2f}pp | "
            f"SA PnL ${m1['stat_arb_pnl'] - m0['stat_arb_pnl']:+.2f} | "
            f"pairs {m1['stat_arb_pairs'] - m0['stat_arb_pairs']:+d} | "
            f"short PnL ${m1['short_pnl'] - m0['short_pnl']:+.2f} | "
            f"core VTI {m1['core_vti_pct']*100 - m0['core_vti_pct']*100:+.0f}pp | "
            f"SA no_room {m1['sa_no_room'] - m0['sa_no_room']:+d}"
        )


_LEGACY_V13_STAT_ARB_PRE = {
    "PAPER_STAT_ARB_MIN_CORR": 0.72,
    "PAPER_STAT_ARB_COINT_PVALUE": 0.12,
    "PAPER_STAT_ARB_MAX_PAIRS": 10,
    "PAPER_STAT_ARB_MAX_PAIRS_EXPANDED": 11,
    "PAPER_STAT_ARB_MAX_PAIRS_CEILING": 12,
    "PAPER_STAT_ARB_Z_ENTRY_BASE": 2.0,
    "PAPER_STAT_ARB_Z_ENTRY_MAX": 2.5,
    "PAPER_STAT_ARB_RISK_REWARD": 1.5,
    "PAPER_STAT_ARB_MIN_DOLLAR_VOLUME": 25_000_000,
}


def _stat_arb_v13_config_snapshot() -> dict[str, float | int]:
    return {
        "PAPER_STAT_ARB_MIN_CORR": float(config.PAPER_STAT_ARB_MIN_CORR),
        "PAPER_STAT_ARB_COINT_PVALUE": float(config.PAPER_STAT_ARB_COINT_PVALUE),
        "PAPER_STAT_ARB_MAX_PAIRS": int(config.PAPER_STAT_ARB_MAX_PAIRS),
        "PAPER_STAT_ARB_MAX_PAIRS_EXPANDED": int(config.PAPER_STAT_ARB_MAX_PAIRS_EXPANDED),
        "PAPER_STAT_ARB_MAX_PAIRS_CEILING": int(config.PAPER_STAT_ARB_MAX_PAIRS_CEILING),
        "PAPER_STAT_ARB_Z_ENTRY_BASE": float(config.PAPER_STAT_ARB_Z_ENTRY_BASE),
        "PAPER_STAT_ARB_Z_ENTRY_MAX": float(config.PAPER_STAT_ARB_Z_ENTRY_MAX),
        "PAPER_STAT_ARB_RISK_REWARD": float(config.PAPER_STAT_ARB_RISK_REWARD),
        "PAPER_STAT_ARB_MIN_DOLLAR_VOLUME": float(config.PAPER_STAT_ARB_MIN_DOLLAR_VOLUME),
    }


def run_stat_arb_v13_push_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare Stat Arb v1.3 pre-push vs pushed capacity (10-14p, RR 1.6, Z 2.0-2.6)."""
    sim_days = days or config.BACKTEST_DAYS
    _prefetch_screener_for_backtest(sim_days, refresh=refresh, use_max=use_max)
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        data = _ensure_daily_data(sim_days, refresh=refresh, use_max=False)
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    base_kwargs = {
        "paper_aggressive": True,
        "paper_sleeve_features": True,
        "stat_arb_report": True,
    }
    configs = [
        ("v1.3 before (10-12p RR1.5)", _LEGACY_V13_STAT_ARB_PRE),
        ("v1.3 pushed (10-14p RR1.6)", _stat_arb_v13_config_snapshot()),
    ]
    print("--- STAT ARB v1.3 PUSH (before vs after) ---")
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        f"{'Config':<28} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'SA PnL':>9} {'Fill%':>6} {'Pairs':>6} {'SA nr':>6}"
    )
    print("-" * 88)

    saved = _stat_arb_v13_config_snapshot()
    original_enforce = config.enforce_realistic_research_profile
    results: list[tuple[str, dict]] = []

    def _skip_profile_enforce() -> None:
        return None

    try:
        config.enforce_realistic_research_profile = _skip_profile_enforce
        for label, cfg in configs:
            _apply_stat_arb_config(cfg)
            result = run_backtest(data, track_metrics=True, **base_kwargs)
            m = _stat_arb_validation_metrics(result)
            sa = (result.get("attribution") or {}).get("stat_arb") or {}
            m["fill_rate"] = float(sa.get("fill_rate_pct", 0) or 0)
            results.append((label, m))
            print(
                f"{label:<28} "
                f"{m['return_pct']:>+7.2f}% "
                f"{m['sharpe']:>7.2f} "
                f"{m['max_dd']:>7.2f}% "
                f"{m['stat_arb_pnl']:>+9.2f} "
                f"{m['fill_rate']:>5.1f}% "
                f"{m['stat_arb_pairs']:>6} "
                f"{m['sa_no_room']:>6}"
            )
    finally:
        config.enforce_realistic_research_profile = original_enforce
        _apply_stat_arb_config(saved)

    print("-" * 88)
    if len(results) == 2:
        _, m0 = results[0]
        _, m1 = results[1]
        print(
            f"Delta (pushed - before): "
            f"return {m1['return_pct'] - m0['return_pct']:+.2f}pp | "
            f"Sharpe {m1['sharpe'] - m0['sharpe']:+.2f} | "
            f"MaxDD {m1['max_dd'] - m0['max_dd']:+.2f}pp | "
            f"SA PnL ${m1['stat_arb_pnl'] - m0['stat_arb_pnl']:+.2f} | "
            f"fill {m1['fill_rate'] - m0['fill_rate']:+.1f}pp | "
            f"pairs {m1['stat_arb_pairs'] - m0['stat_arb_pairs']:+d} | "
            f"no_room {m1['sa_no_room'] - m0['sa_no_room']:+d}"
        )


_LEGACY_STAT_ARB_CONFIG = {
    "PAPER_STAT_ARB_MAX_PAIRS": 4,
    "PAPER_STAT_ARB_MAX_PAIRS_EXPANDED": 6,
    "PAPER_STAT_ARB_MAX_PAIRS_CEILING": 8,
    "PAPER_STAT_ARB_RISK_REWARD": 1.2,
    "PAPER_STAT_ARB_MIN_DOLLAR_VOLUME": 0,
    "PAPER_STAT_ARB_TRAILING_ARM_FRAC": 2.0,
    "PAPER_STAT_ARB_TRAILING_PULLBACK_FRAC": 0.35,
}


def _stat_arb_config_snapshot() -> dict[str, float | int]:
    return {
        "PAPER_STAT_ARB_MAX_PAIRS": int(config.PAPER_STAT_ARB_MAX_PAIRS),
        "PAPER_STAT_ARB_MAX_PAIRS_EXPANDED": int(config.PAPER_STAT_ARB_MAX_PAIRS_EXPANDED),
        "PAPER_STAT_ARB_MAX_PAIRS_CEILING": int(config.PAPER_STAT_ARB_MAX_PAIRS_CEILING),
        "PAPER_STAT_ARB_RISK_REWARD": float(config.PAPER_STAT_ARB_RISK_REWARD),
        "PAPER_STAT_ARB_MIN_DOLLAR_VOLUME": float(config.PAPER_STAT_ARB_MIN_DOLLAR_VOLUME),
        "PAPER_STAT_ARB_TRAILING_ARM_FRAC": float(config.PAPER_STAT_ARB_TRAILING_ARM_FRAC),
        "PAPER_STAT_ARB_TRAILING_PULLBACK_FRAC": float(
            config.PAPER_STAT_ARB_TRAILING_PULLBACK_FRAC
        ),
    }


def _apply_stat_arb_config(values: dict[str, float | int]) -> None:
    for key, val in values.items():
        setattr(config, key, val)


def _stat_arb_validation_metrics(result: dict) -> dict[str, float | int]:
    att = result.get("attribution") or {}
    sa = att.get("stat_arb") or {}
    sleeves = att.get("sleeves") or {}
    stat_pnl = float((sleeves.get("stat_arb") or {}).get("total_pnl_usd", 0.0))
    skip = result.get("entry_skip_breakdown") or {}
    by_cat = skip.get("by_category") or {}
    rejects = att.get("stat_arb_rejects") or {}
    return {
        "return_pct": float(result.get("total_return_pct", 0) or 0),
        "sharpe": float(result.get("sharpe", 0) or 0),
        "max_dd": float(result.get("max_drawdown_pct", 0) or 0),
        "stat_arb_pnl": stat_pnl,
        "stat_arb_pairs": int(sa.get("pair_entries", 0) or 0),
        "stat_arb_win": float((sleeves.get("stat_arb") or {}).get("win_rate_pct", 0) or 0),
        "avg_z": float(sa.get("avg_entry_z", 0) or 0),
        "avg_hold": float(sa.get("avg_hold_bars", 0) or 0),
        "sa_no_room": int(rejects.get("no_room", 0) or 0),
        "skip_no_room": int(by_cat.get("no_room", 0) or 0),
    }


def run_stat_arb_v12_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare stat arb v1.1 (4-8 pairs, 1.2 RR) vs v1.2 (8-10, 1.5 RR + trail)."""
    sim_days = days or config.BACKTEST_DAYS
    _prefetch_screener_for_backtest(sim_days, refresh=refresh, use_max=use_max)
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        data = _ensure_daily_data(sim_days, refresh=refresh, use_max=False)
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    base_kwargs = {
        "paper_aggressive": True,
        "paper_sleeve_features": True,
        "stat_arb_report": True,
    }
    configs = [
        ("Stat Arb v1.1 (4-8p, RR 1.2)", _LEGACY_STAT_ARB_CONFIG),
        ("Stat Arb v1.2 (8-10p, RR 1.5)", _stat_arb_config_snapshot()),
    ]
    print("--- STAT ARB v1.2 VALIDATION (Realistic Research v1.1c) ---")
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        f"{'Config':<28} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'SA PnL':>9} {'Pairs':>6} {'Win%':>5} {'AvgZ':>5} "
        f"{'SA nr':>6} {'Skip':>5}"
    )
    print("-" * 98)

    saved = _stat_arb_config_snapshot()
    results: list[tuple[str, dict]] = []
    try:
        for label, cfg in configs:
            _apply_stat_arb_config(cfg)
            result = run_backtest(data, track_metrics=True, **base_kwargs)
            m = _stat_arb_validation_metrics(result)
            results.append((label, m))
            print(
                f"{label:<28} "
                f"{m['return_pct']:>+7.2f}% "
                f"{m['sharpe']:>7.2f} "
                f"{m['max_dd']:>7.2f}% "
                f"{m['stat_arb_pnl']:>+9.2f} "
                f"{m['stat_arb_pairs']:>6} "
                f"{m['stat_arb_win']:>4.0f}% "
                f"{m['avg_z']:>5.2f} "
                f"{m['sa_no_room']:>6} "
                f"{m['skip_no_room']:>5}"
            )
    finally:
        _apply_stat_arb_config(saved)

    print("-" * 98)
    if len(results) == 2:
        _, m0 = results[0]
        _, m1 = results[1]
        print(
            f"Delta (v1.2 - v1.1): "
            f"return {m1['return_pct'] - m0['return_pct']:+.2f}pp | "
            f"Sharpe {m1['sharpe'] - m0['sharpe']:+.2f} | "
            f"MaxDD {m1['max_dd'] - m0['max_dd']:+.2f}pp | "
            f"SA PnL ${m1['stat_arb_pnl'] - m0['stat_arb_pnl']:+.2f} | "
            f"pairs {m1['stat_arb_pairs'] - m0['stat_arb_pairs']:+d} | "
            f"SA no_room {m1['sa_no_room'] - m0['sa_no_room']:+d} | "
            f"skip no_room {m1['skip_no_room'] - m0['skip_no_room']:+d}"
        )


def run_stat_arb_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare paper aggressive with vs without statistical arbitrage sleeve."""
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
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
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
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
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
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
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
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

    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
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
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
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
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
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
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
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
        if len(data) < 20:
            print(f"{label}: need 20 bars; got {len(data)}.")
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
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
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
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
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
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
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
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
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


def run_nyse_conditional_compare(days=None, refresh=False, use_max=False) -> None:
    """Paper aggressive: NYSE conditional-on-SPY filter on vs off."""
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
        return

    bench = _benchmark_return(data, MIN_HISTORY)
    configs = [
        (
            "Paper (no NYSE conditional)",
            {
                "paper_aggressive": True,
                "paper_sleeve_features": True,
                "paper_dynamic_vti": True,
                "nyse_conditional_on_spy": False,
            },
        ),
        (
            "Paper (+NYSE conditional on SPY)",
            {
                "paper_aggressive": True,
                "paper_sleeve_features": True,
                "paper_dynamic_vti": True,
                "nyse_conditional_on_spy": True,
            },
        ),
    ]
    print("--- NYSE CONDITIONAL ON SPY A/B (paper aggressive) ---")
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    print(
        f"{'Config':<34} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'SPY+NYSE':>8} {'NYSE':>5}"
    )
    print("-" * 82)

    results: list[tuple[str, dict]] = []
    for label, kwargs in configs:
        result = run_backtest(data, track_active_exposure=True, track_metrics=True, **kwargs)
        results.append((label, result))
        print(
            f"{label:<34} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{result.get('spy_nyse_cofire_pct', 0):>7.1f}% "
            f"{result.get('nyse_signals', 0):>5}"
        )
    print("-" * 82)
    for label, result in results:
        picks = result.get("nyse_pick_counts") or {}
        top = list(picks.items())[:6]
        pick_txt = ", ".join(f"{s}({n})" for s, n in top) if top else "—"
        print(f"{label}: top NYSE picks -> {pick_txt}")
    print("-" * 82)


def run_dynamic_universe_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare static UNIVERSE vs strict dynamic screener (paper aggressive)."""
    saved_dyn = config.PAPER_DYNAMIC_UNIVERSE_ENABLED
    saved_strict = config.PAPER_DYNAMIC_UNIVERSE_STRICT
    config.PAPER_DYNAMIC_UNIVERSE_ENABLED = True
    config.PAPER_DYNAMIC_UNIVERSE_STRICT = True
    config.set_paper_aggressive_context(True)
    config.set_backtest_paper_sleeves_context(True)

    sim_days = days or config.BACKTEST_DAYS
    from modules.dynamic_universe import screener_universe_meta

    filters = (screener_universe_meta().get("filters") or {})
    need_strict_file = filters.get("strict_mode") is not True
    screener = _prefetch_screener_for_backtest(
        sim_days, refresh=refresh or need_strict_file, use_max=use_max
    )

    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        data = _ensure_daily_data(sim_days, refresh=refresh, use_max=False)

    config.PAPER_DYNAMIC_UNIVERSE_ENABLED = saved_dyn
    config.PAPER_DYNAMIC_UNIVERSE_STRICT = saved_strict

    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
        return

    static_size = len(_static_equity_universe(data.columns))
    saved_dyn = config.PAPER_DYNAMIC_UNIVERSE_ENABLED
    saved_strict = config.PAPER_DYNAMIC_UNIVERSE_STRICT
    config.PAPER_DYNAMIC_UNIVERSE_ENABLED = True
    config.PAPER_DYNAMIC_UNIVERSE_STRICT = True
    dyn_size = len(_dynamic_equity_universe(data.columns))
    config.PAPER_DYNAMIC_UNIVERSE_ENABLED = saved_dyn
    config.PAPER_DYNAMIC_UNIVERSE_STRICT = saved_strict
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
            {**base_kwargs, "paper_dynamic_universe": False, "paper_dynamic_universe_strict": False},
        ),
        (
            f"Dynamic strict ({dyn_size} names)",
            {
                **base_kwargs,
                "paper_dynamic_universe": True,
                "paper_dynamic_universe_strict": True,
            },
        ),
    ]

    print("--- PAPER DYNAMIC UNIVERSE A/B (static vs strict screener, paper only) ---")
    print(
        f"Window: {data.index[MIN_HISTORY].date()} -> {data.index[-1].date()} "
        f"({len(data) - MIN_HISTORY} sim bars)"
    )
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")
    from modules.dynamic_universe import STRICT_MIN_AVG_DOLLAR_VOLUME

    print(
        f"Screener file: {len(screener)} tickers | "
        f"static pool {static_size} | dynamic strict pool {dyn_size} | "
        f"filters: 30d momentum rank, ${STRICT_MIN_AVG_DOLLAR_VOLUME/1e6:.0f}M $vol, "
        f"sector cap, ETB-only (no short-interest feed)"
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

    results: list[tuple[str, dict]] = []
    for label, kwargs in configs:
        result = run_backtest(data, **kwargs)
        results.append((label, result))
        print(
            f"{label:<32} "
            f"{result['total_return_pct']:>+7.2f}% "
            f"{result['sharpe']:>7.2f} "
            f"{result['max_drawdown_pct']:>7.2f}% "
            f"{result.get('nyse_signals', 0):>7} "
            f"{result.get('equity_universe_size', 0):>5}"
        )

    print("-" * 82)
    if len(results) == 2:
        _, static_r = results[0]
        _, dyn_r = results[1]
        print(
            f"Delta (dynamic strict - static): "
            f"return {dyn_r['total_return_pct'] - static_r['total_return_pct']:+.2f}pp | "
            f"Sharpe {dyn_r['sharpe'] - static_r['sharpe']:+.2f} | "
            f"MaxDD {dyn_r['max_drawdown_pct'] - static_r['max_drawdown_pct']:+.2f}pp | "
            f"Trades {dyn_r.get('nyse_signals', 0) - static_r.get('nyse_signals', 0):+d}"
        )
    print("-" * 82)


def run_dynamic_vti_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare fixed 20% VTI vs dynamic 40-75% VTI on paper aggressive profile."""
    if use_max:
        data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    else:
        days = days or config.BACKTEST_DAYS
        data = _ensure_daily_data(days, refresh=refresh, use_max=False)
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
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
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
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
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
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
    stat_arb_report: bool | None = None,
    paper_crypto_enabled: bool | None = None,
    regime_filter: str | None = None,
    regime_breakdown: bool = False,
    deep_history: bool = False,
    deep_history_indicators_only: bool = False,
    max_years: int = 20,
):
    if paper_aggressive and not deep_history and config.DEEP_HISTORY_ENABLED:
        deep_history = True
    if deep_history and not deep_history_indicators_only and config.DEEP_HISTORY_INDICATORS_ONLY:
        deep_history_indicators_only = True
    if use_max:
        print("--- STARTING FUND BACKTEST (max available daily history) ---")
    else:
        days = days or config.BACKTEST_DAYS
        print(f"--- STARTING FUND BACKTEST ({days} days) ---")
    if small_account and not paper_aggressive:
        conservative = vti_core_pct <= 0 or abs(
            vti_core_pct - config.LIVE_VTI_CORE_PCT
        ) < 0.005
        if vti_core_pct > 0 and abs(vti_core_pct - 0.90) < 0.005:
            conservative = False
        config.set_backtest_small_account_context(True)
        config.set_backtest_live_conservative_context(conservative)
        if conservative:
            config.enforce_live_small_account_profile()
    if small_account:
        if config.live_conservative_profile_active():
            print(
                f"--- LIVE CONSERVATIVE: ${config.SMALL_ACCOUNT_BACKTEST_EQUITY:,.0f} start | "
                f"{config.format_live_conservative_banner()} ---"
            )
        else:
            display_vti = (
                vti_core_pct if vti_core_pct > 0 else config.SMALL_ACCOUNT_VTI_CORE_PCT
            )
            print(
                f"--- SMALL-ACCOUNT MODE (legacy): ${config.SMALL_ACCOUNT_BACKTEST_EQUITY:,.0f} start | "
                f"{display_vti:.0%} {VTI_CORE_SYMBOL} | "
                f"risk {config.SMALL_ACCOUNT_RISK_PER_TRADE:.0%} | "
                f"max ${config.SMALL_ACCOUNT_MAX_NOTIONAL:,.0f}/order ---"
            )
        if vti_core_pct <= 0:
            vti_core_pct = (
                config.LIVE_VTI_CORE_PCT
                if config.live_conservative_profile_active()
                else config.SMALL_ACCOUNT_VTI_CORE_PCT
            )
    elif paper_aggressive:
        if config.effective_core_allocator_locked() and vti_core_pct <= 0:
            from modules.core_allocator import (
                current_core_choice,
                effective_vti_core_pct,
                lock_core_allocator,
            )

            lock_core_allocator()
            vti_core_pct = float(effective_vti_core_pct() or config.PAPER_VTI_CORE_PCT)
            print(
                f"--- PAPER AGGRESSIVE: locked {current_core_choice().upper()} @ "
                f"{vti_core_pct:.0%} passive core | "
                f"{1 - vti_core_pct:.0%} active (boost "
                f"{config.PAPER_ACTIVE_SLEEVE_BOOST:.0%}x) ---"
            )
        elif not config.PAPER_DYNAMIC_VTI_ENABLED and vti_core_pct <= 0:
            vti_core_pct = config.PAPER_VTI_CORE_PCT
            print(
                f"--- PAPER AGGRESSIVE: {vti_core_pct:.0%} {VTI_CORE_SYMBOL} core | "
                f"{1 - vti_core_pct:.0%} active (boost "
                f"{config.PAPER_ACTIVE_SLEEVE_BOOST:.0%}x) ---"
            )
        elif config.PAPER_DYNAMIC_VTI_ENABLED:
            print(
                f"--- PAPER AGGRESSIVE: dynamic {VTI_CORE_SYMBOL} "
                f"({config.DYNAMIC_VTI_PAPER_FLOOR:.0%}-75% by vol/stress) | "
                f"boost {config.PAPER_ACTIVE_SLEEVE_BOOST:.0%}x ---"
            )
        else:
            if vti_core_pct <= 0:
                vti_core_pct = config.PAPER_VTI_CORE_PCT
            print(
                f"--- PAPER AGGRESSIVE: {vti_core_pct:.0%} {VTI_CORE_SYMBOL} core | "
                f"{1 - vti_core_pct:.0%} active (boost "
                f"{config.PAPER_ACTIVE_SLEEVE_BOOST:.0%}x) ---"
            )
    elif vti_core_pct > 0:
        print(
            f"--- VTI core: {vti_core_pct:.0%} passive {VTI_CORE_SYMBOL} | "
            f"{1 - vti_core_pct:.0%} active sleeves ---"
        )
    try:
        from modules.operating_layer import format_operating_layer_banner

        op_banner = format_operating_layer_banner()
        if op_banner:
            print(f"--- {op_banner} ---")
    except Exception:
        pass
    if regime_filter:
        resolved = resolve_regime_name(regime_filter) or regime_filter
        print(f"--- REGIME FILTER: trade only on {resolved} ---")
    if deep_history:
        print(
            f"--- DEEP INDICATOR HISTORY: up to {max(1, int(max_years))}y "
            f"(Alpaca + yfinance, cached as *_deep.pkl) ---"
        )
        _print_deep_history_mode_banner(
            deep_history=deep_history,
            deep_history_indicators_only=deep_history_indicators_only,
            max_years=max_years,
        )
        if refresh:
            clear_deep_history_cache()
    config.DEEP_HISTORY_ENABLED = bool(deep_history)
    config.DEEP_HISTORY_INDICATORS_ONLY = bool(deep_history_indicators_only)
    try:
        full_data = _ensure_daily_data(days or 0, refresh=refresh, use_max=use_max)
    except Exception as e:
        print("Database error: " + str(e))
        return
    if len(full_data) < 20:
        sim_target = days or config.BACKTEST_DAYS
        print(
            f"Need at least 20 daily bars to run; got {len(full_data)}."
        )
        print(
            "Run: python fetch_data.py --daily --days "
            f"{_calendar_days_to_fetch(sim_target)}"
        )
        print("Or:  python fetch_data.py --daily --max")
        return

    sim_target_days = days or config.BACKTEST_DAYS
    target_sim_bars = max(5, int(sim_target_days * 0.80))
    indicator_context = None
    allocator_data = None
    data = full_data
    if deep_history:
        if deep_history_indicators_only:
            allocator_data = _trim_baseline_backtest_data(
                full_data, target_sim_bars=target_sim_bars
            )
            if len(full_data) > target_sim_bars:
                data = full_data.iloc[-target_sim_bars:].copy()
            n_bars = len(data)
            warmup = 0
            start_date = data.index[0]
            end_date = data.index[-1]
            indicator_context = _build_indicator_context(
                data, max_years=max_years, refresh=refresh
            )
            if indicator_context is None or indicator_context.empty:
                print(
                    "WARNING: deep indicator history unavailable; "
                    "falling back to in-window warmup."
                )
                indicator_context = None
                allocator_data = None
                data = allocator_data or _trim_baseline_backtest_data(
                    full_data, target_sim_bars=target_sim_bars
                )
                n_bars = len(data)
                warmup = min(MIN_HISTORY, max(0, n_bars - 5))
                start_date = data.index[warmup]
            else:
                _print_indicator_context_summary(indicator_context, data)
        else:
            if len(full_data) > target_sim_bars:
                data = full_data.iloc[-target_sim_bars:].copy()
            n_bars = len(data)
            warmup = 0
            start_date = data.index[0]
            end_date = data.index[-1]
            indicator_context = _build_indicator_context(
                data, max_years=max_years, refresh=refresh
            )
            if indicator_context is None or indicator_context.empty:
                print(
                    "WARNING: deep indicator history unavailable; "
                    "falling back to in-window warmup."
                )
                indicator_context = None
                n_bars = len(data)
                warmup = min(MIN_HISTORY, max(0, n_bars - 5))
                start_date = data.index[warmup]
            else:
                _print_indicator_context_summary(indicator_context, data)
    else:
        data = _trim_baseline_backtest_data(full_data, target_sim_bars=target_sim_bars)
        n_bars = len(data)
        warmup = min(MIN_HISTORY, max(0, n_bars - 5))
        start_date = data.index[warmup]
        end_date = data.index[-1]
    cooldown_bars = DAILY_COOLDOWN_BARS
    bar_label = "daily bars"
    sim_bars = len(data) - warmup
    sim_days = (end_date - start_date).days

    print(f"Loaded {len(data.columns)} tickers over {len(data)} {bar_label}.")
    if deep_history:
        print(
            f"Warmup: 0 bars (deep indicator context) | "
            f"Simulation: {sim_bars} bars"
        )
        if deep_history_indicators_only and allocator_data is not None:
            print(
                f"Allocator window: {len(allocator_data)} bars "
                f"(baseline sim + warmup, pinned)"
            )
    else:
        print(
            f"Warmup: {warmup} bars (SPY MA{config.SPY_MA_WINDOW}) | "
            f"Simulation: {sim_bars} bars"
        )
    print(f"Simulation window: {start_date.date()} to {end_date.date()} ({sim_days} calendar days)")
    print(f"Cooldown: {cooldown_bars} bar(s) (~{COOLDOWN_SECONDS // 60} min live logic)")

    saved_paper_ctx = config.paper_aggressive_context()
    saved_small_ctx = config.backtest_small_account_context()
    saved_live_conservative_ctx = config.backtest_live_conservative_context()
    saved_bt_sleeves = config.backtest_paper_sleeves_context()
    saved_vti_ceil = config.backtest_vti_ceiling()
    saved_paper_crypto = config.PAPER_CRYPTO_ENABLED
    saved_paper_crypto_v2 = config.PAPER_CRYPTO_V2_ENABLED
    config.set_paper_aggressive_context(paper_aggressive)
    config.set_backtest_small_account_context(small_account)
    if small_account and not paper_aggressive:
        conservative = vti_core_pct <= 0 or abs(
            vti_core_pct - config.LIVE_VTI_CORE_PCT
        ) < 0.005
        if vti_core_pct > 0 and abs(vti_core_pct - 0.90) < 0.005:
            conservative = False
        config.set_backtest_live_conservative_context(conservative)
        if conservative:
            config.enforce_live_small_account_profile()
    else:
        config.set_backtest_live_conservative_context(False)
    config.set_backtest_paper_sleeves_context(paper_aggressive)
    if paper_crypto_enabled is not None:
        config.PAPER_CRYPTO_ENABLED = bool(paper_crypto_enabled)
        if not paper_crypto_enabled:
            config.PAPER_CRYPTO_V2_ENABLED = False
    if paper_aggressive and not config.PAPER_DYNAMIC_VTI_ENABLED:
        effective_vti = (
            vti_core_pct if vti_core_pct > 0 else config.PAPER_VTI_CORE_PCT
        )
        config.set_backtest_vti_ceiling(effective_vti)
    try:
        stack_profile = "paper" if paper_aggressive else "live"
        config.print_recommended_stack_flags(profile=stack_profile)
        alloc = config.fund_allocation_pct()
    finally:
        config.set_paper_aggressive_context(saved_paper_ctx)
        config.set_backtest_small_account_context(saved_small_ctx)
        config.set_backtest_live_conservative_context(saved_live_conservative_ctx)
        config.set_backtest_paper_sleeves_context(saved_bt_sleeves)
        config.set_backtest_vti_ceiling(saved_vti_ceil)
        config.PAPER_CRYPTO_ENABLED = saved_paper_crypto
        config.PAPER_CRYPTO_V2_ENABLED = saved_paper_crypto_v2

    result = run_backtest(
        data,
        track_spy_fill=False,
        verbose=True,
        vti_core_pct=vti_core_pct,
        paper_aggressive=paper_aggressive,
        small_account=small_account,
        stat_arb_report=stat_arb_report,
        paper_crypto_enabled=paper_crypto_enabled,
        regime_filter=regime_filter,
        indicator_context=indicator_context,
        max_years=max_years,
        allocator_data=allocator_data,
        deep_history_indicators_only=deep_history_indicators_only,
    )
    core = (result or {}).get("core_allocator") or {}
    if core:
        choice = str(core.get("choice", "?")).upper()
        pct = float(core.get("vti_pct", 0.0))
        metrics = core.get("metrics") or {}
        vti_sh = float((metrics.get("vti") or {}).get("sharpe", 0.0))
        spy_sh = float((metrics.get("spy") or {}).get("sharpe", 0.0))
        print(
            f"Final core allocator: {choice} @ {pct:.0%} "
            f"(Sharpe VTI {vti_sh:.2f} vs SPY {spy_sh:.2f})"
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
    if result.get("attribution"):
        from modules.backtest_attribution import (
            format_crypto_banner,
            format_opportunistic_short_banner,
            format_stat_arb_banner,
        )

        stat_arb_banner = format_stat_arb_banner(result["attribution"])
        if stat_arb_banner:
            print(stat_arb_banner)
        short_banner = format_opportunistic_short_banner(result["attribution"])
        if short_banner:
            print(short_banner)
        crypto_banner = format_crypto_banner(result["attribution"])
        if crypto_banner:
            print(crypto_banner)
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
    if result.get("vti_core_pct") is not None:
        core_avg = float(result["vti_core_pct"]) * 100
        if config.effective_core_allocator_locked():
            from modules.core_allocator import current_core_choice

            label = current_core_choice().upper()
            print(f"Core {label} (avg): {core_avg:.1f}% (locked)")
        else:
            print(f"VTI core (avg):   {core_avg:.1f}%")
    elif alloc.get("vti_core"):
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
    skip_bd = result.get("entry_skip_breakdown") or {}
    if skip_bd:
        print(
            f"Skip breakdown:   cycles={skip_bd.get('cycles', 0)} "
            f"traded={skip_bd.get('traded_cycles', 0)} "
            f"skipped={skip_bd.get('skipped_cycles', 0)}"
        )
        by_cat = skip_bd.get("by_category") or {}
        by_token = skip_bd.get("by_token") or {}
        from modules.entry_skip_tracker import _top_blockers

        blocker_lines = _top_blockers(by_cat, by_token, n=2)
        if blocker_lines:
            for line in blocker_lines:
                print(f"  {line}")
        elif by_cat:
            cat_line = ", ".join(
                f"{k}={v}" for k, v in sorted(by_cat.items(), key=lambda x: -x[1])
            )
            print(f"  by category:    {cat_line}")
        top_tok = sorted(by_token.items(), key=lambda x: -x[1])[:8]
        if top_tok and not blocker_lines:
            tok_line = ", ".join(f"{k}={v}" for k, v in top_tok)
            print(f"  top tokens:     {tok_line}")
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
    if regime_breakdown:
        print(format_regime_breakdown_table(result.get("regime_breakdown") or []))
    if result.get("regime_filter"):
        skips = result.get("regime_filter_skips", 0)
        print(f"Regime filter skips: {skips} bars (no new trading)")
    print(f"Profit factor:    {round(result.get('profit_factor', 0), 2)}")
    print(f"Avg trade return: {round(result.get('avg_trade_return_pct', 0), 3)}%")
    if result.get("attribution"):
        from modules.backtest_attribution import print_attribution_report

        print_attribution_report(result["attribution"])
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
        "--deep-history",
        action="store_true",
        help=(
            "Warm indicators from deepest available daily history (Alpaca + yfinance) "
            "while trading only over the requested simulation window"
        ),
    )
    parser.add_argument(
        "--deep-history-indicators-only",
        action="store_true",
        help=(
            "With --deep-history: use deep context for indicators/regime only; "
            "pin core allocator Sharpe to baseline sim window"
        ),
    )
    parser.add_argument(
        "--max-years",
        type=int,
        default=20,
        metavar="N",
        help="Cap yfinance/Alpaca lookback when --deep-history is set (default: 20)",
    )
    parser.add_argument(
        "--vti-core",
        type=float,
        default=0.0,
        metavar="PCT",
        help="Passive core fraction (e.g. 0.4 = 40%% passive). Overrides locked allocator when set.",
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
        "--dynamic-core",
        action="store_true",
        help="Enable dynamic core allocator (VTI/SPY/blend/cash); overrides --vti-core",
    )
    parser.add_argument(
        "--paper-crypto",
        action="store_true",
        help="Enable PAPER_CRYPTO_ENABLED (vol-gated crypto / stat-arb crypto sleeve)",
    )
    parser.add_argument(
        "--regime-breakdown",
        action="store_true",
        help="Print performance split by RHYME regime (A–E)",
    )
    parser.add_argument(
        "--regime",
        default=None,
        metavar="NAME",
        help="Trade only on matching RHYME regime (e.g. RHYME_D or D)",
    )
    parser.add_argument(
        "--small-account",
        action="store_true",
        help=(
            "Live small-account profile: $100 start, 85%% VTI + 5%% SPY trend, 1%% risk, "
            "$10 max order, scaled min notional (use --vti-core 0.90 for legacy baseline)"
        ),
    )
    parser.add_argument(
        "--no-nyse-conditional",
        action="store_true",
        help="Disable NYSE conditional-on-SPY filter (paper aggressive only)",
    )
    parser.add_argument(
        "--compare-nyse-conditional",
        action="store_true",
        help="Compare paper aggressive with vs without NYSE conditional-on-SPY filter",
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
        help="Compare small-account live sim with vs without capped thinking tilts (±6%%)",
    )
    parser.add_argument(
        "--with-news",
        action="store_true",
        help=(
            "With --simulate-live-thinking: compare no-thinking vs thinking+news "
            "(synthetic 8 AM digest per bar)"
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
        "--compare-realistic-research-v13",
        action="store_true",
        help="Compare Realistic Research v1.2 vs v1.3 (stat arb, shorts, core, monitoring)",
    )
    parser.add_argument(
        "--compare-stat-arb-v13-push",
        action="store_true",
        help="Compare Stat Arb v1.3 before (10-12p RR1.5) vs pushed (10-14p RR1.6 Z2.6)",
    )
    parser.add_argument(
        "--compare-stat-arb-v12",
        action="store_true",
        help="Compare stat arb v1.1 vs v1.2 (8-10 pairs, 1.5 RR, trailing stop)",
    )
    parser.add_argument(
        "--compare-stat-arb",
        action="store_true",
        help="Compare paper aggressive with vs without statistical arbitrage sleeve",
    )
    parser.add_argument(
        "--compare-opportunistic-shorts",
        action="store_true",
        help="Compare paper v1.1c with vs without opportunistic shorts (max 15%%)",
    )
    parser.add_argument(
        "--compare-universe-size",
        action="store_true",
        help="Compare legacy (75/35/160) vs expanded (110/45/180) universe sizing",
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
        "--stat-arb-report",
        dest="stat_arb_report",
        action="store_true",
        default=None,
        help="Print per-sleeve attribution (default: on for --paper-aggressive)",
    )
    parser.add_argument(
        "--no-stat-arb-report",
        dest="stat_arb_report",
        action="store_false",
        help="Skip stat arb / sleeve attribution block in backtest report",
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
    elif args.compare_nyse_conditional:
        if not args.paper_aggressive:
            print("--compare-nyse-conditional requires --paper-aggressive")
            sys.exit(1)
        run_nyse_conditional_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
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
    elif args.compare_opportunistic_shorts:
        if not args.paper_aggressive:
            print("--compare-opportunistic-shorts requires --paper-aggressive")
            sys.exit(1)
        run_opportunistic_short_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_universe_size:
        if not args.paper_aggressive:
            print("--compare-universe-size requires --paper-aggressive")
            sys.exit(1)
        run_universe_size_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_realistic_research_v13:
        if not args.paper_aggressive:
            print("--compare-realistic-research-v13 requires --paper-aggressive")
            sys.exit(1)
        run_realistic_research_v13_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_stat_arb_v13_push:
        if not args.paper_aggressive:
            print("--compare-stat-arb-v13-push requires --paper-aggressive")
            sys.exit(1)
        run_stat_arb_v13_push_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
        )
    elif args.compare_stat_arb_v12:
        if not args.paper_aggressive:
            print("--compare-stat-arb-v12 requires --paper-aggressive")
            sys.exit(1)
        run_stat_arb_v12_compare(
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
    elif args.compare_crypto_universe:
        if not args.paper_aggressive:
            print("--compare-crypto-universe requires --paper-aggressive")
            sys.exit(1)
        run_compare_crypto_universe(
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
        if args.no_nyse_conditional and args.paper_aggressive:
            config.PAPER_NYSE_CONDITIONAL_ON_SPY = False
        if args.dynamic_core:
            config.DYNAMIC_CORE_ENABLED = True
        regime_filter = None
        if args.regime:
            regime_filter = resolve_regime_name(args.regime)
            if not regime_filter:
                from modules.backtester_core import RHYME_REGIME_LABELS

                print(f"Unknown regime: {args.regime}")
                print(
                    "Valid examples: "
                    + ", ".join(r.split(":")[0].strip() for r in RHYME_REGIME_LABELS)
                )
                raise SystemExit(1)
        if args.deep_history_indicators_only and not args.deep_history:
            print("ERROR: --deep-history-indicators-only requires --deep-history")
            raise SystemExit(1)
        run_performance_test(
            days=args.days,
            refresh=args.refresh,
            use_max=args.max,
            vti_core_pct=vti_core,
            paper_aggressive=args.paper_aggressive,
            small_account=args.small_account,
            stat_arb_report=args.stat_arb_report,
            paper_crypto_enabled=(
                True if args.paper_crypto
                else False if args.paper_aggressive
                else None
            ),
            regime_filter=regime_filter,
            regime_breakdown=args.regime_breakdown,
            deep_history=args.deep_history,
            deep_history_indicators_only=args.deep_history_indicators_only,
            max_years=max(1, int(args.max_years)),
        )
        if args.paper_aggressive:
            print(f"Paper sleeve flags: {config.get_paper_feature_flags()}")
