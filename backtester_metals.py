"""Metal hedge sleeve backtest — GLD, SLV, copper, uranium, etc. on macro stress.

Each variant: 90% long fund + 10% metal sleeve (buy on stress, exit on calm).
Optional: game_plan metals = yield gate + stress cash + metal sleeve.

Run:
  python backtester_metals.py
  python backtester_metals.py --from 2017 --to 2023
  python backtester_metals.py --from 2022 --to 2022 --refresh
"""

from __future__ import annotations

import argparse
import sqlite3
import warnings
from contextlib import contextmanager

import numpy as np
import pandas as pd
import yfinance as yf

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
from backtester_macro_hedge import (
    STRESS_CASH_PCT,
    HedgeSleevePortfolio,
    _bond_stress,
    _load_macro_column,
    _normalize_df,
    _trim_to_cash_target,
    _yield_gate,
    fund_columns,
    macro_stress,
)
from backtester_wisdom import _slice_data
from modules.wayback_sentiment import load_monthly_web_sentiment
from modules.wisdom_sentiment import resolve_backtest_regime
from modules.market_context import get_market_regime, get_price_sentiment, get_volatility
from modules.pipeline_strategies import run_crypto_strategy, run_equity_strategy, run_spy_strategy
from modules.risk_management import RiskManager

warnings.filterwarnings("ignore", category=RuntimeWarning)

# yfinance ticker -> column name in DB (must match config.METAL_SYMBOLS)
METAL_YF = {sym: sym for sym in sorted(config.METAL_SYMBOLS)}
MACRO_BOND = {"SH": "SH", "TLT": "TLT", "^TNX": "TNX"}

# strategy name -> {symbol: weight within metal sleeve (sums to 1.0)}
METAL_STRATEGIES: dict[str, dict[str, float] | None] = {
    "baseline": None,
    "yield_gate_only": None,  # yield gate + full long caps; no metal / stress cash
    "gld_only": {"GLD": 1.0},
    "slv_only": {"SLV": 1.0},
    "gld_slv": {"GLD": 0.5, "SLV": 0.5},
    "gld_slv_cper": {"GLD": 0.50, "SLV": 0.30, "CPER": 0.20},
    "precious": {"GLD": 0.6, "SLV": 0.3, "PPLT": 0.1},
    "industrial": {"CPER": 0.5, "URA": 0.5},
    "full_basket": {"GLD": 0.35, "SLV": 0.25, "CPER": 0.20, "URA": 0.15, "DBB": 0.05},
    "miners": {"GDX": 1.0},
    "game_plan_gld": {"GLD": 1.0},  # + yield gate + cash trim
    "game_plan_gld_slv_cper": {"GLD": 0.50, "SLV": 0.30, "CPER": 0.20},
    "game_plan_basket": {"GLD": 0.35, "SLV": 0.25, "CPER": 0.20, "URA": 0.15, "DBB": 0.05},
}


def _validate_metal_strategies() -> None:
    universe = frozenset(config.UNIVERSE)
    for name, weights in METAL_STRATEGIES.items():
        if weights is None:
            continue
        config.validate_metal_weights(weights, strategy=name)
        missing_univ = sorted(set(weights) - universe)
        if missing_univ:
            raise ValueError(
                f"Metal strategy '{name}' references {missing_univ} not in UNIVERSE"
            )


_validate_metal_strategies()

SLEEVE_PCT = 0.10
LONG_PCT = 1.0 - SLEEVE_PCT

# Maps backtest strategy -> config flags for sleeve caps and game-plan features
_STRATEGY_CONFIG_MODE: dict[str, str] = {
    "baseline": "baseline",
    "yield_gate_only": "yield_gate_only",
}


@contextmanager
def _game_plan_config(mode: str):
    """Temporarily set config flags: baseline | full | yield_gate_only."""
    orig_enabled = config.GAME_PLAN_ENABLED
    orig_ygo = config.GAME_PLAN_YIELD_GATE_ONLY
    try:
        if mode == "baseline":
            config.GAME_PLAN_ENABLED = False
            config.GAME_PLAN_YIELD_GATE_ONLY = False
        elif mode == "full":
            config.GAME_PLAN_ENABLED = True
            config.GAME_PLAN_YIELD_GATE_ONLY = False
        elif mode == "yield_gate_only":
            config.GAME_PLAN_ENABLED = False
            config.GAME_PLAN_YIELD_GATE_ONLY = True
        else:
            raise ValueError(f"Unknown game plan config mode: {mode}")
        yield
    finally:
        config.GAME_PLAN_ENABLED = orig_enabled
        config.GAME_PLAN_YIELD_GATE_ONLY = orig_ygo


