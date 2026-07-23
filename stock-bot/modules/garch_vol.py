"""GARCH(1,1) next-day volatility forecast for Realistic Research sizing.

Fits a lightweight variance-targeting GARCH(1,1) on daily returns (no ``arch``
dependency) and maps the next-day σ forecast to a **size multiplier** that
reduces risk in high-vol regimes. Default multiplier cap is 1.0 so low-vol
never increases risk vs the current baseline.

Locked paper / Realistic Research default ON; live stays off unless
``GARCH_VOL_LIVE_ENABLED=true``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

import config

logger = logging.getLogger(__name__)

_DEFAULT_ALPHA = 0.06
_DEFAULT_BETA = 0.92


@dataclass
class GarchVolState:
    ok: bool = False
    symbol: str = ""
    forecast_vol: float | None = None  # daily σ
    anchor_vol: float | None = None  # rolling realized σ (median)
    ratio: float | None = None  # forecast / anchor
    size_mult: float = 1.0
    vti_adj_pp: float = 0.0
    omega: float | None = None
    alpha: float | None = None
    beta: float | None = None
    n_obs: int = 0
    reason: str = ""


_state = GarchVolState()
_high_vol_days = 0  # bars where size_mult < 0.95 (compare / stats)
_bars_since_retrain = 0
_cached_alpha: float | None = None
_cached_beta: float | None = None


def reset_garch_vol_state() -> None:
    """Clear in-memory forecast (compare legs / tests)."""
    global _state, _high_vol_days, _bars_since_retrain, _cached_alpha, _cached_beta
    _state = GarchVolState()
    _high_vol_days = 0
    _bars_since_retrain = 0
    _cached_alpha = None
    _cached_beta = None


def get_garch_vol_state() -> GarchVolState:
    return GarchVolState(**asdict(_state))


def garch_high_vol_day_count() -> int:
    return int(_high_vol_days)


def _lookback() -> int:
    return max(60, int(getattr(config, "GARCH_VOL_LOOKBACK", 252) or 252))


def _anchor_window() -> int:
    return max(10, int(getattr(config, "GARCH_VOL_ANCHOR_WINDOW", 21) or 21))


def _mult_bounds() -> tuple[float, float]:
    lo = float(getattr(config, "GARCH_VOL_MULT_MIN", 0.55) or 0.55)
    hi = float(getattr(config, "GARCH_VOL_MULT_MAX", 1.0) or 1.0)
    lo = max(0.20, min(1.0, lo))
    hi = max(lo, min(1.25, hi))
    return lo, hi


def _ratio_bounds() -> tuple[float, float]:
    """ratio <= low → mult_max; ratio >= high → mult_min."""
    low = float(getattr(config, "GARCH_VOL_RATIO_LOW", 0.85) or 0.85)
    high = float(getattr(config, "GARCH_VOL_RATIO_HIGH", 1.35) or 1.35)
    if high <= low:
        high = low + 0.25
    return low, high


def _vti_scale_pp() -> float:
    """Percentage points of VTI nudge per unit of (ratio - 1), clamped later."""
    return max(0.0, float(getattr(config, "GARCH_VOL_VTI_SCALE_PP", 8.0) or 8.0))


def _vti_max_pp() -> float:
    return max(0.0, float(getattr(config, "GARCH_VOL_VTI_MAX_PP", 6.0) or 6.0))


def _price_series(data, symbol: str | None = None):
    if data is None or getattr(data, "empty", True):
        return None
    candidates: list[str] = []
    sym = (symbol or getattr(config, "GARCH_VOL_SYMBOL", None) or "").strip().upper()
    if sym:
        candidates.append(sym)
    for name in (
        getattr(config, "SPY_BOT_SYMBOL", "SPY"),
        getattr(config, "VTI_CORE_SYMBOL", "VTI"),
        "SPY",
        "VTI",
    ):
        key = str(name or "").upper()
        if key and key not in candidates:
            candidates.append(key)
    for key in candidates:
        if key in data.columns:
            return data[key].dropna()
        usd = f"{key}-USD"
        if usd in data.columns:
            return data[usd].dropna()
    # Fallback: first numeric column that looks equity-like
    for col in data.columns:
        series = data[col].dropna()
        if len(series) >= 40:
            return series
    return None


def _daily_returns(prices) -> np.ndarray | None:
    if prices is None or len(prices) < 40:
        return None
    arr = np.asarray(prices, dtype=float)
    if arr.ndim != 1 or len(arr) < 40:
        return None
    # log returns are more GARCH-friendly; fall back to simple if needed
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = np.diff(np.log(np.maximum(arr, 1e-12)))
    rets = rets[np.isfinite(rets)]
    if len(rets) < 30:
        return None
    return rets


def fit_garch11(
    returns: np.ndarray,
    *,
    alpha: float = _DEFAULT_ALPHA,
    beta: float = _DEFAULT_BETA,
) -> tuple[float, float, float, float] | None:
    """Variance-targeting GARCH(1,1).

    Returns ``(omega, alpha, beta, next_day_variance)`` or None.
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 30:
        return None
    alpha = float(alpha)
    beta = float(beta)
    persistence = alpha + beta
    if persistence >= 0.999:
        # Keep stationary; nudge beta down.
        beta = max(0.50, 0.998 - alpha)
        persistence = alpha + beta
    if alpha <= 0 or beta <= 0 or persistence >= 1.0:
        return None
    uncond = float(np.mean(r * r))
    if uncond <= 0:
        return None
    omega = uncond * (1.0 - persistence)
    # Initialize σ² at unconditional; recurse.
    sigma2 = uncond
    for rt in r:
        sigma2 = omega + alpha * (rt * rt) + beta * sigma2
        if not np.isfinite(sigma2) or sigma2 <= 0:
            sigma2 = uncond
    next_var = omega + alpha * (r[-1] * r[-1]) + beta * sigma2
    if not np.isfinite(next_var) or next_var <= 0:
        return None
    return omega, alpha, beta, float(next_var)


