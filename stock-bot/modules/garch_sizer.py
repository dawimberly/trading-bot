"""Paper-only GARCH(1,1) volatility-target position sizing.

Gate: PAPER_GARCH_SIZING=true and PAPER_TRADING=true.
On any failure, callers should treat the returned multiplier as 1.0
(get_multiplier already falls back to 1.0).

This is separate from modules/garch_vol.py (existing Realistic Research path).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_DEFAULT_TARGET = 0.15
_DEFAULT_MIN = 0.5
_DEFAULT_MAX = 1.5
_MIN_OBS = 60


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def paper_garch_sizing_enabled() -> bool:
    """True only when explicitly enabled and running in paper mode."""
    if not _env_bool("PAPER_GARCH_SIZING", False):
        return False
    # Paper only — refuse live
    if not _env_bool("PAPER_TRADING", True):
        return False
    allow_live = (os.getenv("ALLOW_LIVE_TRADING") or "").strip().lower()
    if allow_live == "yes" and not _env_bool("PAPER_TRADING", True):
        return False
    return True


def target_vol() -> float:
    return max(0.01, _env_float("GARCH_TARGET_VOL", _DEFAULT_TARGET))


def multiplier_bounds() -> tuple[float, float]:
    lo = max(0.1, _env_float("GARCH_MIN_MULTIPLIER", _DEFAULT_MIN))
    hi = max(lo, _env_float("GARCH_MAX_MULTIPLIER", _DEFAULT_MAX))
    return lo, hi


def _to_price_series(prices: Any) -> pd.Series | None:
    if prices is None:
        return None
    if isinstance(prices, pd.DataFrame):
        if "Close" in prices.columns:
            s = prices["Close"]
        else:
            s = prices.iloc[:, 0]
    else:
        s = pd.Series(prices) if not isinstance(prices, pd.Series) else prices
    s = pd.to_numeric(s, errors="coerce").dropna()
    if len(s) < _MIN_OBS:
        return None
    return s.astype(float)


def _daily_closes(prices: pd.Series) -> pd.Series:
    """Ensure daily frequency (last close per calendar day)."""
    if not isinstance(prices.index, pd.DatetimeIndex):
        return prices
    try:
        daily = prices.resample("1D").last().dropna()
        if len(daily) >= _MIN_OBS:
            return daily
    except Exception:
        pass
    return prices


def get_multiplier(price_series: Any) -> float:
    """Fit GARCH(1,1) and return vol-target size multiplier in [min, max].

    On any error or when disabled, returns 1.0 (base size).
    """
    if not paper_garch_sizing_enabled():
        return 1.0
    try:
        series = _to_price_series(price_series)
        if series is None:
            return 1.0
        series = _daily_closes(series)
        returns = series.pct_change().dropna()
        if len(returns) < _MIN_OBS:
            return 1.0

        from arch import arch_model

        # Decimal returns; rescale=False so variance stays in decimal^2 units
        model = arch_model(returns, vol="Garch", p=1, q=1, rescale=False)
        result = model.fit(disp="off")
        forecast = result.forecast(horizon=1)
        var = float(forecast.variance.values[-1][0])
        if not np.isfinite(var) or var <= 0:
            return 1.0
        predicted_vol = float(np.sqrt(var) * np.sqrt(252))
        if predicted_vol <= 1e-12:
            return 1.0

        mult = target_vol() / predicted_vol
        lo, hi = multiplier_bounds()
        mult = float(np.clip(mult, lo, hi))
        if not np.isfinite(mult):
            return 1.0
        return mult
    except Exception as exc:
        logger.debug("garch_sizer.get_multiplier fallback to 1.0: %s", exc)
        return 1.0


def spy_series_from_data(data: Any) -> pd.Series | None:
    """Extract SPY (or VTI) daily closes from a wide price DataFrame."""
    if data is None or getattr(data, "empty", True):
        return None
    for key in ("SPY", "SPY-USD", "VTI", "VTI-USD"):
        if key in getattr(data, "columns", []):
            return _to_price_series(data[key])
    return None
