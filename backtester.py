"""Backtest that mirrors run_all.py (regime + crypto pairs + equity MA50).

Default: 365-day simulation on daily bars (fetch if missing).
Live bot still uses 5m data via fetch_data.py without --daily.

Run:  python backtester.py
       python backtester.py --days 180
       python backtester.py --days 365 --small-account
       python backtester.py --days 365 --small-account --compare-vti-core
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
    run_spy_exits,
    run_spy_strategy,
)
from modules.pipeline_strategies import regime_entries_paused
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

    def _get_account(self):
        """Alpaca-compatible shim for pipeline_strategies (live uses real account)."""
        equity = self.portfolio.equity(self.prices)

        class _Acct:
            pass

        acct = _Acct()
        acct.equity = equity
        acct.cash = self.portfolio.cash
        return acct

    def begin_deployment_cycle(self):
        self._cofire_notionals = {}
        self._sizing_data = None
        self._paper_feature_flags = config.get_paper_feature_flags()

    def set_sizing_context(self, data=None):
        self._sizing_data = data

    def set_wisdom_sizing_multiplier(self, multiplier: float = 1.0) -> None:
        self._wisdom_sizing_multiplier = float(multiplier)

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

    def _scaled_cap_pct(self, sleeve_cap_pct: float) -> float:
        return round(sleeve_cap_pct * self._cap_scale, 6)

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

    def compute_notional(self):
        equity = self.portfolio.equity(self.prices)
        cash = self.portfolio.cash
        if config.backtest_small_account_context():
            risk = config.effective_risk_per_trade(equity)
            max_order = config.effective_max_notional_per_order(equity)
            min_n = config.effective_min_notional(equity)
        else:
            risk = config.RISK_PER_TRADE
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
        return self._compute_capped_notional(
            config.NYSE_SLEEVE_CAP_PCT,
            self.nyse_sleeve_value(),
            "nyse",
        )

    def compute_spy_notional(self):
        base = self._compute_capped_notional(
            config.SPY_SLEEVE_CAP_PCT,
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

    def equity(self, prices):
        total = self.cash
        for symbol, qty in self.positions.items():
            p = prices.get(symbol)
            if p is not None and np.isfinite(p):
                total += qty * p
        return total

    def trade(self, symbol, side, price, tx_cost=TX_COST, notional=None):
        if notional is None:
            equity = self.equity({symbol: price})
            if config.backtest_small_account_context():
                risk = config.effective_risk_per_trade(equity)
                max_order = config.effective_max_notional_per_order(equity)
                min_n = config.effective_min_notional(equity)
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
    if use_max:
        if not refresh:
            data = load_close_matrix(interval="1d")
            if len(data) >= MIN_HISTORY + 10:
                return data
        print("--- Downloading max daily history (may take a few minutes) ---")
        fetch_daily_history(use_max=True)
        return load_close_matrix(interval="1d")

    sim_days = days or config.BACKTEST_DAYS
    need_rows = _min_rows_for_backtest(sim_days)
    if not refresh:
        data = load_close_matrix(interval="1d")
        if len(data) >= need_rows:
            if len(data) > need_rows:
                data = data.iloc[-need_rows:]
            return data

    fetch_days = _calendar_days_to_fetch(sim_days)
    print(
        f"--- Downloading {fetch_days} calendar days of daily history "
        f"({MIN_HISTORY}-bar SPY MA warmup + ~{sim_days} sim days) ---"
    )
    fetch_daily_history(fetch_days)
    data = load_close_matrix(interval="1d")
    if len(data) > need_rows:
        data = data.iloc[-need_rows:]
    if len(data) < MIN_HISTORY:
        print(
            f"--- Still short ({len(data)} rows); downloading max daily history ---"
        )
        fetch_daily_history(use_max=True)
        data = load_close_matrix(interval="1d")
    return data


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
        return config.PAPER_VTI_CORE_PCT
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
    track_active_exposure: bool = False,
):
    """Run fund pipeline on daily data; return performance + optional SPY fill metrics."""
    saved_paper_ctx = config.paper_aggressive_context()
    saved_small_ctx = config.backtest_small_account_context()
    saved_social = config.SOCIAL_SLEEVE_ENABLED
    saved_dynamic_vti = config.PAPER_DYNAMIC_VTI_ENABLED
    saved_paper_sleeve_flags = config.snapshot_paper_sleeve_flags()
    saved_macro_overrides = config.SOCIAL_MACRO_OVERRIDES_ENABLED
    saved_macro_boost = config.PAPER_SOCIAL_MACRO_BOOST_ENABLED
    saved_paper_macro = config.PAPER_MACRO_REGIME_ADAPTOR_ENABLED
    config.set_paper_aggressive_context(paper_aggressive)
    config.set_backtest_small_account_context(small_account)
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

    fixed_vti_core_pct = vti_core_pct
    if paper_aggressive and not config.PAPER_DYNAMIC_VTI_ENABLED:
        fixed_vti_core_pct = config.PAPER_VTI_CORE_PCT

    start_date = data.index[MIN_HISTORY]
    end_date = data.index[-1]
    cooldown_bars = DAILY_COOLDOWN_BARS
    sharpe_scale = np.sqrt(252)
    sim_days = (end_date - start_date).days
    cap_scale = _backtest_cap_scale(fixed_vti_core_pct, paper_aggressive=paper_aggressive)

    initial_capital = _small_account_start_equity() if small_account else 10000.0
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
        vti_core_pct = _resolve_backtest_vti_pct(
            eq,
            vol_score=vol_score,
            volatility=vol,
            macro_stress_flag=macro_stress_flag,
            paper_aggressive=paper_aggressive,
            fixed_vti_core_pct=fixed_vti_core_pct,
        )
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
                regime_scale = min(
                    macro_regime.get("spy_scale", 1.0),
                    macro_regime.get("nyse_scale", 1.0),
                )
                cap_scale = round(cap_scale * regime_scale, 6)
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
        total_crypto += run_crypto_strategy(
            window,
            executor,
            regime,
            i,
            pair_cooldown,
            cooldown_bars=cooldown_bars,
            volatility=vol,
        )
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
        total_equity += run_equity_strategy(
            window,
            executor,
            regime,
            i,
            pair_cooldown,
            cooldown_bars=cooldown_bars,
            yield_gated=yield_gated,
        )
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

    curve = pd.Series(equity_curve)
    returns = curve.pct_change().dropna()
    total_ret = (curve.iloc[-1] / portfolio.initial_capital - 1) * 100
    sharpe = (
        (returns.mean() / returns.std()) * sharpe_scale if returns.std() != 0 else 0
    )
    downside = returns[returns < 0]
    sortino = (
        (returns.mean() / downside.std()) * sharpe_scale
        if len(downside) > 0 and downside.std() != 0
        else 0
    )
    max_dd = ((curve / curve.cummax()) - 1).min() * 100
    calmar = (total_ret / abs(max_dd)) if max_dd != 0 else 0.0
    bench = _benchmark_return(data, MIN_HISTORY)

    result = {
        "start_date": str(start_date.date()),
        "end_date": str(end_date.date()),
        "sim_days": sim_days,
        "final_equity": round(curve.iloc[-1], 2),
        "total_return_pct": round(total_ret, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "calmar": round(calmar, 2),
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
        "vti_core_pct": round(float(np.mean(vti_core_samples)), 4)
        if vti_core_samples
        else fixed_vti_core_pct,
        "avg_active_exposure_pct": round(float(np.mean(active_exposure_samples)) * 100, 2)
        if active_exposure_samples
        else round((1.0 - fixed_vti_core_pct) * 100, 2),
        "paper_aggressive": paper_aggressive,
        "paper_dynamic_vti": config.PAPER_DYNAMIC_VTI_ENABLED if paper_aggressive else False,
        "paper_sleeve_features": config.get_paper_feature_flags() if paper_aggressive else {},
        "cofire_pct": round(100 * cofire_days / trade_days, 1) if trade_days else 0.0,
        "cofire_days": cofire_days,
        "small_account": small_account,
        "cap_scale": cap_scale,
        "equity_index": [data.index[i].isoformat() for i in range(MIN_HISTORY, len(data))],
        "equity_values": [round(v, 2) for v in equity_curve],
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
    config.set_paper_aggressive_context(saved_paper_ctx)
    config.set_backtest_small_account_context(saved_small_ctx)
    config.SOCIAL_SLEEVE_ENABLED = saved_social
    config.PAPER_DYNAMIC_VTI_ENABLED = saved_dynamic_vti
    config.apply_paper_sleeve_flags(saved_paper_sleeve_flags)
    config.SOCIAL_MACRO_OVERRIDES_ENABLED = saved_macro_overrides
    config.PAPER_SOCIAL_MACRO_BOOST_ENABLED = saved_macro_boost
    config.PAPER_MACRO_REGIME_ADAPTOR_ENABLED = saved_paper_macro
    return result


def run_macro_regime_compare(days=None, refresh=False, use_max=False) -> None:
    """Compare paper aggressive with vs without Macro Regime Adaptor."""
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
            "Paper (no macro adaptor)",
            {
                "paper_aggressive": True,
                "paper_sleeve_features": True,
                "paper_dynamic_vti": True,
                "paper_macro_regime": False,
            },
        ),
        (
            "Paper (+ Macro Regime Adaptor)",
            {
                "paper_aggressive": True,
                "paper_sleeve_features": True,
                "paper_dynamic_vti": True,
                "paper_macro_regime": True,
            },
        ),
    ]
    print("--- MACRO REGIME ADAPTOR A/B (Felix/Social off, dynamic VTI + sleeve flags) ---")
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
        help="Compare paper aggressive with vs without Macro Regime Adaptor",
    )
    args = parser.parse_args()
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
    elif args.compare_macro_regime:
        run_macro_regime_compare(
            days=args.days, refresh=args.refresh, use_max=args.max
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