def _config_mode_for_strategy(strategy: str) -> str:
    if strategy in _STRATEGY_CONFIG_MODE:
        return _STRATEGY_CONFIG_MODE[strategy]
    if strategy.startswith("game_plan_"):
        return "full"
    return "baseline"


def fetch_metals_daily(refresh: bool = False) -> None:
    conn = sqlite3.connect(config.DB_PATH)
    for yf_ticker, col in {**METAL_YF, **MACRO_BOND}.items():
        table = f"{col}_daily"
        if not refresh:
            try:
                n = pd.read_sql(f"SELECT COUNT(*) AS n FROM '{table}'", conn).iloc[0, 0]
                if n >= MIN_HISTORY + 10:
                    continue
            except Exception:
                pass
        try:
            df = yf.download(yf_ticker, period="max", interval="1d", progress=False, auto_adjust=True)
            df = _normalize_df(df)
            if df.empty:
                print(f"No data: {yf_ticker}")
                continue
            df.to_sql(table, conn, if_exists="replace", index=False)
            print(f"Stored: {table} ({len(df)} rows)")
        except Exception as e:
            print(f"Failed {yf_ticker}: {e}")
    conn.close()


def load_fund_with_metals(refresh: bool = False) -> pd.DataFrame:
    fetch_metals_daily(refresh=refresh)
    data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    for col in {**METAL_YF, **MACRO_BOND}.values():
        series = _load_macro_column(col)
        if not series.empty:
            data[col] = series.reindex(data.index).ffill()
    return data


def _metal_bnh_return(data: pd.DataFrame, symbol: str, start_i: int) -> float | None:
    if symbol not in data.columns:
        return None
    col = data[symbol].iloc[start_i:].dropna()
    if len(col) < 2 or col.iloc[0] <= 0:
        return None
    return (col.iloc[-1] / col.iloc[0] - 1) * 100


def _deploy_metal_basket(book: HedgeSleevePortfolio, weights: dict[str, float], prices_full) -> int:
    """Deploy using exact strategy weights — never re-normalize when a symbol is skipped."""
    config.validate_metal_weights(weights, strategy="deploy")

    missing_cols = sorted(s for s in weights if s not in prices_full.index)
    if missing_cols:
        raise ValueError(
            f"Metal deploy missing price columns {missing_cols}. "
            f"Run with --refresh or fetch_metals_daily(); weights are not re-normalized."
        )

    trades = 0
    px_map: dict[str, float] = {}
    for symbol in weights:
        px = float(prices_full[symbol])
        if np.isfinite(px) and px > 0:
            px_map[symbol] = px

    if not px_map:
        return 0

    eq = book.equity(px_map)
    if eq <= 0:
        return 0

    for symbol, w in weights.items():
        if symbol not in px_map:
            continue
        px = px_map[symbol]
        current = book.positions.get(symbol, 0.0) * px
        target = eq * w * 0.90
        if current >= target * 0.95:
            continue
        buy_notional = max(config.MIN_NOTIONAL, round(min(target - current, book.cash * 0.98), 2))
        if buy_notional < config.MIN_NOTIONAL or buy_notional > book.cash:
            continue
        qty = buy_notional / px
        book.cash -= buy_notional * (1 + TX_COST)
        book.positions[symbol] = book.positions.get(symbol, 0.0) + qty
        trades += 1
    return trades


def _exit_metal_basket(book: HedgeSleevePortfolio, weights: dict[str, float], prices_full) -> int:
    trades = 0
    for symbol in weights:
        if symbol not in prices_full.index:
            continue
        px = float(prices_full[symbol])
        if np.isfinite(px) and px > 0:
            trades += book.exit_all(symbol, px)
    return trades


