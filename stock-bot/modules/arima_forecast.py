"""Optional ARIMA mean forecast for Realistic Research (paper-only boost).

Fits a simple ARIMA on a rolling window of **daily log returns** (safer for
stationarity than close levels). Default order is ARIMA(1,0,1) with fallback to
(1,1,1); if both fail, uses the last return as a crude next-step mean.

Directional signal: sign of the one-step-ahead mean forecast.

Hybrid (default when ARIMA on): ARIMA mean × GARCH vol awareness
-----------------------------------------------------------------
``ARIMA_GARCH_HYBRID=true`` (default) combines mean direction with a GARCH
vol dampener for sizing/conviction:

    mean_mult  = boost (~1.08) if forecast > 0 else neg (~1.0)
    vol_scale  = 1.0 at low GARCH ratio → 0.0 at high ratio
                 (reuses GARCH_VOL_RATIO_LOW/HIGH; orthogonal to full GARCH size)

**Double-count guard** (critical): when ``effective_garch_vol_enabled()`` is
already multiplying ``effective_risk_per_trade``, hybrid does **not** multiply
the full ``garch_vol_size_multiplier()`` again. Instead it only scales the
ARIMA *excess* above 1.0:

    size_mult = clamp(1 + (mean_mult - 1) * vol_scale, min, max)

When GARCH is off / unavailable, hybrid falls back to mean_mult alone (or
``mean_mult * garch_mult`` only if GARCH state exists but is not already in
the risk path — rare). Caps: ``ARIMA_HYBRID_MULT_MIN`` (~0.55) …
``ARIMA_MULT_MAX`` (~1.15).

Opt-in via ``ARIMA_ENABLED=true`` on paper; live stays off unless
``ARIMA_LIVE_ENABLED=true``. Not locked ON for Realistic Research.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

import config

logger = logging.getLogger(__name__)


@dataclass
class ArimaForecastState:
    ok: bool = False
    symbol: str = ""
    forecast: float | None = None  # next-step mean (log-return units)
    sign: int = 0  # -1, 0, +1
    size_mult: float = 1.0
    mean_mult: float = 1.0  # directional-only (pre-hybrid)
    vol_scale: float = 1.0  # 1=full mean boost; 0=neutralize excess in high vol
    hybrid: bool = False
    garch_ratio: float | None = None
    order: str = ""
    n_obs: int = 0
    reason: str = ""


_state = ArimaForecastState()
_bars_since_retrain = 0
_positive_days = 0


def reset_arima_forecast_state() -> None:
    """Clear in-memory forecast (compare legs / tests)."""
    global _state, _bars_since_retrain, _positive_days
    _state = ArimaForecastState()
    _bars_since_retrain = 0
    _positive_days = 0


def get_arima_forecast_state() -> ArimaForecastState:
    return ArimaForecastState(**asdict(_state))


def arima_positive_day_count() -> int:
    return int(_positive_days)


def _lookback() -> int:
    return max(
        40,
        int(
            getattr(config, "ARIMA_WINDOW", None)
            or getattr(config, "ARIMA_LOOKBACK", 252)
            or 252
        ),
    )


def _boost_mult() -> float:
    boost = float(getattr(config, "ARIMA_BOOST_MULT", 1.08) or 1.08)
    cap = float(getattr(config, "ARIMA_MULT_MAX", 1.15) or 1.15)
    boost = max(1.0, min(cap, boost))
    return round(boost, 4)


def _neg_mult() -> float:
    """Neutral (1.0) by default; slight dampen allowed but never aggressive."""
    raw = float(getattr(config, "ARIMA_NEG_MULT", 1.0) or 1.0)
    # Cap: no size-up on negative; floor soft dampen only.
    return round(max(0.90, min(1.0, raw)), 4)


def _hybrid_mult_bounds() -> tuple[float, float]:
    lo = float(getattr(config, "ARIMA_HYBRID_MULT_MIN", 0.55) or 0.55)
    hi = float(getattr(config, "ARIMA_MULT_MAX", 1.15) or 1.15)
    lo = max(0.20, min(1.0, lo))
    hi = max(lo, min(1.25, hi))
    return lo, hi


def _hybrid_enabled() -> bool:
    """Master: ARIMA on + ARIMA_GARCH_HYBRID (default true)."""
    if not config.effective_arima_enabled():
        return False
    return bool(getattr(config, "ARIMA_GARCH_HYBRID", True))


def _garch_ratio_and_mult() -> tuple[float | None, float]:
    """Read current GARCH state without re-fitting (may be unset)."""
    try:
        from modules.garch_vol import get_garch_vol_state

        st = get_garch_vol_state()
        if not st.ok:
            return None, 1.0
        ratio = float(st.ratio) if st.ratio is not None else None
        mult = float(st.size_mult or 1.0)
        return ratio, mult
    except Exception:
        return None, 1.0


def _vol_scale_from_ratio(ratio: float | None) -> float:
    """Map GARCH forecast/anchor ratio → [0, 1] dampener for mean *excess*.

    Low vol (ratio ≤ low) → 1.0 (full ARIMA boost).
    High vol (ratio ≥ high) → 0.0 (neutralize boost toward 1.0).
    Linear in between. Does **not** embed the full GARCH size multiplier.
    """
    if ratio is None or not np.isfinite(ratio):
        return 1.0
    r_lo = float(getattr(config, "GARCH_VOL_RATIO_LOW", 0.85) or 0.85)
    r_hi = float(getattr(config, "GARCH_VOL_RATIO_HIGH", 1.35) or 1.35)
    if r_hi <= r_lo:
        r_hi = r_lo + 0.25
    floor = float(getattr(config, "ARIMA_HYBRID_VOL_SCALE_FLOOR", 0.0) or 0.0)
    floor = max(0.0, min(1.0, floor))
    if ratio <= r_lo:
        return 1.0
    if ratio >= r_hi:
        return floor
    t = (ratio - r_lo) / (r_hi - r_lo)
    return round(1.0 + t * (floor - 1.0), 4)


def combine_arima_garch_mult(
    mean_mult: float,
    *,
    garch_ratio: float | None = None,
    garch_size_mult: float = 1.0,
    garch_already_in_risk: bool | None = None,
) -> tuple[float, float]:
    """Conservative hybrid combine → ``(size_mult, vol_scale)``.

    Documented rule:
      - positive mean → modest boost (``mean_mult`` ~1.08)
      - high GARCH vol → dampen via ``vol_scale`` from ratio (not full GARCH mult
        when GARCH already multiplies risk)
      - final = clamp(..., ARIMA_HYBRID_MULT_MIN, ARIMA_MULT_MAX)

    When GARCH is **not** already in the risk path and a GARCH size mult is
    available (< 1), fall back to ``mean_mult * garch_size_mult`` so hybrid
    still provides vol awareness standalone.
    """
    lo, hi = _hybrid_mult_bounds()
    mean_mult = float(mean_mult)
    if garch_already_in_risk is None:
        try:
            garch_already_in_risk = bool(config.effective_garch_vol_enabled())
        except Exception:
            garch_already_in_risk = False

    vol_scale = _vol_scale_from_ratio(garch_ratio)

    if garch_already_in_risk:
        # Orthogonal: shrink only the mean excess; GARCH size already applied.
        size = 1.0 + (mean_mult - 1.0) * vol_scale
    elif garch_size_mult < 0.999 and garch_ratio is not None:
        # GARCH off in risk path but we have a vol signal — mean × vol once.
        size = mean_mult * float(garch_size_mult)
        vol_scale = float(garch_size_mult)
    else:
        size = mean_mult
        vol_scale = 1.0

    size = max(lo, min(hi, size))
    return round(size, 4), round(vol_scale, 4)


def _price_series(data, symbol: str | None = None):
    if data is None or getattr(data, "empty", True):
        return None
    candidates: list[str] = []
    sym = (symbol or getattr(config, "ARIMA_SYMBOL", None) or "").strip().upper()
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
    for col in data.columns:
        series = data[col].dropna()
        if len(series) >= 40:
            return series
    return None


def _daily_log_returns(prices) -> np.ndarray | None:
    if prices is None or len(prices) < 40:
        return None
    arr = np.asarray(prices, dtype=float)
    if arr.ndim != 1 or len(arr) < 40:
        return None
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = np.diff(np.log(np.maximum(arr, 1e-12)))
    rets = rets[np.isfinite(rets)]
    if len(rets) < 30:
        return None
    return rets


def _parse_order(raw: str | None = None) -> tuple[int, int, int]:
    text = (raw or getattr(config, "ARIMA_ORDER", "1,0,1") or "1,0,1").strip()
    parts = [p.strip() for p in text.replace(" ", "").split(",") if p.strip()]
    if len(parts) != 3:
        return 1, 0, 1
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return 1, 0, 1


def fit_arima_mean_forecast(returns: np.ndarray) -> tuple[float, str] | None:
    """Fit ARIMA on log returns; return ``(next_step_mean, order_label)`` or None.

    Tries configured order (default 1,0,1), then (1,1,1). On total failure,
    falls back to the last observed return as a crude mean.
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 30:
        return None

    primary = _parse_order()
    orders = [primary]
    if primary != (1, 1, 1):
        orders.append((1, 1, 1))
    if primary != (1, 0, 1) and (1, 0, 1) not in orders:
        orders.append((1, 0, 1))

    try:
        from statsmodels.tsa.arima.model import ARIMA
    except Exception as exc:
        logger.debug("statsmodels ARIMA unavailable: %s", exc)
        last = float(r[-1])
        return last, "fallback_last_ret"

    for p, d, q in orders:
        try:
            model = ARIMA(
                r,
                order=(p, d, q),
                enforce_stationarity=False,
                enforce_invertibility=False,
            )
            fitted = model.fit(method_kwargs={"warn_convergence": False})
            fc = fitted.forecast(steps=1)
            val = float(np.asarray(fc).ravel()[0])
            if np.isfinite(val):
                return val, f"{p},{d},{q}"
        except Exception as exc:
            logger.debug("ARIMA(%s,%s,%s) fit failed: %s", p, d, q, exc)
            continue

    last = float(r[-1])
    if np.isfinite(last):
        return last, "fallback_last_ret"
    return None


