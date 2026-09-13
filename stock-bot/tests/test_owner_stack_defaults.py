"""Owner stack defaults: paper 12 NYSE trades/cycle, vti_core 0, no env keys required."""

from __future__ import annotations

import config


def test_effective_max_equity_trades_paper_12():
    was = config.paper_aggressive_context()
    saved = config.PAPER_MAX_EQUITY_TRADES
    try:
        config.set_paper_aggressive_context(True)
        config.PAPER_MAX_EQUITY_TRADES = 12
        assert config.effective_max_equity_trades() == 12
        config.set_paper_aggressive_context(False)
        assert config.effective_max_equity_trades() == 1
    finally:
        config.PAPER_MAX_EQUITY_TRADES = saved
        config.set_paper_aggressive_context(was)


def test_vti_core_zero_under_paper_lock_without_env_keys():
    was = config.paper_aggressive_context()
    dyn = config.PAPER_DYNAMIC_VTI_ENABLED
    core = config.PAPER_VTI_CORE_PCT
    try:
        config.set_paper_aggressive_context(True)
        config.PAPER_DYNAMIC_VTI_ENABLED = False
        config.PAPER_VTI_CORE_PCT = 0.0
        assert config.vti_core_allocation_pct() == 0.0
        assert config.vti_core_enabled() is False
    finally:
        config.PAPER_DYNAMIC_VTI_ENABLED = dyn
        config.PAPER_VTI_CORE_PCT = core
        config.set_paper_aggressive_context(was)


def test_enforce_empty_env_does_not_turn_dyn_vti_or_stat_arb_on():
    saved_explicit = config._env_explicit
    dyn = config.PAPER_DYNAMIC_VTI_ENABLED
    core = config.PAPER_VTI_CORE_PCT
    trades = config.PAPER_MAX_EQUITY_TRADES
    sa = config.PAPER_STAT_ARB_ENABLED
    try:
        config._env_explicit = lambda *keys: False  # type: ignore[method-assign]
        config.enforce_realistic_research_profile()
        assert config.PAPER_DYNAMIC_VTI_ENABLED is False
        assert float(config.PAPER_VTI_CORE_PCT) == 0.0
        assert int(config.PAPER_MAX_EQUITY_TRADES) == 12
        assert config.PAPER_STAT_ARB_ENABLED is False
        config.set_paper_aggressive_context(True)
        assert config.vti_core_allocation_pct() == 0.0
        assert config.effective_max_equity_trades() == 12
    finally:
        config._env_explicit = saved_explicit
        config.PAPER_DYNAMIC_VTI_ENABLED = dyn
        config.PAPER_VTI_CORE_PCT = core
        config.PAPER_MAX_EQUITY_TRADES = trades
        config.PAPER_STAT_ARB_ENABLED = sa
        config.set_paper_aggressive_context(False)
