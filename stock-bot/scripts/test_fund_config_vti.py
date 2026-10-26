"""Unit tests for paper-aggressive dynamic VTI tiers (fund_config.get_vti_core_pct)."""

from __future__ import annotations

import sys
from pathlib import Path
from contextlib import contextmanager
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from modules.fund_config import get_vti_core_pct  # noqa: E402


@contextmanager
def _paper_agg_env():
    with mock.patch.multiple(
        "modules.fund_config",
        _PAPER_SMALL_VTI=0.90,
        _VTI_STRESS=0.75,
        _VTI_CALM=0.50,
        _VTI_DEFAULT_AGGRESSIVE=0.65,
        _VTI_VOL_STRESS=0.025,
        _VTI_VOL_CALM=0.015,
        _VTI_HARD_FLOOR=0.40,
    ):
        with mock.patch.multiple(
            config,
            PAPER_TRADING=True,
            PAPER_DYNAMIC_VTI_ENABLED=True,
            PAPER_AGGRESSIVE_ENABLED=True,
            paper_aggressive_context=lambda: True,
            live_conservative_profile_active=lambda: False,
            SMALL_ACCOUNT_EQUITY_THRESHOLD=500.0,
            PAPER_VTI_CORE_PCT=0.80,
            VTI_CORE_PCT=0.80,
            SMALL_ACCOUNT_VTI_CORE_PCT=0.85,
            LIVE_VTI_CORE_PCT=0.85,
            DYNAMIC_VTI_STRESS_PCT=0.75,
            DYNAMIC_VTI_CALM_PCT=0.50,
            DYNAMIC_VTI_DEFAULT_PCT=0.65,
            DYNAMIC_VTI_PAPER_FLOOR=0.40,
            DYNAMIC_VTI_VOL_STRESS=0.025,
            DYNAMIC_VTI_VOL_CALM=0.015,
        ):
            yield


def test_small_account_paper_aggressive() -> None:
    with _paper_agg_env():
        assert get_vti_core_pct(400, is_paper_aggressive=True) == 0.90


def test_stress_tier() -> None:
    with _paper_agg_env():
        assert get_vti_core_pct(10_000, vol_score=0.03, is_paper_aggressive=True) == 0.75
        assert get_vti_core_pct(10_000, macro_stress=True, is_paper_aggressive=True) == 0.75


def test_calm_tier() -> None:
    with _paper_agg_env():
        assert get_vti_core_pct(10_000, vol_score=0.012, is_paper_aggressive=True) == 0.50


def test_default_aggressive_tier() -> None:
    with _paper_agg_env():
        assert get_vti_core_pct(10_000, vol_score=0.020, is_paper_aggressive=True) == 0.65


def test_hard_floor_never_below_40() -> None:
    with _paper_agg_env():
        with mock.patch.object(config, "DYNAMIC_VTI_CALM_PCT", 0.30):
            assert get_vti_core_pct(10_000, vol_score=0.012, is_paper_aggressive=True) == 0.40


def test_live_stays_static() -> None:
    with mock.patch.multiple(
        config,
        PAPER_TRADING=False,
        paper_aggressive_context=lambda: False,
        VTI_CORE_PCT=0.80,
        SMALL_ACCOUNT_EQUITY_THRESHOLD=500.0,
    ):
        assert get_vti_core_pct(10_000, vol_score=0.012, is_paper_aggressive=False) == 0.80


def test_dynamic_disabled_uses_fixed_paper() -> None:
    with mock.patch.multiple(
        config,
        PAPER_TRADING=True,
        PAPER_DYNAMIC_VTI_ENABLED=False,
        paper_aggressive_context=lambda: True,
        PAPER_VTI_CORE_PCT=0.80,
        SMALL_ACCOUNT_EQUITY_THRESHOLD=500.0,
    ):
        assert get_vti_core_pct(10_000, vol_score=0.012, is_paper_aggressive=True) == 0.80


if __name__ == "__main__":
    test_small_account_paper_aggressive()
    test_stress_tier()
    test_calm_tier()
    test_default_aggressive_tier()
    test_hard_floor_never_below_40()
    test_live_stays_static()
    test_dynamic_disabled_uses_fixed_paper()
    print("fund_config VTI tests passed")