def _map_forecast_to_mean_mult(forecast: float) -> float:
    if forecast > 0:
        return _boost_mult()
    return _neg_mult()


def _finalize_size_mult(mean_mult: float) -> tuple[float, float, float | None, bool]:
    """Apply hybrid (or pass-through) → size_mult, vol_scale, garch_ratio, hybrid."""
    if not _hybrid_enabled():
        return mean_mult, 1.0, None, False
    ratio, g_mult = _garch_ratio_and_mult()
    size, vol_scale = combine_arima_garch_mult(
        mean_mult,
        garch_ratio=ratio,
        garch_size_mult=g_mult,
    )
    return size, vol_scale, ratio, True


def update_arima_forecast(data, *, symbol: str | None = None) -> ArimaForecastState:
    """Fit ARIMA on trailing log returns and refresh the size soft-signal."""
    global _state, _bars_since_retrain, _positive_days

    if not config.effective_arima_enabled():
        _state = ArimaForecastState(ok=False, reason="disabled", size_mult=1.0)
        return get_arima_forecast_state()

    series = _price_series(data, symbol=symbol)
    if series is None:
        _state = ArimaForecastState(ok=False, reason="no_price_series", size_mult=1.0)
        return get_arima_forecast_state()

    rets = _daily_log_returns(series)
    if rets is None:
        _state = ArimaForecastState(ok=False, reason="short_history", size_mult=1.0)
        return get_arima_forecast_state()

    lookback = _lookback()
    if len(rets) > lookback:
        rets = rets[-lookback:]

    retrain_every = max(1, int(getattr(config, "ARIMA_RETRAIN_EVERY_BARS", 5) or 5))
    need_retrain = (
        not _state.ok
        or _bars_since_retrain >= retrain_every
        or _state.forecast is None
    )
    if not need_retrain:
        # Refresh hybrid vol scale from latest GARCH state without re-fitting ARIMA.
        if _state.ok and _state.forecast is not None and _hybrid_enabled():
            mean_mult = float(_state.mean_mult or _map_forecast_to_mean_mult(_state.forecast))
            size, vol_scale, ratio, hybrid = _finalize_size_mult(mean_mult)
            _state = ArimaForecastState(
                ok=_state.ok,
                symbol=_state.symbol,
                forecast=_state.forecast,
                sign=_state.sign,
                size_mult=size,
                mean_mult=mean_mult,
                vol_scale=vol_scale,
                hybrid=hybrid,
                garch_ratio=ratio,
                order=_state.order,
                n_obs=_state.n_obs,
                reason=_state.reason,
            )
        _bars_since_retrain += 1
        return get_arima_forecast_state()

    result = fit_arima_mean_forecast(rets)
    if result is None:
        _state = ArimaForecastState(ok=False, reason="fit_failed", size_mult=1.0)
        return get_arima_forecast_state()

    forecast, order_label = result
    mean_mult = _map_forecast_to_mean_mult(forecast)
    size_mult, vol_scale, garch_ratio, hybrid = _finalize_size_mult(mean_mult)
    sign = 1 if forecast > 0 else (-1 if forecast < 0 else 0)
    sym = str(
        getattr(series, "name", None)
        or symbol
        or getattr(config, "ARIMA_SYMBOL", "SPY")
    )
    _state = ArimaForecastState(
        ok=True,
        symbol=str(sym).upper(),
        forecast=round(forecast, 8),
        sign=sign,
        size_mult=size_mult,
        mean_mult=mean_mult,
        vol_scale=vol_scale,
        hybrid=hybrid,
        garch_ratio=round(garch_ratio, 4) if garch_ratio is not None else None,
        order=order_label,
        n_obs=len(rets),
        reason="ok",
    )
    _bars_since_retrain = 0
    if sign > 0:
        _positive_days += 1
    return get_arima_forecast_state()


