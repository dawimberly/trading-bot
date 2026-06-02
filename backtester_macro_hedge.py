"""Macro hedge backtest — bond/yield stress vs long-only fund (2017–2023 grid).

Compares responses to Japan-style / rising-yield / K-shaped concerns:

  baseline      — current fund (SPY + crypto + NYSE), no macro overlay
  dynamic_cash  — raise cash to 25% on stress (SPY below MA200, TLT weak, bear regime)
  gld_hedge     — 10% sleeve buys GLD on stress, exits on calm
  sh_smart      — 10% sleeve buys SH when SPY below MA200 + RHYME_E (not panic vol)
  yield_gate    — block new SPY buys when 10Y yield (TNX) above MA50 and rising
  macro_combo   — 90% long + dynamic cash + 5% GLD + 5% SH smart

Run:
  python backtester_macro_hedge.py
  python backtester_macro_hedge.py --from 2017 --to 2023
  python backtester_macro_hedge.py --from 2022 --to 2022
"""

from __future__ import annotations

import argparse
import sqlite3
import warnings

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
from backtester_wisdom import _slice_data
from modules.market_context import get_market_regime, get_price_sentiment, get_volatility
from modules.pipeline_strategies import (
    PAUSED_REGIMES,
    run_crypto_strategy,
    run_equity_strategy,
    run_spy_strategy,
    _spy_market_up_signal,
)
from modules.risk_management import RiskManager

warnings.filterwarnings("ignore", category=RuntimeWarning)

MACRO_YF = {"GLD": "GLD", "SH": "SH", "TLT": "TLT", "^TNX": "TNX"}
STRATEGIES = (
    "baseline",
    "dynamic_cash",
    "gld_hedge",
    "sh_smart",
    "yield_gate",
    "macro_combo",
    "game_plan",
)
GAME_PLAN_STRATEGIES = ("baseline", "game_plan")
NORMAL_CASH_PCT = config.effective_cash_buffer_pct()
STRESS_CASH_PCT = 0.25
BEAR_REGIME = "RHYME_E: Steady_Bearish_Decline"
PANIC_REGIME = "RHYME_B: Panic_Volatility"


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    close_col = next((c for c in df.columns if str(c).lower() == "close"), None)
    if close_col is None:
        return pd.DataFrame()
    out = df[[close_col]].copy()
    out.columns = ["Close"]
    out.index.name = "Date"
    return out.reset_index()


def fetch_macro_daily(refresh: bool = False) -> None:
    """Ensure GLD, SH, TLT, TNX daily tables exist in market_data.db."""
    conn = sqlite3.connect(config.DB_PATH)
    for yf_ticker, col in MACRO_YF.items():
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
                print(f"No macro data for {yf_ticker}")
                continue
            df.to_sql(table, conn, if_exists="replace", index=False)
            print(f"Stored: {table} ({len(df)} rows)")
        except Exception as e:
            print(f"Failed macro {yf_ticker}: {e}")
    conn.close()


def _load_macro_column(col: str) -> pd.Series:
    conn = sqlite3.connect(config.DB_PATH)
    table = f"{col}_daily"
    df = pd.read_sql(f"SELECT * FROM '{table}'", conn)
    conn.close()
    target = next((c for c in df.columns if "close" in c.lower()), None)
    if target is None:
        return pd.Series(dtype=float)
    s = pd.to_numeric(df.set_index("Date")[target], errors="coerce")
    s.index = pd.to_datetime(s.index, errors="coerce")
    return s.sort_index()


def load_fund_with_macro(refresh: bool = False) -> pd.DataFrame:
    fetch_macro_daily(refresh=refresh)
    data = _ensure_daily_data(0, refresh=refresh, use_max=True)
    for col in MACRO_YF.values():
        series = _load_macro_column(col)
        if not series.empty:
            data[col] = series.reindex(data.index).ffill()
    return data


def fund_columns(data: pd.DataFrame) -> list[str]:
    return [c for c in data.columns if c in config.UNIVERSE]


def _bond_stress(window: pd.DataFrame) -> bool:
    if "TLT" not in window.columns:
        return False
    tlt = window["TLT"].dropna()
    if len(tlt) < 50:
        return False
    return float(tlt.iloc[-1]) < float(tlt.rolling(50).mean().iloc[-1])