def _calibrate_alpha_beta(returns: np.ndarray) -> tuple[float, float]:
    """Coarse grid QMLE over a small (α, β) lattice; fallback to defaults."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 60:
        return _DEFAULT_ALPHA, _DEFAULT_BETA
    best_ll = -np.inf
    best = (_DEFAULT_ALPHA, _DEFAULT_BETA)
    alphas = (0.04, 0.06, 0.08, 0.10)
    betas = (0.88, 0.90, 0.92, 0.94)
    for a in alphas:
        for b in betas:
            if a + b >= 0.999:
                continue
            fitted = fit_garch11(r, alpha=a, beta=b)
            if fitted is None:
                continue
            omega, _, _, _ = fitted
            # Reconstruct path for log-likelihood
            uncond = float(np.mean(r * r))
            sigma2 = uncond
            ll = 0.0
            ok = True
            for rt in r:
                sigma2 = omega + a * (rt * rt) + b * sigma2
                if sigma2 <= 1e-16:
                    ok = False
                    break
                ll += -0.5 * (np.log(sigma2) + (rt * rt) / sigma2)
            if ok and ll > best_ll:
                best_ll = ll
                best = (a, b)
    return best


def _map_ratio_to_mult(ratio: float) -> float:
    lo, hi = _mult_bounds()
    r_lo, r_hi = _ratio_bounds()
    if ratio <= r_lo:
        return hi
    if ratio >= r_hi:
        return lo
    # Linear: higher ratio → lower mult
    t = (ratio - r_lo) / (r_hi - r_lo)
    return round(hi + t * (lo - hi), 4)


def _map_ratio_to_vti_pp(ratio: float) -> float:
    """High forecast vol → more VTI (defensive). Low vol → 0 or tiny cut."""
    scale = _vti_scale_pp()
    cap = _vti_max_pp()
    raw = (ratio - 1.0) * scale
    # Conservative: do not pull VTI down much on calm forecasts (active risk stays capped by mult<=1).
    if raw < 0:
        raw = max(raw, -min(2.0, cap * 0.25))
    return round(max(-cap, min(cap, raw)), 2)


def update_garch_vol(data, *, symbol: str | None = None) -> GarchVolState:
    """Fit GARCH on trailing daily returns and refresh size/VTI soft-signals."""
    global _state, _high_vol_days, _bars_since_retrain, _cached_alpha, _cached_beta

    if not config.effective_garch_vol_enabled():
        _state = GarchVolState(ok=False, reason="disabled", size_mult=1.0)
        return get_garch_vol_state()

    series = _price_series(data, symbol=symbol)
    if series is None:
        _state = GarchVolState(ok=False, reason="no_price_series", size_mult=1.0)
        return get_garch_vol_state()

    rets = _daily_returns(series)
    if rets is None:
        _state = GarchVolState(ok=False, reason="short_history", size_mult=1.0)
        return get_garch_vol_state()

    lookback = _lookback()
    if len(rets) > lookback:
        rets = rets[-lookback:]

    retrain_every = max(1, int(getattr(config, "GARCH_VOL_RETRAIN_EVERY_BARS", 5) or 5))
    need_retrain = (
        _cached_alpha is None
        or _cached_beta is None
        or _bars_since_retrain >= retrain_every
        or not _state.ok
    )
    if need_retrain:
        alpha, beta = _calibrate_alpha_beta(rets)
        _cached_alpha, _cached_beta = alpha, beta
        _bars_since_retrain = 0
    else:
        alpha, beta = float(_cached_alpha), float(_cached_beta)
        _bars_since_retrain += 1

    fitted = fit_garch11(rets, alpha=alpha, beta=beta)
    if fitted is None:
        _state = GarchVolState(ok=False, reason="fit_failed", size_mult=1.0)
        return get_garch_vol_state()

    omega, alpha, beta, next_var = fitted
    forecast = float(np.sqrt(next_var))

    # Anchor: median of recent rolling realized-vol windows (capped for speed).
    aw = _anchor_window()
    if len(rets) < aw:
        anchor = float(np.std(rets, ddof=1)) if len(rets) >= 5 else forecast
    else:
        # Sample up to ~12 trailing windows instead of every day in lookback.
        ends = list(range(len(rets), aw - 1, -max(1, aw // 2)))[:12]
        windows = []
        for end in ends:
            start = end - aw
            if start < 0:
                continue
            s = float(np.std(rets[start:end], ddof=1))
            if s > 0:
                windows.append(s)
        anchor = float(np.median(windows)) if windows else float(np.std(rets[-aw:], ddof=1))

    if not np.isfinite(anchor) or anchor <= 1e-12:
        _state = GarchVolState(ok=False, reason="bad_anchor", size_mult=1.0)
        return get_garch_vol_state()

    ratio = float(forecast / anchor)
    size_mult = _map_ratio_to_mult(ratio)
    vti_pp = _map_ratio_to_vti_pp(ratio)

    sym = str(getattr(series, "name", None) or symbol or getattr(config, "GARCH_VOL_SYMBOL", "SPY"))
    _state = GarchVolState(
        ok=True,
        symbol=str(sym).upper(),
        forecast_vol=round(forecast, 6),
        anchor_vol=round(anchor, 6),
        ratio=round(ratio, 4),
        size_mult=size_mult,
        vti_adj_pp=vti_pp,
        omega=round(omega, 10),
        alpha=round(alpha, 4),
        beta=round(beta, 4),
        n_obs=len(rets),
        reason="ok",
    )
    if size_mult < 0.95:
        _high_vol_days += 1
    return get_garch_vol_state()


def garch_vol_size_multiplier() -> float:
    if not config.effective_garch_vol_enabled():
        return 1.0
    if not _state.ok:
        return 1.0
    return float(_state.size_mult or 1.0)


def garch_vol_vti_adjustment_pp() -> float:
    if not config.effective_garch_vol_enabled():
        return 0.0
    if not _state.ok:
        return 0.0
    return float(_state.vti_adj_pp or 0.0)


def format_garch_vol_banner() -> str | None:
    if not getattr(config, "GARCH_VOL_ENABLED", False):
        return None
    if not (
        config.effective_garch_vol_enabled()
        or getattr(config, "PAPER_AGGRESSIVE_ENABLED", False)
        or getattr(config, "PAPER_TRADING", False)
    ):
        return None
    lo, hi = _mult_bounds()
    if _state.ok and _state.ratio is not None:
        return (
            f"GARCH Vol: ON locked paper (σ̂/anchor {_state.ratio:.2f} → "
            f"size x{_state.size_mult:.2f}, VTI {_state.vti_adj_pp:+.1f}pp | "
            f"mult {lo:.2f}-{hi:.2f})"
        )
    return f"GARCH Vol: ON locked paper (mult {lo:.2f}-{hi:.2f}, await fit)"


def heartbeat_garch_vol_payload() -> dict[str, Any] | None:
    if not config.effective_garch_vol_enabled():
        return None
    st = get_garch_vol_state()
    return {
        "enabled": True,
        "ok": bool(st.ok),
        "forecast_vol": st.forecast_vol,
        "anchor_vol": st.anchor_vol,
        "ratio": st.ratio,
        "size_mult": st.size_mult,
        "vti_adj_pp": st.vti_adj_pp,
        "high_vol_days": garch_high_vol_day_count(),
    }