def arima_size_multiplier() -> float:
    if not config.effective_arima_enabled():
        return 1.0
    if not _state.ok:
        return 1.0
    return float(_state.size_mult or 1.0)


def arima_forecast_sign() -> int:
    if not config.effective_arima_enabled() or not _state.ok:
        return 0
    return int(_state.sign)


def format_arima_forecast_banner() -> str | None:
    if not getattr(config, "ARIMA_ENABLED", False):
        return None
    if not (
        config.effective_arima_enabled()
        or getattr(config, "PAPER_AGGRESSIVE_ENABLED", False)
        or getattr(config, "PAPER_TRADING", False)
    ):
        return None
    boost = _boost_mult()
    neg = _neg_mult()
    hybrid_on = bool(getattr(config, "ARIMA_GARCH_HYBRID", True))
    mode = "ARIMA–GARCH hybrid" if hybrid_on else "ARIMA mean"
    if _state.ok and _state.forecast is not None:
        extra = ""
        if hybrid_on:
            ratio_s = (
                f" σ̂/anc {_state.garch_ratio:.2f}"
                if _state.garch_ratio is not None
                else ""
            )
            extra = (
                f" | mean x{_state.mean_mult:.2f}×vol_scale {_state.vol_scale:.2f}"
                f"{ratio_s}"
            )
        return (
            f"{mode}: ON optional paper "
            f"(fc {_state.forecast:+.5f} sign{_state.sign:+d} → "
            f"size x{_state.size_mult:.2f}{extra} | boost {boost:.2f}/neg {neg:.2f}, "
            f"order {_state.order})"
        )
    return (
        f"{mode}: ON optional paper "
        f"(boost {boost:.2f}/neg {neg:.2f}, await fit)"
    )


def heartbeat_arima_forecast_payload() -> dict[str, Any] | None:
    if not config.effective_arima_enabled():
        return None
    st = get_arima_forecast_state()
    return {
        "enabled": True,
        "ok": bool(st.ok),
        "forecast": st.forecast,
        "sign": st.sign,
        "size_mult": st.size_mult,
        "mean_mult": st.mean_mult,
        "vol_scale": st.vol_scale,
        "hybrid": bool(st.hybrid),
        "garch_ratio": st.garch_ratio,
        "order": st.order,
        "positive_days": arima_positive_day_count(),
    }