def _yield_gate(window: pd.DataFrame) -> bool:
    """True when rising-rate environment — block new SPY longs."""
    if "TNX" in window.columns:
        y = window["TNX"].dropna()
        if len(y) >= 50:
            ma50 = float(y.rolling(50).mean().iloc[-1])
            return float(y.iloc[-1]) > ma50 and float(y.iloc[-1]) > float(y.iloc[-6])
    return _bond_stress(window)


def macro_stress(window: pd.DataFrame, regime: str) -> bool:
    bullish, _ = _spy_market_up_signal(window, config.SPY_BOT_SYMBOL, config.SPY_MA_WINDOW)
    if not bullish:
        return True
    if regime in (BEAR_REGIME, PANIC_REGIME):
        return True
    return _bond_stress(window)


class HedgeSleevePortfolio:
    """Small dedicated book for one hedge symbol (GLD or SH)."""

    def __init__(self, initial_capital: float):
        self.cash = initial_capital
        self.initial_capital = initial_capital
        self.positions: dict[str, float] = {}

    def equity(self, prices) -> float:
        total = self.cash
        for symbol, qty in self.positions.items():
            p = prices.get(symbol) if hasattr(prices, "get") else prices[symbol]
            if p is not None and np.isfinite(p):
                total += qty * p
        return total

    def deploy(self, symbol: str, price: float, target_pct: float = 0.90):
        eq = self.equity({symbol: price})
        if eq <= 0 or price <= 0:
            return 0
        current = self.positions.get(symbol, 0.0) * price
        target = eq * target_pct
        if current >= target * 0.95:
            return 0
        buy_notional = min(target - current, self.cash * 0.98)
        buy_notional = max(config.MIN_NOTIONAL, round(buy_notional, 2))
        if buy_notional < config.MIN_NOTIONAL or buy_notional > self.cash:
            return 0
        qty = buy_notional / price
        self.cash -= buy_notional * (1 + TX_COST)
        self.positions[symbol] = self.positions.get(symbol, 0.0) + qty
        return 1

    def exit_all(self, symbol: str, price: float):
        qty = self.positions.get(symbol, 0.0)
        if qty <= 0 or price <= 0:
            return 0
        proceeds = qty * price * (1 - TX_COST)
        self.cash += proceeds
        del self.positions[symbol]
        return 1


def _trim_to_cash_target(portfolio: BacktestPortfolio, prices, target_pct: float) -> int:
    """Sell holdings until cash >= target_pct of equity (proportional trim)."""
    eq = portfolio.equity(prices)
    if eq <= 0:
        return 0
    target_cash = eq * target_pct
    if portfolio.cash >= target_cash:
        return 0
    need = target_cash - portfolio.cash
    sells = 0
    for symbol in list(portfolio.positions.keys()):
        if need <= 0:
            break
        qty = portfolio.positions.get(symbol, 0)
        price = prices.get(symbol)
        if qty <= 0 or price is None or price <= 0:
            continue
        pos_val = qty * price
        sell_val = min(pos_val, need / (1 - TX_COST))
        sell_qty = sell_val / price
        proceeds = sell_val * (1 - TX_COST)
        portfolio.cash += proceeds
        portfolio.positions[symbol] = qty - sell_qty
        if portfolio.positions[symbol] < 1e-9:
            del portfolio.positions[symbol]
        need -= proceeds
        sells += 1
    return sells


def _sh_enter(regime: str, window: pd.DataFrame) -> bool:
    """Bear hedge: SPY below MA200 and bonds/yields stressed; skip euphoric rallies."""
    bullish, _ = _spy_market_up_signal(window, config.SPY_BOT_SYMBOL, config.SPY_MA_WINDOW)
    if bullish or regime in ("RHYME_A: Euphoric_Volatility", PANIC_REGIME):
        return False
    return _bond_stress(window) or not bullish


def _sh_exit(regime: str, window: pd.DataFrame) -> bool:
    bullish, _ = _spy_market_up_signal(window, config.SPY_BOT_SYMBOL, config.SPY_MA_WINDOW)
    return bullish or regime in ("RHYME_C: Steady_Bullish_Growth", "RHYME_A: Euphoric_Volatility")