def run_metals_backtest(
    data: pd.DataFrame,
    strategy: str,
    *,
    initial_capital: float = 10_000.0,
    wisdom_mode: str | None = None,
    monthly_web: pd.Series | None = None,
    gap_threshold: float | None = None,
) -> dict:
    weights = METAL_STRATEGIES[strategy]
    if weights is not None:
        config.validate_metal_weights(weights, strategy=strategy)
        config.validate_metal_weights(
            weights, available=frozenset(data.columns), strategy=strategy
        )
    cols = fund_columns(data)
    game_plan = strategy.startswith("game_plan_")
    use_yield_gate = strategy in ("yield_gate", "yield_gate_only") or game_plan
    config_mode = _config_mode_for_strategy(strategy)

    with _game_plan_config(config_mode):
        metal_book = None
        long_pct = 1.0
        if weights is not None:
            long_pct = LONG_PCT
            metal_book = HedgeSleevePortfolio(initial_capital * SLEEVE_PCT)

        portfolio = BacktestPortfolio(initial_capital * long_pct)
        pair_cooldown: dict = {}
        risk_manager = RiskManager(max_drawdown_pct=config.MAX_DRAWDOWN_PCT)
        equity_curve = []
        metal_trades = 0
        yield_gate_days = 0
        cash_trims = 0
        wisdom_pause_days = 0
        halted = False
        start_i = MIN_HISTORY

        for i in range(start_i, len(data)):
            window_full = data.iloc[: i + 1]
            window = window_full[cols]
            prices = window.iloc[-1]
            prices_full = window_full.iloc[-1]

            long_eq = portfolio.equity(prices)
            metal_eq = metal_book.equity(prices_full) if metal_book else 0.0
            combined = long_eq + metal_eq
            equity_curve.append(combined)

            if halted or not risk_manager.check_drawdown(combined):
                if not halted:
                    halted = True
                continue

            ts = data.index[i]
            regime, vol, paused, sizing_mult = resolve_backtest_regime(
                window,
                ts,
                monthly_web,
                wisdom_mode=wisdom_mode,
                gap_threshold=gap_threshold,
            )
            if paused:
                wisdom_pause_days += 1
            stress = macro_stress(window_full, regime)

            executor = BacktestExecutor(portfolio, prices)
            executor.set_wisdom_sizing_multiplier(sizing_mult)
            gated = use_yield_gate and _yield_gate(window_full)
            if not gated:
                run_spy_strategy(
                    window, executor, regime, i, pair_cooldown, cooldown_bars=DAILY_COOLDOWN_BARS
                )
            else:
                yield_gate_days += 1

            run_crypto_strategy(
                window, executor, regime, i, pair_cooldown,
                cooldown_bars=DAILY_COOLDOWN_BARS, volatility=vol,
            )
            run_equity_strategy(
                window, executor, regime, i, pair_cooldown, cooldown_bars=DAILY_COOLDOWN_BARS
            )

            if game_plan and stress:
                cash_trims += _trim_to_cash_target(portfolio, prices, STRESS_CASH_PCT)

            if metal_book and weights:
                if stress:
                    metal_trades += _deploy_metal_basket(metal_book, weights, prices_full)
                else:
                    metal_trades += _exit_metal_basket(metal_book, weights, prices_full)

        curve = pd.Series(equity_curve, index=data.index[start_i:])
        returns = curve.pct_change().dropna()
        total_ret = (curve.iloc[-1] / initial_capital - 1) * 100
        sharpe = (
            (returns.mean() / returns.std()) * np.sqrt(252) if returns.std() != 0 else 0.0
        )
        max_dd = ((curve / curve.cummax()) - 1).min() * 100
        bench = _benchmark_return(data[cols], start_i)

        return {
            "strategy": strategy,
            "final_equity": round(curve.iloc[-1], 2),
            "metal_final": round(metal_book.equity(data.iloc[-1]) if metal_book else 0.0, 2),
            "total_return_pct": round(total_ret, 2),
            "sharpe": round(sharpe, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "benchmark_pct": round(bench, 2) if bench is not None else None,
            "metal_trades": metal_trades,
            "yield_gate_days": yield_gate_days,
            "cash_trims": cash_trims,
            "wisdom_pause_days": wisdom_pause_days,
            "wisdom_mode": wisdom_mode or "price_only",
            "halted": halted,
            "start": data.index[start_i].date(),
            "end": data.index[-1].date(),
        }


def _index_on_or_after(data: pd.DataFrame, date: str) -> int:
    ts = pd.Timestamp(date)
    if data.index.tz is not None:
        ts = ts.tz_localize(data.index.tz)
    for i in range(MIN_HISTORY, len(data)):
        if data.index[i] >= ts:
            return i
    raise ValueError(f"No bar on/after {date} with {MIN_HISTORY}+ rows of history")


def _index_on_or_before(data: pd.DataFrame, date: str) -> int:
    ts = pd.Timestamp(date)
    if data.index.tz is not None:
        ts = ts.tz_localize(data.index.tz)
    mask = data.index <= ts
    if not mask.any():
        raise ValueError(f"No bar on/before {date}")
    return int(np.where(mask)[0][-1])


def run_fresh_capital_backtest(
    data: pd.DataFrame,
    strategy: str,
    *,
    reset_date: str = "2022-01-01",
    end_date: str = "2022-12-31",
    initial_capital: float = 10_000.0,
    wisdom_mode: str | None = None,
    monthly_web: pd.Series | None = None,
    gap_threshold: float | None = None,
) -> dict:
    """Stress test with fresh capital at reset_date (MA warmup from prior bars only).

    Unlike run_metals_backtest(), does not carry positions, peak equity, or halt state
    from years before reset_date — intended for fair 2022-style stress reads.
    """
    weights = METAL_STRATEGIES[strategy]
    if weights is not None:
        config.validate_metal_weights(weights, strategy=strategy)
        config.validate_metal_weights(
            weights, available=frozenset(data.columns), strategy=strategy
        )
    cols = fund_columns(data)
    game_plan = strategy.startswith("game_plan_")
    use_yield_gate = strategy in ("yield_gate", "yield_gate_only") or game_plan
    config_mode = _config_mode_for_strategy(strategy)
    long_pct = LONG_PCT if weights is not None else 1.0

    start_i = _index_on_or_after(data, reset_date)
    end_i = _index_on_or_before(data, end_date)
    if end_i < start_i:
        raise ValueError(f"end_date {end_date} before reset {reset_date}")

    with _game_plan_config(config_mode):
        portfolio = BacktestPortfolio(initial_capital * long_pct)
        metal_book = (
            HedgeSleevePortfolio(initial_capital * SLEEVE_PCT) if weights is not None else None
        )
        pair_cooldown: dict = {}
        risk_manager = RiskManager(max_drawdown_pct=config.MAX_DRAWDOWN_PCT)
        equity_curve = []
        metal_trades = 0
        yield_gate_days = 0
        cash_trims = 0
        wisdom_pause_days = 0
        halted = False

        for i in range(start_i, end_i + 1):
            window_full = data.iloc[: i + 1]
            window = window_full[cols]
            prices = window.iloc[-1]
            prices_full = window_full.iloc[-1]

            long_eq = portfolio.equity(prices)
            metal_eq = metal_book.equity(prices_full) if metal_book else 0.0
            combined = long_eq + metal_eq
            equity_curve.append(combined)

            if halted or not risk_manager.check_drawdown(combined):
                if not halted:
                    halted = True
                continue

            ts = data.index[i]
            regime, vol, paused, sizing_mult = resolve_backtest_regime(
                window,
                ts,
                monthly_web,
                wisdom_mode=wisdom_mode,
                gap_threshold=gap_threshold,
            )
            if paused:
                wisdom_pause_days += 1
            stress = macro_stress(window_full, regime)

            executor = BacktestExecutor(portfolio, prices)
            executor.set_wisdom_sizing_multiplier(sizing_mult)
            gated = use_yield_gate and _yield_gate(window_full)
            if not gated:
                run_spy_strategy(
                    window, executor, regime, i, pair_cooldown, cooldown_bars=DAILY_COOLDOWN_BARS
                )
            else:
                yield_gate_days += 1

            run_crypto_strategy(
                window,
                executor,
                regime,
                i,
                pair_cooldown,
                cooldown_bars=DAILY_COOLDOWN_BARS,
                volatility=vol,
            )
            run_equity_strategy(
                window, executor, regime, i, pair_cooldown, cooldown_bars=DAILY_COOLDOWN_BARS
            )

            if game_plan and stress:
                cash_trims += _trim_to_cash_target(portfolio, prices, STRESS_CASH_PCT)

            if metal_book and weights:
                if stress:
                    metal_trades += _deploy_metal_basket(metal_book, weights, prices_full)
                else:
                    metal_trades += _exit_metal_basket(metal_book, weights, prices_full)

        curve = pd.Series(equity_curve, index=data.index[start_i : end_i + 1])
        returns = curve.pct_change().dropna()
        total_ret = (curve.iloc[-1] / initial_capital - 1) * 100
        sharpe = (
            (returns.mean() / returns.std()) * np.sqrt(252) if returns.std() != 0 else 0.0
        )
        max_dd = ((curve / curve.cummax()) - 1).min() * 100
        bench = _benchmark_return(data[cols], start_i)

        return {
            "strategy": strategy,
            "mode": "fresh_capital",
            "final_equity": round(curve.iloc[-1], 2),
            "metal_final": round(metal_book.equity(data.iloc[end_i]) if metal_book else 0.0, 2),
            "total_return_pct": round(total_ret, 2),
            "sharpe": round(sharpe, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "benchmark_pct": round(bench, 2) if bench is not None else None,
            "metal_trades": metal_trades,
            "yield_gate_days": yield_gate_days,
            "cash_trims": cash_trims,
            "wisdom_pause_days": wisdom_pause_days,
            "wisdom_mode": wisdom_mode or "price_only",
            "halted": halted,
            "start": data.index[start_i].date(),
            "end": data.index[end_i].date(),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Metal hedge sleeve backtest grid")
    parser.add_argument("--from", dest="year_from", type=int, default=2017)
    parser.add_argument("--to", dest="year_to", type=int, default=2023)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--capital", type=float, default=10_000.0)
    args = parser.parse_args()

    print("=== METAL HEDGE BACKTEST ===")
    print(f"Window: {args.year_from} -> {args.year_to}")
    print(f"Sleeve: {SLEEVE_PCT:.0%} on macro stress | long fund {LONG_PCT:.0%}")
    print("Stress = SPY below MA200 OR TLT below MA50 OR bear/panic regime\n")

    data = load_fund_with_metals(refresh=args.refresh)
    data = _slice_data(data, args.year_from, args.year_to)
    start_i = MIN_HISTORY
    print(
        f"Daily bars: {len(data)} "
        f"({data.index.min().date()} -> {data.index.max().date()})\n"
    )

    print("--- Metal buy & hold (same window, no bot) ---")
    for sym in METAL_YF.values():
        bnh = _metal_bnh_return(data, sym, start_i)
        if bnh is not None:
            print(f"  {sym:<6} buy & hold: {bnh:+.2f}%")

    results = []
    for name in METAL_STRATEGIES:
        print(f"\n--- {name} ---")
        row = run_metals_backtest(data, name, initial_capital=args.capital)
        results.append(row)
        extra = ""
        if name.startswith("game_plan"):
            extra = f"  gate {row['yield_gate_days']}d  cash trims {row['cash_trims']}"
        print(
            f"  return {row['total_return_pct']:+7.2f}%  Sharpe {row['sharpe']:5.2f}  "
            f"max DD {row['max_drawdown_pct']:6.2f}%  equity ${row['final_equity']:,.0f}  "
            f"metal sleeve ${row['metal_final']:,.0f}{extra}"
        )

    print("\n=== COMPARISON ===")
    header = f"{'Strategy':<18} {'Return':>9} {'Sharpe':>7} {'MaxDD':>8} {'Metal$':>8}"
    print(header)
    print("-" * len(header))
    for row in sorted(results, key=lambda r: -r["sharpe"]):
        print(
            f"{row['strategy']:<18} {row['total_return_pct']:+8.2f}% "
            f"{row['sharpe']:7.2f} {row['max_drawdown_pct']:7.2f}% "
            f"{row['metal_final']:8.0f}"
        )

    best = max(results, key=lambda r: r["sharpe"])
    best_ret = max(results, key=lambda r: r["total_return_pct"])
    print(f"\nBest Sharpe: {best['strategy']} ({best['sharpe']:.2f})")
    print(f"Best return: {best_ret['strategy']} ({best_ret['total_return_pct']:+.2f}%)")

    out = "fund_metals_backtest_results.csv"
    pd.DataFrame(results).to_csv(out, index=False)
    print(f"\nSaved -> {out}")


if __name__ == "__main__":
    main()
