"""Draft paper VTI cash-need skip helper. Flag stays OFF — no live/paper orders."""

from __future__ import annotations

import config
from modules.vti_core import paper_vti_reduce_skip_reason


def test_flag_default_off():
    assert getattr(config, "PAPER_VTI_CASH_NEED_SKIP", None) is False


def test_off_identical_to_today():
    assert (
        paper_vti_reduce_skip_reason(
            vti_pct=0.83, cash=30000, reduce_notional=8000, enabled=False
        )
        is None
    )


def test_on_83_pct_vti_cap_allows():
    # 83% VTI + $30k cash, $8k reduce → allow (cap; never cash_unused)
    assert (
        paper_vti_reduce_skip_reason(
            vti_pct=0.83, cash=30000, reduce_notional=8000, enabled=True
        )
        is None
    )


def test_on_50_pct_vti_cash_unused_skips():
    # 50% VTI + $30k cash, $8k reduce → skip cash_unused
    assert (
        paper_vti_reduce_skip_reason(
            vti_pct=0.50, cash=30000, reduce_notional=8000, enabled=True
        )
        == "cash_unused"
    )


def test_on_50_pct_vti_cash_tight_allows():
    # 50% VTI + $2k cash, $5k reduce → allow
    assert (
        paper_vti_reduce_skip_reason(
            vti_pct=0.50, cash=2000, reduce_notional=5000, enabled=True
        )
        is None
    )


def test_on_38_pct_vti_floor_skips():
    # 38% VTI → skip floor
    assert (
        paper_vti_reduce_skip_reason(
            vti_pct=0.38, cash=30000, reduce_notional=8000, enabled=True
        )
        == "floor"
    )
