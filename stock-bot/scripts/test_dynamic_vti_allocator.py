"""Unit tests for smart Dynamic VTI allocator."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from modules.dynamic_vti_allocator import (  # noqa: E402
    VtiAllocatorContext,
    build_vti_allocator_context,
    compute_smart_vti_core_pct,
    format_dynamic_vti_banner,
    spy_like_size_boost,
)


def _price_frame() -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=30, freq="D")
    vti = pd.Series([100 + i for i in range(30)], index=idx, dtype=float)
    spy = pd.Series([100 + i * 0.6 for i in range(30)], index=idx, dtype=float)
    gld = pd.Series([100 + i * 0.8 for i in range(30)], index=idx, dtype=float)
    stocks = pd.DataFrame(
        {f"S{i}": pd.Series([100 + i + j for j in range(30)], index=idx, dtype=float) for i in range(5)},
        index=idx,
    )
    return pd.concat([vti.rename("VTI"), spy.rename("SPY"), gld.rename("GLD"), stocks], axis=1)


def test_active_signals_lower_vti_than_stress_baseline() -> None:
    calm = VtiAllocatorContext(vol_score=0.012, volatility="Low", macro_stress=False)
    stressed = VtiAllocatorContext(
        vol_score=0.03, volatility="High", macro_stress=True, bubble_score_100=85.0
    )
    active = VtiAllocatorContext(
        vol_score=0.012,
        volatility="Low",
        macro_stress=False,
        nyse_momentum=0.06,
        metal_momentum=0.05,
        insider_cluster_buys=2,
        bubble_score_100=30.0,
        regime_conviction=0.75,
        vti_vs_spy_momentum=-0.03,
    )
    calm_pct = compute_smart_vti_core_pct(10_000, calm).pct
    stress_pct = compute_smart_vti_core_pct(10_000, stressed).pct
    active_pct = compute_smart_vti_core_pct(10_000, active).pct
    assert active_pct < calm_pct
    assert stress_pct > calm_pct


def test_banner_includes_drivers() -> None:
    decision = compute_smart_vti_core_pct(
        10_000,
        VtiAllocatorContext(
            macro_stress=False,
            nyse_momentum=0.06,
            bubble_score_100=32.0,
        ),
    )
    banner = format_dynamic_vti_banner(decision.pct, decision.drivers)
    assert "Dynamic VTI" in banner
    assert "%" in banner


def test_build_context_from_data() -> None:
    data = _price_frame()
    ctx = build_vti_allocator_context(
        data=data,
        regime="RHYME_C: Steady_Bullish_Growth",
        vol_score=0.012,
        volatility="Low",
        insider_state={"cluster_count": 1, "short_count": 0},
    )
    assert ctx.nyse_momentum is not None
    assert ctx.metal_momentum is not None
    assert ctx.insider_cluster_buys == 1


def test_optional_floor_reduces_on_spy_like_strength() -> None:
    with patch.object(config, "effective_dynamic_vti_optional", return_value=True), patch.object(
        config, "effective_dynamic_vti_allow_zero", return_value=True
    ):
        base = config.resolve_dynamic_vti_floor(0.0)
        reduced = config.resolve_dynamic_vti_floor(0.65)
        zero = config.resolve_dynamic_vti_floor(0.90)
    assert base == float(config.DYNAMIC_VTI_PAPER_FLOOR)
    assert reduced == float(config.DYNAMIC_VTI_FLOOR_MIN)
    assert zero == 0.0

    strong = VtiAllocatorContext(
        vol_score=0.012,
        volatility="Low",
        macro_stress=False,
        nyse_momentum=0.06,
        spy_like_strength=0.90,
        bubble_score_100=30.0,
        regime_conviction=0.75,
    )
    with patch.object(config, "effective_dynamic_vti_optional", return_value=True), patch.object(
        config, "effective_dynamic_vti_allow_zero", return_value=True
    ):
        decision = compute_smart_vti_core_pct(10_000, strong)
    assert decision.floor == 0.0
    assert decision.pct < float(config.DYNAMIC_VTI_PAPER_FLOOR)


def test_spy_like_boost_requires_confluence() -> None:
    with patch.object(config, "effective_spy_like_boost_enabled", return_value=True), patch(
        "modules.dynamic_vti_allocator.score_spy_like_confluence",
        return_value={
            "symbol": "AAPL",
            "flags": {"rvol": True, "orb": True, "catalyst": True, "insider": False},
            "hits": 3,
            "strength": 0.75,
            "confluent": True,
        },
    ):
        mult = spy_like_size_boost("AAPL", data=None)
    assert 1.05 <= mult <= 1.20

    with patch.object(config, "effective_spy_like_boost_enabled", return_value=True), patch(
        "modules.dynamic_vti_allocator.score_spy_like_confluence",
        return_value={
            "symbol": "AAPL",
            "flags": {"rvol": True, "orb": False, "catalyst": False, "insider": False},
            "hits": 1,
            "strength": 0.25,
            "confluent": False,
        },
    ):
        assert spy_like_size_boost("AAPL", data=None) == 1.0


if __name__ == "__main__":
    test_active_signals_lower_vti_than_stress_baseline()
    test_banner_includes_drivers()
    test_build_context_from_data()
    test_optional_floor_reduces_on_spy_like_strength()
    test_spy_like_boost_requires_confluence()
    print("dynamic_vti_allocator tests passed")