def run_macro_backtest(
    data: pd.DataFrame,
    strategy: str,
    *,
    initial_capital: float = 10_000.0,
) -> dict:
    if len(data) < MIN_HISTORY:
        raise ValueError(f"Need {MIN_HISTORY}+ rows; got {len(data)}")

    cols = fund_columns(data)
    if config.SPY_BOT_SYMBOL not in cols:
        raise ValueError("SPY missing from fund data")

    long_pct = 1.0
    gld_book = sh_book = None
    if strategy == "gld_hedge":
        long_pct = 0.90
        gld_book = HedgeSleevePortfolio(initial_capital * 0.10)
    elif strategy == "sh_smart":
        long_pct = 0.90
        sh_book = HedgeSleevePortfolio(initial_capital * 0.10)
    elif strategy == "macro_combo":
        long_pct = 0.90
        gld_book = HedgeSleevePortfolio(initial_capital * 0.05)
        sh_book = HedgeSleevePortfolio(initial_capital * 0.05)
    elif strategy == "game_plan":
        # Live target: 90% long fund + yield gate + 10% GLD on stress + cash trim on stress
        long_pct = 0.90
        gld_book = HedgeSleevePortfolio(initial_capital * 0.10)

    portfolio = BacktestPortfolio(initial_capital * long_pct)
    pair_cooldown: dict = {}
    risk_manager = RiskManager(max_drawdown_pct=config.MAX_DRAWDOWN_PCT)
    equity_curve = []
    long_curve = []
    hedge_curve = []
    stress_days = 0
    yield_gate_days = 0
    cash_trims = 0
    gld_trades = sh_trades = 0
    halted = False
    start_i = MIN_HISTORY

    for i in range(start_i, len(data)):
        window_full = data.iloc[: i + 1]
        window = window_full[cols]
        prices = window.iloc[-1]
        prices_full = window_full.iloc[-1]

        long_eq = portfolio.equity(prices)
        hedge_eq = 0.0
        if gld_book:
            hedge_eq += gld_book.equity(prices_full)
        if sh_book:
            hedge_eq += sh_book.equity(prices_full)
        combined = long_eq + hedge_eq
        equity_curve.append(combined)
        long_curve.append(long_eq)
        hedge_curve.append(hedge_eq)

        if halted or not risk_manager.check_drawdown(combined):
            if not halted:
                halted = True
            continue

        sentiment = get_price_sentiment(window)
        vol = get_volatility(window)
        regime = get_market_regime(sentiment, vol)
        stress = macro_stress(window_full, regime)
        if stress:
            stress_days += 1

        executor = BacktestExecutor(portfolio, prices)

        use_yield_gate = strategy in ("yield_gate", "game_plan")
        if not use_yield_gate or not _yield_gate(window_full):
            run_spy_strategy(
                window,
                executor,
                regime,
                i,
                pair_cooldown,
                cooldown_bars=DAILY_COOLDOWN_BARS,
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
            window,
            executor,
            regime,
            i,
            pair_cooldown,
            cooldown_bars=DAILY_COOLDOWN_BARS,
        )

        if strategy in ("dynamic_cash", "macro_combo", "game_plan") and stress:
            cash_trims += _trim_to_cash_target(portfolio, prices, STRESS_CASH_PCT)

        if gld_book and "GLD" in prices_full.index and np.isfinite(prices_full["GLD"]):
            gld_px = float(prices_full["GLD"])
            if stress:
                gld_trades += gld_book.deploy("GLD", gld_px)
            else:
                gld_trades += gld_book.exit_all("GLD", gld_px)

        if sh_book and "SH" in prices_full.index and np.isfinite(prices_full["SH"]):
            sh_px = float(prices_full["SH"])
            if _sh_enter(regime, window_full):
                sh_trades += sh_book.deploy("SH", sh_px)
            elif _sh_exit(regime, window_full):
                sh_trades += sh_book.exit_all("SH", sh_px)

    curve = pd.Series(equity_curve, index=data.index[start_i:])
    long_s = pd.Series(long_curve, index=data.index[start_i:])
    hedge_s = pd.Series(hedge_curve, index=data.index[start_i:])
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
        "long_final": round(long_s.iloc[-1], 2),
        "hedge_final": round(hedge_s.iloc[-1], 2),
        "total_return_pct": round(total_ret, 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "benchmark_pct": round(bench, 2) if bench is not None else None,
        "stress_days": stress_days,
        "yield_gate_days": yield_gate_days,
        "cash_trims": cash_trims,
        "gld_trades": gld_trades,
        "sh_trades": sh_trades,
        "halted": halted,
        "start": data.index[start_i].date(),
        "end": data.index[-1].date(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Macro hedge fund backtest comparison")
    parser.add_argument("--from", dest="year_from", type=int, default=2017)
    parser.add_argument("--to", dest="year_to", type=int, default=2023)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--capital", type=float, default=10_000.0)
    parser.add_argument(
        "--game-plan",
        action="store_true",
        help="Compare baseline vs game_plan only (agreed live target)",
    )
    args = parser.parse_args()

    strategies = GAME_PLAN_STRATEGIES if args.game_plan else STRATEGIES

    print("=== MACRO HEDGE BACKTEST ===")
    print(f"Window: {args.year_from} -> {args.year_to}")
    print("Stress = SPY below MA200 OR TLT below MA50 OR bear/panic regime")
    print(f"Dynamic cash: {NORMAL_CASH_PCT:.0%} calm / {STRESS_CASH_PCT:.0%} stress")
    print("GLD sleeve: 10% | SH smart: 10% (SPY below MA200 + bond stress, skip panic)")
    print("Yield gate: block SPY buys when 10Y yield above MA50 and rising")
    if args.game_plan:
        print(
            "game_plan: 90% long + yield gate + 10% GLD on stress + 25% cash on stress"
        )

    data = load_fund_with_macro(refresh=args.refresh)
    data = _slice_data(data, args.year_from, args.year_to)
    print(
        f"Daily bars: {len(data)} "
        f"({data.index.min().date()} -> {data.index.max().date()})"
    )
    macro_ok = [c for c in ("GLD", "SH", "TLT", "TNX") if c in data.columns]
    print(f"Macro series: {', '.join(macro_ok) or 'none'}")

    results = []
    for name in strategies:
        print(f"\n--- {name} ---")
        row = run_macro_backtest(data, name, initial_capital=args.capital)
        results.append(row)
        print(
            f"  return {row['total_return_pct']:+7.2f}%  Sharpe {row['sharpe']:5.2f}  "
            f"max DD {row['max_drawdown_pct']:6.2f}%  equity ${row['final_equity']:,.0f}  "
            f"(long ${row['long_final']:,.0f} + hedge ${row['hedge_final']:,.0f})"
        )
        if name in ("dynamic_cash", "macro_combo", "game_plan"):
            print(f"    cash trims: {row['cash_trims']}  stress days: {row['stress_days']}")
        if name in ("yield_gate", "game_plan"):
            print(f"    SPY gated days: {row['yield_gate_days']}")
        if name in ("gld_hedge", "macro_combo", "game_plan"):
            print(f"    GLD sleeve trades: {row['gld_trades']}")
        if name in ("sh_smart", "macro_combo"):
            print(f"    SH sleeve trades: {row['sh_trades']}")

    print("\n=== COMPARISON ===")
    header = f"{'Strategy':<14} {'Return':>9} {'Sharpe':>7} {'MaxDD':>8} {'Hedge$':>8}"
    print(header)
    print("-" * len(header))
    for row in sorted(results, key=lambda r: -r["sharpe"]):
        print(
            f"{row['strategy']:<14} {row['total_return_pct']:+8.2f}% "
            f"{row['sharpe']:7.2f} {row['max_drawdown_pct']:7.2f}% "
            f"{row['hedge_final']:8.0f}"
        )
    bench = results[0].get("benchmark_pct")
    if bench is not None:
        print(f"\n{BENCHMARK} buy & hold: {bench:+.2f}%")

    best_ret = max(results, key=lambda r: r["total_return_pct"])
    best_sh = max(results, key=lambda r: r["sharpe"])
    best_dd = max(results, key=lambda r: r["max_drawdown_pct"])
    print(
        f"\nBest return: {best_ret['strategy']} ({best_ret['total_return_pct']:+.2f}%)"
    )
    print(f"Best Sharpe: {best_sh['strategy']} ({best_sh['sharpe']:.2f})")
    print(f"Smallest max DD: {best_dd['strategy']} ({best_dd['max_drawdown_pct']:.2f}%)")

    out_path = "fund_macro_hedge_backtest_results.csv"
    pd.DataFrame(results).to_csv(out_path, index=False)
    print(f"\nSaved -> {out_path}")


if __name__ == "__main__":
    main()
