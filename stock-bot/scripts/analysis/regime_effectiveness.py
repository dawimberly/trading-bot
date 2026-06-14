"""Regime filter and risk-halt effectiveness analysis (daily bars on market_data.db).

Compares backtest variants:
  baseline     — current config (PAUSED_REGIMES, CRYPTO_VOL_ONLY, 10% halt)
  no_pause     — entries never blocked by PAUSED_REGIMES
  no_vol_gate  — crypto allowed regardless of vol (still respects pause)
  no_halt      — drawdown halt disabled
  all_off      — no pause, no vol gate, no halt

Also computes trigger frequency, counterfactual skip value, and regime collapse stats.

Run from repo root:
  python scripts/analysis/regime_effectiveness.py
  python scripts/analysis/regime_effectiveness.py --max
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config
from backtester import (
    BacktestExecutor,
    BacktestPortfolio,
    MIN_HISTORY,
    TX_COST,
    BENCHMARK,
    DAILY_COOLDOWN_BARS,
    _benchmark_return,
    _ensure_daily_data,
)
from modules.crypto_vol_gate import crypto_trading_allowed
from modules.market_context import (
    get_market_regime,
    get_price_sentiment,
    get_volatility,
)
from modules.pipeline_strategies import (
    PAUSED_REGIMES,
    _crypto_pair_z,
    _equity_momentum_candidates,
    _spy_market_up_signal,
    run_crypto_strategy,
    run_equity_strategy,
    run_spy_strategy,
)
from modules.risk_management import RiskManager

REPORT_PATH = Path(__file__).resolve().parent / "regime_effectiveness_report.json"

# Daily-calibrated sentiment thresholds (±0.5 never fires on daily; see sleeve_overlap)
DAILY_SENTIMENT_THRESHOLDS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]


@dataclass
class RunFlags:
    paused_regimes: bool = True
    crypto_vol_only: bool = True
    drawdown_halt: bool = True
    max_drawdown_pct: float = 0.10


@dataclass
class BacktestResult:
    name: str
    flags: RunFlags
    final_equity: float
    total_return_pct: float
    sharpe: float
    max_drawdown_pct: float
    bench_return_pct: float | None
    spy_signals: int
    crypto_signals: int
    nyse_signals: int
    total_orders: int
    halt_date: str | None
    halt_equity: float | None
    regime_counts: dict
    equity_curve: list = field(repr=False)


class AnalysisExecutor(BacktestExecutor):
    """Alias kept for analysis scripts; BacktestExecutor is already sleeve-aware."""

    pass


def _regime_at_threshold(sentiment: float, vol: str, thresh: float) -> str:
    if sentiment > thresh and vol == "High":
        return "RHYME_A: Euphoric_Volatility"
    if sentiment < -thresh and vol == "High":
        return "RHYME_B: Panic_Volatility"
    if sentiment > thresh and vol == "Low":
        return "RHYME_C: Steady_Bullish_Growth"
    if sentiment < -thresh and vol == "Low":
        return "RHYME_E: Steady_Bearish_Decline"
    return "RHYME_D: Range_Bound_Neutral"


def run_backtest(data: pd.DataFrame, flags: RunFlags, name: str) -> BacktestResult:
    portfolio = BacktestPortfolio()
    pair_cooldown = {}
    risk_manager = RiskManager(max_drawdown_pct=flags.max_drawdown_pct)
    equity_curve = []
    regime_counts = Counter()
    total_crypto = total_equity = total_spy = total_orders = 0
    halted = False
    halt_date = None
    halt_equity = None
    cooldown_bars = DAILY_COOLDOWN_BARS

    orig_crypto_vol = config.CRYPTO_VOL_ONLY
    config.CRYPTO_VOL_ONLY = flags.crypto_vol_only

    try:
        for i in range(MIN_HISTORY, len(data)):
            window = data.iloc[: i + 1]
            prices = window.iloc[-1]
            eq = portfolio.equity(prices)
            equity_curve.append(eq)

            if flags.drawdown_halt:
                if halted or not risk_manager.check_drawdown(eq):
                    if not halted:
                        halted = True
                        halt_date = str(data.index[i].date())
                        halt_equity = round(eq, 2)
                    continue

            sentiment = get_price_sentiment(window)
            vol = get_volatility(window)
            regime = get_market_regime(sentiment, vol)
            regime_counts[regime] += 1

            effective_regime = regime
            if not flags.paused_regimes:
                effective_regime = "RHYME_D: Range_Bound_Neutral"

            executor = AnalysisExecutor(portfolio, prices)
            total_crypto += run_crypto_strategy(
                window,
                executor,
                effective_regime,
                i,
                pair_cooldown,
                cooldown_bars=cooldown_bars,
                volatility=vol,
            )
            total_spy += run_spy_strategy(
                window,
                executor,
                effective_regime,
                i,
                pair_cooldown,
                cooldown_bars=cooldown_bars,
            )
            total_equity += run_equity_strategy(
                window,
                executor,
                effective_regime,
                i,
                pair_cooldown,
                cooldown_bars=cooldown_bars,
            )
            total_orders += len(executor.orders)
    finally:
        config.CRYPTO_VOL_ONLY = orig_crypto_vol

    curve = pd.Series(equity_curve)
    returns = curve.pct_change().dropna()
    total_ret = (curve.iloc[-1] / portfolio.initial_capital - 1) * 100
    sharpe_scale = np.sqrt(252)
    sharpe = (returns.mean() / returns.std()) * sharpe_scale if returns.std() != 0 else 0
    max_dd = ((curve / curve.cummax()) - 1).min() * 100
    bench = _benchmark_return(data, MIN_HISTORY)

    return BacktestResult(
        name=name,
        flags=flags,
        final_equity=round(float(curve.iloc[-1]), 2),
        total_return_pct=round(total_ret, 2),
        sharpe=round(sharpe, 2),
        max_drawdown_pct=round(max_dd, 2),
        bench_return_pct=round(bench, 2) if bench is not None else None,
        spy_signals=total_spy,
        crypto_signals=total_crypto,
        nyse_signals=total_equity,
        total_orders=total_orders,
        halt_date=halt_date,
        halt_equity=halt_equity,
        regime_counts=dict(regime_counts),
        equity_curve=equity_curve,
    )


def trigger_frequency(data: pd.DataFrame) -> dict:
    """Count how often each gate would block entries (baseline logic)."""
    n = 0
    regime_counts = Counter()
    vol_counts = Counter()
    pause_days = 0
    vol_gate_blocks = 0
    spy_wants_raw = spy_blocked_pause = 0
    nyse_wants_raw = nyse_blocked_pause = 0
    crypto_z_days = 0
    crypto_blocked_regime = crypto_blocked_vol = 0
    crypto_allowed = 0
    gate_reasons = Counter()

    # Counterfactual: days SPY above MA200 but would pause
    spy_bull_pause = []
    nyse_momentum_pause = []

    for i in range(MIN_HISTORY, len(data)):
        window = data.iloc[: i + 1]
        ts = data.index[i]
        sentiment = get_price_sentiment(window)
        vol = get_volatility(window)
        regime = get_market_regime(sentiment, vol)
        n += 1
        regime_counts[regime] += 1
        vol_counts[vol] += 1

        paused = regime in PAUSED_REGIMES
        if paused:
            pause_days += 1

        spy_bull, _ = _spy_market_up_signal(window, config.SPY_BOT_SYMBOL, config.SPY_MA_WINDOW)
        if spy_bull:
            spy_wants_raw += 1
            if paused:
                spy_blocked_pause += 1
                spy_bull_pause.append(ts)

        equity_cols = [
            c
            for c in window.columns
            if not config.is_crypto(c)
            and c != config.SPY_BOT_SYMBOL
            and not config.is_metal_symbol(c)
        ]
        ranked = _equity_momentum_candidates(window, equity_cols)
        if ranked:
            nyse_wants_raw += 1
            if paused:
                nyse_blocked_pause += 1
                nyse_momentum_pause.append(ts)

        gate = crypto_trading_allowed(vol, regime, spacex_snapshot=None)
        gate_reasons[gate["reason"]] += 1
        if gate["allowed"]:
            crypto_allowed += 1
        elif gate["reason"] == "regime_paused":
            crypto_blocked_regime += 1
        elif gate["reason"] == "vol_low":
            crypto_blocked_vol += 1
            vol_gate_blocks += 1

        crypto_cols = [c for c in window.columns if config.is_crypto(c)]
        has_z = False
        for a in range(len(crypto_cols)):
            for b in range(a + 1, len(crypto_cols)):
                t1, t2 = crypto_cols[a], crypto_cols[b]
                if window[t1].corr(window[t2]) < config.CRYPTO_MIN_CORRELATION:
                    continue
                z = _crypto_pair_z(window, t1, t2)
                if abs(z) > 2.0:
                    has_z = True
                    break
            if has_z:
                break
        if has_z:
            crypto_z_days += 1

    return {
        "trading_days": n,
        "regime_counts": dict(regime_counts),
        "vol_counts": dict(vol_counts),
        "pause_regime_days": pause_days,
        "pause_regime_pct": round(100 * pause_days / n, 2),
        "vol_gate_block_days": vol_gate_blocks,
        "vol_gate_block_pct": round(100 * vol_gate_blocks / n, 2),
        "crypto_allowed_days": crypto_allowed,
        "crypto_allowed_pct": round(100 * crypto_allowed / n, 2),
        "crypto_gate_reasons": dict(gate_reasons),
        "spy_above_ma200_days": spy_wants_raw,
        "spy_blocked_by_pause": spy_blocked_pause,
        "nyse_momentum_days": nyse_wants_raw,
        "nyse_blocked_by_pause": nyse_blocked_pause,
        "crypto_z_signal_days": crypto_z_days,
        "crypto_z_blocked_by_vol": crypto_z_days - crypto_allowed if crypto_z_days > crypto_allowed else 0,
        "crypto_blocked_regime_days": crypto_blocked_regime,
        "crypto_blocked_vol_days": crypto_blocked_vol,
    }


def sentiment_calibration(data: pd.DataFrame) -> dict:
    sentiments = []
    vols = []
    for i in range(MIN_HISTORY, len(data)):
        window = data.iloc[: i + 1]
        sentiments.append(get_price_sentiment(window))
        vols.append(get_volatility(window))

    s = pd.Series(sentiments)
    v = pd.Series(vols)
    out = {
        "sentiment_min": round(float(s.min()), 4),
        "sentiment_max": round(float(s.max()), 4),
        "sentiment_mean": round(float(s.mean()), 4),
        "sentiment_std": round(float(s.std()), 4),
        "pct_above_0_5": round(100 * (s > 0.5).mean(), 2),
        "pct_below_neg_0_5": round(100 * (s < -0.5).mean(), 2),
        "vol_high_pct": round(100 * (v == "High").mean(), 2),
        "live_threshold_note": "±0.5 sentiment on daily never triggers RHYME A/B/C/E",
        "threshold_sweep": {},
    }
    for thresh in DAILY_SENTIMENT_THRESHOLDS:
        counts = Counter()
        pause_days = 0
        for sent, vol in zip(sentiments, vols):
            r = _regime_at_threshold(sent, vol, thresh)
            counts[r] += 1
            if r in PAUSED_REGIMES:
                pause_days += 1
        out["threshold_sweep"][str(thresh)] = {
            "regime_counts": dict(counts),
            "pause_days": pause_days,
            "pause_pct": round(100 * pause_days / len(sentiments), 2),
        }
    return out


def counterfactual_skip_value(data: pd.DataFrame, signals_df: pd.DataFrame | None = None) -> dict:
    """Estimate whether blocked-entry days had worse forward returns (5d/20d)."""
    rows = []
    for i in range(MIN_HISTORY, len(data) - 20):
        window = data.iloc[: i + 1]
        ts = data.index[i]
        sentiment = get_price_sentiment(window)
        vol = get_volatility(window)
        regime = get_market_regime(sentiment, vol)
        paused = regime in PAUSED_REGIMES

        spy_bull, _ = _spy_market_up_signal(window, config.SPY_BOT_SYMBOL, config.SPY_MA_WINDOW)
        gate = crypto_trading_allowed(vol, regime, spacex_snapshot=None)

        spy_fwd5 = spy_fwd20 = np.nan
        if config.SPY_BOT_SYMBOL in data.columns:
            spy_px = data[config.SPY_BOT_SYMBOL]
            spy_fwd5 = float(spy_px.iloc[i + 5] / spy_px.iloc[i] - 1) if i + 5 < len(spy_px) else np.nan
            spy_fwd20 = float(spy_px.iloc[i + 20] / spy_px.iloc[i] - 1) if i + 20 < len(spy_px) else np.nan

        # Derived bear proxy (what pause would catch if thresholds worked on daily)
        derived_bear = (sentiment < -0.10 and vol == "High") or (sentiment < -0.08 and vol == "Low")
        derived_panic = sentiment < -0.10 and vol == "High"

        rows.append(
            {
                "date": ts,
                "paused_live": paused,
                "derived_bear": derived_bear,
                "derived_panic": derived_panic,
                "spy_bull": spy_bull,
                "vol_low_crypto_block": not gate["allowed"] and gate["reason"] == "vol_low",
                "spy_fwd5": spy_fwd5,
                "spy_fwd20": spy_fwd20,
            }
        )

    df = pd.DataFrame(rows).set_index("date")

    def _fwd_stats(mask, label):
        sub = df[mask]
        if len(sub) < 5:
            return {"label": label, "days": len(sub), "note": "insufficient"}
        return {
            "label": label,
            "days": len(sub),
            "spy_fwd5_mean_pct": round(100 * sub["spy_fwd5"].mean(), 3),
            "spy_fwd20_mean_pct": round(100 * sub["spy_fwd20"].mean(), 3),
            "spy_fwd5_median_pct": round(100 * sub["spy_fwd5"].median(), 3),
        }

    # Compare: spy_bull days blocked vs allowed
    would_block_spy = df["spy_bull"] & df["derived_bear"]
    allowed_spy = df["spy_bull"] & ~df["derived_bear"]

    return {
        "live_pause_days": int(df["paused_live"].sum()),
        "derived_bear_days": int(df["derived_bear"].sum()),
        "derived_panic_days": int(df["derived_panic"].sum()),
        "vol_low_block_days": int(df["vol_low_crypto_block"].sum()),
        "fwd_spy_when_derived_bear_and_spy_bull": _fwd_stats(would_block_spy, "derived_bear_spy_bull"),
        "fwd_spy_when_spy_bull_not_derived_bear": _fwd_stats(allowed_spy, "spy_bull_ok"),
        "fwd_spy_vol_low_crypto_block": _fwd_stats(df["vol_low_crypto_block"], "vol_low_block"),
        "fwd_spy_vol_high_crypto_ok": _fwd_stats(df["vol"] if "vol" in df else pd.Series(False, index=df.index), "skip"),
    }


def counterfactual_skip_value_fixed(data: pd.DataFrame) -> dict:
    rows = []
    for i in range(MIN_HISTORY, len(data) - 20):
        window = data.iloc[: i + 1]
        sentiment = get_price_sentiment(window)
        vol = get_volatility(window)
        regime = get_market_regime(sentiment, vol)
        spy_bull, _ = _spy_market_up_signal(window, config.SPY_BOT_SYMBOL, config.SPY_MA_WINDOW)
        gate = crypto_trading_allowed(vol, regime, spacex_snapshot=None)
        derived_bear = (sentiment < -0.10 and vol == "High") or (sentiment < -0.08 and vol == "Low")
        spy_px = data[config.SPY_BOT_SYMBOL]
        spy_fwd5 = float(spy_px.iloc[i + 5] / spy_px.iloc[i] - 1)
        spy_fwd20 = float(spy_px.iloc[i + 20] / spy_px.iloc[i] - 1)
        rows.append(
            {
                "paused_live": regime in PAUSED_REGIMES,
                "derived_bear": derived_bear,
                "spy_bull": spy_bull,
                "vol_low_block": not gate["allowed"] and gate["reason"] == "vol_low",
                "vol_high": vol == "High",
                "spy_fwd5": spy_fwd5,
                "spy_fwd20": spy_fwd20,
            }
        )
    df = pd.DataFrame(rows)

    def stats(mask, label):
        sub = df[mask]
        if len(sub) < 5:
            return {"label": label, "days": len(sub)}
        return {
            "label": label,
            "days": len(sub),
            "spy_fwd5_mean_pct": round(100 * sub["spy_fwd5"].mean(), 3),
            "spy_fwd20_mean_pct": round(100 * sub["spy_fwd20"].mean(), 3),
        }

    return {
        "live_pause_days": int(df["paused_live"].sum()),
        "derived_bear_days": int(df["derived_bear"].sum()),
        "derived_panic_days": int((df["derived_bear"] & df["vol_high"]).sum()),
        "vol_low_block_days": int(df["vol_low_block"].sum()),
        "fwd_when_derived_bear_spy_bull": stats(df["spy_bull"] & df["derived_bear"], "block_spy_entry"),
        "fwd_when_spy_bull_normal": stats(df["spy_bull"] & ~df["derived_bear"], "allow_spy_entry"),
        "fwd_vol_low_crypto_block": stats(df["vol_low_block"], "vol_low"),
        "fwd_vol_high_crypto_ok": stats(df["vol_high"], "vol_high"),
    }


def halt_path_analysis(baseline: BacktestResult, no_halt: BacktestResult, data: pd.DataFrame) -> dict:
    if not baseline.halt_date:
        return {"halt_fired": False, "note": "10% drawdown halt never triggered in baseline run"}
    halt_idx = None
    for i in range(MIN_HISTORY, len(data)):
        if str(data.index[i].date()) == baseline.halt_date:
            halt_idx = i
            break
    post_halt_bench = None
    if halt_idx and BENCHMARK in data.columns:
        col = data[BENCHMARK].iloc[halt_idx:]
        if len(col) >= 2 and col.iloc[0] > 0:
            post_halt_bench = round((col.iloc[-1] / col.iloc[0] - 1) * 100, 2)

    # Equity path after halt (flat) vs no_halt
    bl_curve = baseline.equity_curve
    nh_curve = no_halt.equity_curve
    if halt_idx and halt_idx - MIN_HISTORY < len(bl_curve):
        hpos = halt_idx - MIN_HISTORY
        missed_upside = round(nh_curve[-1] - bl_curve[-1], 2)
        nh_post = nh_curve[hpos:]
        bl_post = bl_curve[hpos:]
        nh_post_ret = round((nh_post[-1] / nh_post[0] - 1) * 100, 2) if nh_post[0] else 0
    else:
        missed_upside = round(nh_curve[-1] - bl_curve[-1], 2)
        nh_post_ret = None

    return {
        "halt_fired": True,
        "halt_date": baseline.halt_date,
        "halt_equity": baseline.halt_equity,
        "baseline_final_equity": baseline.final_equity,
        "no_halt_final_equity": no_halt.final_equity,
        "equity_delta_no_halt_vs_baseline": missed_upside,
        "baseline_return_pct": baseline.total_return_pct,
        "no_halt_return_pct": no_halt.total_return_pct,
        "return_delta_pct": round(no_halt.total_return_pct - baseline.total_return_pct, 2),
        "post_halt_vti_return_pct": post_halt_bench,
        "path_distortion_note": (
            "After halt, baseline stops trading; equity flat while no_halt continues. "
            "Max DD in baseline is capped near halt threshold; no_halt shows true path."
        ),
    }


def drawdown_episodes(data: pd.DataFrame, flags: RunFlags) -> list[dict]:
    """Find peak-to-trough episodes >= 8% on no-halt run."""
    r = run_backtest(data, flags, "_ep")
    curve = pd.Series(r.equity_curve)
    dates = data.index[MIN_HISTORY : MIN_HISTORY + len(curve)]
    dd = curve / curve.cummax() - 1
    episodes = []
    in_ep = False
    start = None
    for i, (d, v) in enumerate(zip(dates, dd)):
        if v <= -0.08 and not in_ep:
            in_ep = True
            start = d
            trough = v
        elif in_ep:
            trough = min(trough, v)
            if v > -0.03:
                episodes.append(
                    {
                        "start": str(start.date()),
                        "trough_date": str(dates[i].date()),
                        "min_dd_pct": round(100 * trough, 2),
                    }
                )
                in_ep = False
    return episodes[:15]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", action="store_true")
    parser.add_argument("--days", type=int, default=2000)
    args = parser.parse_args()

    data = _ensure_daily_data(0 if args.max else args.days, use_max=args.max)
    start = data.index[MIN_HISTORY]
    end = data.index[-1]
    print(f"Regime effectiveness analysis: {start.date()} -> {end.date()} ({len(data)} bars)")

    scenarios = [
        ("baseline", RunFlags()),
        ("no_pause", RunFlags(paused_regimes=False)),
        ("no_vol_gate", RunFlags(crypto_vol_only=False)),
        ("no_halt", RunFlags(drawdown_halt=False)),
        ("all_off", RunFlags(paused_regimes=False, crypto_vol_only=False, drawdown_halt=False)),
    ]

    results = {}
    bt_objects = {}
    for name, flags in scenarios:
        print(f"  Running {name}...")
        r = run_backtest(data, flags, name)
        bt_objects[name] = r
        results[name] = {
            k: v
            for k, v in r.__dict__.items()
            if k != "equity_curve" and k != "flags"
        }
        results[name]["flags"] = {
            "paused_regimes": flags.paused_regimes,
            "crypto_vol_only": flags.crypto_vol_only,
            "drawdown_halt": flags.drawdown_halt,
        }
        print(
            f"    ret={r.total_return_pct}% sharpe={r.sharpe} maxDD={r.max_drawdown_pct}% "
            f"orders={r.total_orders} halt={r.halt_date}"
        )

    triggers = trigger_frequency(data)
    calibration = sentiment_calibration(data)
    counterfactual = counterfactual_skip_value_fixed(data)
    halt_analysis = halt_path_analysis(bt_objects["baseline"], bt_objects["no_halt"], data)

    # Delta table vs baseline
    base = bt_objects["baseline"]
    deltas = {}
    for name, r in bt_objects.items():
        if name == "baseline":
            continue
        deltas[name] = {
            "return_delta_pct": round(r.total_return_pct - base.total_return_pct, 2),
            "sharpe_delta": round(r.sharpe - base.sharpe, 2),
            "max_dd_delta_pct": round(r.max_drawdown_pct - base.max_drawdown_pct, 2),
            "orders_delta": r.total_orders - base.total_orders,
        }

    report = {
        "data_window": {"start": str(start.date()), "end": str(end.date()), "bars": len(data)},
        "config": {
            "paused_regimes": list(PAUSED_REGIMES),
            "crypto_vol_only": config.CRYPTO_VOL_ONLY,
            "max_drawdown_pct": config.MAX_DRAWDOWN_PCT,
            "vol_threshold": 0.02,
            "sentiment_threshold": 0.5,
        },
        "backtest_scenarios": results,
        "deltas_vs_baseline": deltas,
        "trigger_frequency": triggers,
        "sentiment_calibration": calibration,
        "counterfactual_skip_value": counterfactual,
        "halt_analysis": halt_analysis,
        "limitations": [
            "Daily bars: RHYME B/E pause never fires at ±0.5 sentiment (see calibration).",
            "Live bot uses 5m bars; vol gate and sentiment differ materially.",
            "AnalysisExecutor enables crypto notional; stock backtester.py skips crypto buys.",
            "No wisdom/yield/SpaceX override in backtest variants.",
            "SPY_EXIT_ON_MA_BREAK=False — no MA breakdown exits.",
        ],
    }

    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nWrote {REPORT_PATH}")
    return report


if __name__ == "__main__":
    main()
