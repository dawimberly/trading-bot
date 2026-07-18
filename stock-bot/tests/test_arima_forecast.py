"""Quiet smoke for optional ARIMA / ARIMA–GARCH hybrid (no full backtest)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import config
from modules.arima_forecast import (
    arima_size_multiplier,
    combine_arima_garch_mult,
    fit_arima_mean_forecast,
    reset_arima_forecast_state,
    update_arima_forecast,
)


def test_fit_arima_mean_forecast_returns_float():
    rng = np.random.default_rng(42)
    # Mild AR(1)-ish series so ARIMA(1,0,1) has something to fit.
    rets = np.zeros(120)
    for i in range(1, len(rets)):
        rets[i] = 0.15 * rets[i - 1] + rng.normal(0, 0.01)
    result = fit_arima_mean_forecast(rets)
    assert result is not None
    forecast, order = result
    assert isinstance(forecast, float)
    assert np.isfinite(forecast)
    assert isinstance(order, str) and order


def test_combine_hybrid_scales_mean_when_garch_in_risk(monkeypatch):
    """GARCH already in risk → scale mean excess by vol_scale; no full GARCH multiply."""
    monkeypatch.setattr(config, "GARCH_VOL_RATIO_LOW", 0.85)
    monkeypatch.setattr(config, "GARCH_VOL_RATIO_HIGH", 1.35)
    monkeypatch.setattr(config, "ARIMA_HYBRID_MULT_MIN", 0.55)
    monkeypatch.setattr(config, "ARIMA_MULT_MAX", 1.15)
    monkeypatch.setattr(config, "ARIMA_HYBRID_VOL_SCALE_FLOOR", 0.0)

    # Calm vol → full boost
    size, scale = combine_arima_garch_mult(
        1.08, garch_ratio=0.80, garch_size_mult=1.0, garch_already_in_risk=True
    )
    assert scale == pytest.approx(1.0)
    assert size == pytest.approx(1.08, abs=1e-4)

    # High vol → neutralize excess (→ 1.0), even if garch size mult is 0.55
    size_hi, scale_hi = combine_arima_garch_mult(
        1.08, garch_ratio=1.50, garch_size_mult=0.55, garch_already_in_risk=True
    )
    assert scale_hi == pytest.approx(0.0)
    assert size_hi == pytest.approx(1.0, abs=1e-4)
    # Must NOT be 1.08 * 0.55 (that would double-apply GARCH)
    assert size_hi != pytest.approx(1.08 * 0.55, abs=1e-3)


def test_combine_hybrid_mean_times_vol_when_garch_not_in_risk(monkeypatch):
    monkeypatch.setattr(config, "ARIMA_HYBRID_MULT_MIN", 0.55)
    monkeypatch.setattr(config, "ARIMA_MULT_MAX", 1.15)
    size, scale = combine_arima_garch_mult(
        1.08, garch_ratio=1.20, garch_size_mult=0.70, garch_already_in_risk=False
    )
    assert size == pytest.approx(max(0.55, min(1.15, 1.08 * 0.70)), abs=1e-4)
    assert scale == pytest.approx(0.70)


def test_update_arima_forecast_boost_when_enabled(monkeypatch):
    reset_arima_forecast_state()
    monkeypatch.setattr(config, "ARIMA_ENABLED", True)
    monkeypatch.setattr(config, "ARIMA_LIVE_ENABLED", False)
    monkeypatch.setattr(config, "ARIMA_GARCH_HYBRID", False)  # mean-only path
    monkeypatch.setattr(config, "ARIMA_BOOST_MULT", 1.08)
    monkeypatch.setattr(config, "ARIMA_NEG_MULT", 1.0)
    monkeypatch.setattr(config, "ARIMA_MULT_MAX", 1.15)
    monkeypatch.setattr(config, "ARIMA_WINDOW", 80)
    monkeypatch.setattr(config, "ARIMA_RETRAIN_EVERY_BARS", 1)
    monkeypatch.setattr(config, "effective_arima_enabled", lambda: True)

    rng = np.random.default_rng(7)
    n = 100
    # Positive drift → often positive mean forecast
    rets = rng.normal(0.002, 0.008, size=n)
    prices = 100.0 * np.exp(np.cumsum(rets))
    data = pd.DataFrame({"SPY": prices})

    st = update_arima_forecast(data, symbol="SPY")
    assert st.ok
    assert st.forecast is not None
    assert st.sign in (-1, 0, 1)
    assert st.hybrid is False
    mult = arima_size_multiplier()
    assert isinstance(mult, float)
    if st.forecast > 0:
        assert mult == pytest.approx(1.08, abs=1e-4)
    else:
        assert mult == pytest.approx(1.0, abs=1e-4)


def test_update_hybrid_damps_boost_in_high_vol(monkeypatch):
    reset_arima_forecast_state()
    monkeypatch.setattr(config, "ARIMA_ENABLED", True)
    monkeypatch.setattr(config, "ARIMA_GARCH_HYBRID", True)
    monkeypatch.setattr(config, "ARIMA_BOOST_MULT", 1.08)
    monkeypatch.setattr(config, "ARIMA_NEG_MULT", 1.0)
    monkeypatch.setattr(config, "ARIMA_MULT_MAX", 1.15)
    monkeypatch.setattr(config, "ARIMA_HYBRID_MULT_MIN", 0.55)
    monkeypatch.setattr(config, "ARIMA_WINDOW", 80)
    monkeypatch.setattr(config, "ARIMA_RETRAIN_EVERY_BARS", 1)
    monkeypatch.setattr(config, "effective_arima_enabled", lambda: True)
    monkeypatch.setattr(config, "effective_garch_vol_enabled", lambda: True)
    monkeypatch.setattr(config, "GARCH_VOL_RATIO_LOW", 0.85)
    monkeypatch.setattr(config, "GARCH_VOL_RATIO_HIGH", 1.35)

    # Stub GARCH state: elevated vol
    from modules import garch_vol

    monkeypatch.setattr(
        garch_vol,
        "get_garch_vol_state",
        lambda: garch_vol.GarchVolState(
            ok=True, ratio=1.50, size_mult=0.55, reason="ok"
        ),
    )

    rng = np.random.default_rng(7)
    rets = rng.normal(0.002, 0.008, size=100)
    prices = 100.0 * np.exp(np.cumsum(rets))
    data = pd.DataFrame({"SPY": prices})

    st = update_arima_forecast(data, symbol="SPY")
    assert st.ok
    assert st.hybrid is True
    if st.forecast is not None and st.forecast > 0:
        # High vol → mean excess neutralized (size ~1.0), not 1.08*0.55
        assert st.mean_mult == pytest.approx(1.08, abs=1e-4)
        assert st.vol_scale == pytest.approx(0.0, abs=1e-4)
        assert st.size_mult == pytest.approx(1.0, abs=1e-4)


def test_arima_disabled_returns_neutral(monkeypatch):
    reset_arima_forecast_state()
    monkeypatch.setattr(config, "ARIMA_ENABLED", False)
    monkeypatch.setattr(config, "effective_arima_enabled", lambda: False)
    assert arima_size_multiplier() == 1.0
