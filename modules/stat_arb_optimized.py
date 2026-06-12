"""DISABLED — experimental Kalman/decay stat-arb optimization (paper A/B only).

Not wired into production. Set PAPER_STAT_ARB_OPTIMIZED=true to experiment via backtester
--compare-stat-arb-optimized. Default path uses modules/stat_arb_sleeve.py (cointegration + Z).
effective_stat_arb_optimized() always returns False.
"""

from __future__ import annotations

ENABLED = False

import numpy as np
import pandas as pd

import config
from modules.stat_arb_sleeve import (
    _scan_pair_candidates,
    hedge_ratio,
    spread_zscore,
)


def window_vol_score(data: pd.DataFrame, lookback: int = 20) -> float:
    if data is None or data.empty or len(data) < 5:
        return 0.02
    rets = data.pct_change().tail(lookback)
    val = float(rets.std(axis=1).mean())
    return val if np.isfinite(val) and val > 0 else 0.02


def decay_weighted_correlation(
    y: pd.Series,
    x: pd.Series,
    *,
    lookback: int,
    half_life: float | None = None,
) -> float:
    sub = pd.concat([y, x], axis=1).dropna().tail(lookback)
    if len(sub) < 15:
        return 0.0
    hl = half_life or config.STAT_ARB_CORR_HALF_LIFE
    n = len(sub)
    weights = np.exp(-np.arange(n - 1, -1, -1) / hl)
    weights /= weights.sum()
    yv = sub.iloc[:, 0].astype(float).values
    xv = sub.iloc[:, 1].astype(float).values
    ym = np.average(yv, weights=weights)
    xm = np.average(xv, weights=weights)
    cov = np.average((yv - ym) * (xv - xm), weights=weights)
    vy = np.average((yv - ym) ** 2, weights=weights)
    vx = np.average((xv - xm) ** 2, weights=weights)
    return float(cov / (np.sqrt(vy * vx) + 1e-9))


def kalman_hedge_ratio(y: pd.Series, x: pd.Series, *, lookback: int) -> float:
    yv = y.tail(lookback).astype(float).values
    xv = x.tail(lookback).astype(float).values
    if len(yv) < 12:
        return hedge_ratio(x, y)
    beta = float(np.cov(xv, yv)[0, 1] / (np.var(xv) + 1e-9))
    q, r = 1e-4, 5e-3
    p = 1.0
    for i in range(1, len(yv)):
        p = p + q
        denom = xv[i] ** 2 * p + r + 1e-9
        k = p * xv[i] / denom
        beta = beta + k * (yv[i] - beta * xv[i])
        p = (1.0 - k * xv[i]) * p
    return float(beta)


def dynamic_z_entry(base: float, vol_score: float | None) -> float:
    vs = vol_score if vol_score is not None else 0.02
    if vs < 0.012:
        return base * 1.05
    if vs > 0.028:
        return base * 0.94
    return base


def dynamic_z_exit(base: float, vol_score: float | None) -> float:
    vs = vol_score if vol_score is not None else 0.02
    if vs < 0.012:
        return base * 0.85
    if vs > 0.028:
        return base * 1.08
    return base


def pair_size_scale(*, abs_z: float, z_entry: float, decay_corr: float, min_corr: float) -> float:
    """Down-size only clearly weak pairs; strong setups stay near full size."""
    if decay_corr >= min_corr * 1.02 and abs_z >= z_entry * 1.05:
        return 1.0
    z_part = min(1.15, abs_z / max(z_entry, 0.1)) / 1.15
    corr_part = min(1.0, decay_corr / max(min_corr * 0.95, 0.01))
    raw = 0.40 * z_part + 0.60 * corr_part
    return max(0.88, min(1.0, raw))


def should_exit_pair(
    position: dict,
    z: float,
    *,
    bar_i: int | None,
    vol_score: float | None,
) -> tuple[bool, str]:
    z_exit = dynamic_z_exit(config.effective_pair_z_exit(), vol_score)
    if abs(z) <= z_exit:
        return True, "z_exit"
    entry_z = float(position.get("entry_z", 0.0))
    reverted = abs(entry_z) - abs(z)
    if reverted >= config.STAT_ARB_PROFIT_Z_DELTA and abs(z) <= z_exit + 0.20:
        return True, "profit_target"
    if bar_i is not None:
        entry_bar = position.get("entry_bar")
        if entry_bar is not None:
            held = int(bar_i) - int(entry_bar)
            stale = abs(z) > z_exit + 0.5
            if held >= config.STAT_ARB_MAX_HOLD_BARS and stale:
                return True, "time_stop"
    return False, ""


def scan_optimized_candidates(
    data: pd.DataFrame,
    symbols: list[str],
    *,
    lookback: int,
    min_corr: float,
    z_entry_base: float,
    momentum_pick: bool = False,
    vol_score: float | None = None,
) -> list[dict]:
    """Legacy cointegration scan, Kalman hedge + decay corr rank/size."""
    z_entry_dyn = dynamic_z_entry(z_entry_base, vol_score)
    raw = _scan_pair_candidates(
        data,
        symbols,
        lookback=lookback,
        min_corr=min_corr,
        z_entry=z_entry_base,
        momentum_pick=momentum_pick,
    )
    out: list[dict] = []
    for _abs_z, z, long_sym, short_sym, beta, y_sym, x_sym in raw:
        k_beta = kalman_hedge_ratio(data[y_sym], data[x_sym], lookback=lookback)
        k_z = spread_zscore(data[y_sym], data[x_sym], k_beta, lookback=lookback)
        dcorr = decay_weighted_correlation(
            data[y_sym], data[x_sym], lookback=lookback
        )
        scale = pair_size_scale(
            abs_z=abs(k_z),
            z_entry=z_entry_dyn,
            decay_corr=dcorr,
            min_corr=min_corr,
        )
        exec_scale = 1.0 if scale >= 0.92 else scale
        if momentum_pick:
            long_s, short_s = long_sym, short_sym
        else:
            long_s = x_sym if k_z > 0 else y_sym
            short_s = y_sym if k_z > 0 else x_sym
        out.append(
            {
                "abs_z": abs(k_z),
                "score": abs(k_z),
                "z_score": k_z,
                "long_symbol": long_s,
                "short_symbol": short_s,
                "beta": k_beta,
                "y_symbol": y_sym,
                "x_symbol": x_sym,
                "size_scale": exec_scale,
                "decay_corr": dcorr,
            }
        )
    out.sort(key=lambda r: r["abs_z"], reverse=True)
    return out
