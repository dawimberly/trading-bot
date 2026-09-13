"""Live leftover NYSE must not share SPY's full yield gate."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pandas as pd

import config
from modules.alpaca_executor import AlpacaExecutor
from modules import pipeline_strategies as ps

MILD = "RHYME_D: Range_Bound_Neutral"
HARD = "RHYME_E: Steady_Bearish_Decline"


def _live_book(monkeypatch):
    monkeypatch.setattr(config, "PAPER_TRADING", False)
    monkeypatch.setattr(config, "PAPER_YIELD_GATE_OVERRIDE", False)


def test_live_mild_yield_blocks_spy_not_nyse_when_vti_core_on(monkeypatch):
    _live_book(monkeypatch)
    monkeypatch.setattr(config, "vti_core_enabled", lambda: True)
    assert config.effective_yield_gate(True, regime=MILD, sleeve="spy") is True
    assert config.effective_yield_gate(True, regime=MILD) is True
    assert config.effective_yield_gate(True, regime=MILD, sleeve="nyse") is False


def test_live_mild_yield_blocks_nyse_when_vti_core_off(monkeypatch):
    _live_book(monkeypatch)
    monkeypatch.setattr(config, "vti_core_enabled", lambda: False)
    assert config.effective_yield_gate(True, regime=MILD, sleeve="spy") is True
    assert config.effective_yield_gate(True, regime=MILD, sleeve="nyse") is True


def test_live_hard_regime_blocks_nyse_and_spy(monkeypatch):
    _live_book(monkeypatch)
    assert config.effective_yield_gate(True, regime=HARD, sleeve="nyse") is True
    assert config.effective_yield_gate(True, regime=HARD, sleeve="spy") is True


def test_paper_override_still_softens_all_sleeves_when_vti_core_on(monkeypatch):
    monkeypatch.setattr(config, "PAPER_TRADING", True)
    monkeypatch.setattr(config, "PAPER_YIELD_GATE_OVERRIDE", True)
    monkeypatch.setattr(config, "paper_aggressive_context", lambda: True)
    monkeypatch.setattr(config, "vti_core_enabled", lambda: True)
    assert config.effective_yield_gate(True, regime=MILD, sleeve="spy") is False
    assert config.effective_yield_gate(True, regime=MILD, sleeve="nyse") is False
    assert config.effective_yield_gate(True, regime=HARD, sleeve="nyse") is True


def test_paper_override_does_not_soften_nyse_when_vti_core_off(monkeypatch):
    monkeypatch.setattr(config, "PAPER_TRADING", True)
    monkeypatch.setattr(config, "PAPER_YIELD_GATE_OVERRIDE", True)
    monkeypatch.setattr(config, "paper_aggressive_context", lambda: True)
    monkeypatch.setattr(config, "vti_core_enabled", lambda: False)
    assert config.effective_yield_gate(True, regime=MILD, sleeve="nyse") is True
    assert config.effective_yield_gate(True, regime=MILD, sleeve="spy") is False


def test_summarize_live_mild_yield_is_not_yield_gated_when_vti_core_on(monkeypatch):
    _live_book(monkeypatch)
    monkeypatch.setattr(config, "vti_core_enabled", lambda: True)
    monkeypatch.setattr(config, "effective_cofire_budget_enabled", lambda: False)
    monkeypatch.setattr(ps, "regime_entries_paused", lambda *a, **k: False)
    reason = ps.summarize_entry_skip_reason(
        pd.DataFrame({"SPY": [100.0, 101.0]}),
        SimpleNamespace(),
        MILD,
        datetime(2026, 8, 17, 10, 0, 0),
        {},
        yield_gated=True,
        market_open=True,
    )
    assert reason != "yield_gated"


def test_summarize_live_mild_yield_is_yield_gated_when_vti_core_off(monkeypatch):
    _live_book(monkeypatch)
    monkeypatch.setattr(config, "vti_core_enabled", lambda: False)
    monkeypatch.setattr(config, "effective_cofire_budget_enabled", lambda: False)
    monkeypatch.setattr(ps, "regime_entries_paused", lambda *a, **k: False)
    reason = ps.summarize_entry_skip_reason(
        pd.DataFrame({"SPY": [100.0, 101.0]}),
        SimpleNamespace(),
        MILD,
        datetime(2026, 8, 17, 10, 0, 0),
        {},
        yield_gated=True,
        market_open=True,
    )
    assert reason == "yield_gated"


def test_summarize_live_hard_regime_is_yield_gated(monkeypatch):
    _live_book(monkeypatch)
    monkeypatch.setattr(config, "effective_cofire_budget_enabled", lambda: False)
    monkeypatch.setattr(ps, "regime_entries_paused", lambda *a, **k: False)
    reason = ps.summarize_entry_skip_reason(
        pd.DataFrame({"SPY": [100.0, 101.0]}),
        SimpleNamespace(),
        HARD,
        datetime(2026, 8, 17, 10, 0, 0),
        {},
        yield_gated=True,
        market_open=True,
    )
    assert reason == "yield_gated"


def test_run_equity_strategy_passes_yield_check_on_live_mild_when_vti_core_on(monkeypatch):
    _live_book(monkeypatch)
    monkeypatch.setattr(config, "vti_core_enabled", lambda: True)
    monkeypatch.setattr(ps, "regime_entries_paused", lambda *a, **k: False)
    called = {"ranked": False}

    def fake_ranked(*args, **kwargs):
        called["ranked"] = True
        return []

    monkeypatch.setattr(ps, "_nyse_equity_columns", lambda data: ["AAPL"])
    monkeypatch.setattr(ps, "_equity_momentum_ranked", fake_ranked)
    n = ps.run_equity_strategy(
        pd.DataFrame(),
        executor=SimpleNamespace(),
        regime=MILD,
        now=datetime(2026, 8, 17, 10, 0, 0),
        pair_cooldown={},
        yield_gated=True,
    )
    assert called["ranked"] is True
    assert n == 0


def test_run_equity_strategy_blocked_on_live_mild_when_vti_core_off(monkeypatch):
    _live_book(monkeypatch)
    monkeypatch.setattr(config, "vti_core_enabled", lambda: False)
    monkeypatch.setattr(ps, "regime_entries_paused", lambda *a, **k: False)
    called = {"ranked": False}

    def fake_ranked(*args, **kwargs):
        called["ranked"] = True
        return []

    monkeypatch.setattr(ps, "_nyse_equity_columns", lambda data: ["AAPL"])
    monkeypatch.setattr(ps, "_equity_momentum_ranked", fake_ranked)
    n = ps.run_equity_strategy(
        pd.DataFrame(),
        executor=SimpleNamespace(),
        regime=MILD,
        now=datetime(2026, 8, 17, 10, 0, 0),
        pair_cooldown={},
        yield_gated=True,
    )
    assert called["ranked"] is False
    assert n == 0


def test_live_nyse_wisdom_half_size_floors_to_min(monkeypatch):
    _live_book(monkeypatch)
    ex = AlpacaExecutor.__new__(AlpacaExecutor)
    ex._wisdom_sizing_multiplier = 0.5
    ex._min_notional = lambda: 5.0
    # 1.8% of ~$308, then ×0.5 wisdom would be ~$2.77 < $5 min.
    assert ex._apply_sizing_multiplier(5.54, sleeve_key="nyse") == 5.0
    assert ex._apply_sizing_multiplier(5.54, sleeve_key="spy") is None
    assert ex._apply_sizing_multiplier(4.0, sleeve_key="nyse") is None


def test_live_same_day_reentry_blocks_after_sell(monkeypatch):
    _live_book(monkeypatch)
    monkeypatch.setattr(config, "LIVE_NYSE_SAME_DAY_REENTRY_BLOCK", True)
    monkeypatch.setattr(config, "paper_aggressive_context", lambda: False)
    monkeypatch.setattr(ps, "_nyse_journal_sold_today", ps._nyse_session_sold_today)
    ps.reset_nyse_entry_hygiene_state()
    now = datetime(2026, 8, 17, 13, 30, 0)
    assert config.effective_nyse_same_day_reentry_block() is True
    assert (
        ps._nyse_entry_hygiene_skip(SimpleNamespace(), "SPCX", now=now) is None
    )
    ps.mark_nyse_sold_today("SPCX", now=now)
    reason = ps._nyse_entry_hygiene_skip(SimpleNamespace(), "SPCX", now=now)
    assert reason == "same-day reentry block (sold earlier today)"
    assert ps._nyse_entry_hygiene_skip(SimpleNamespace(), "NUE", now=now) is None


def test_live_same_day_reentry_can_be_disabled(monkeypatch):
    _live_book(monkeypatch)
    monkeypatch.setattr(config, "LIVE_NYSE_SAME_DAY_REENTRY_BLOCK", False)
    monkeypatch.setattr(config, "paper_aggressive_context", lambda: False)
    monkeypatch.setattr(ps, "_nyse_journal_sold_today", ps._nyse_session_sold_today)
    ps.reset_nyse_entry_hygiene_state()
    now = datetime(2026, 8, 17, 13, 30, 0)
    ps.mark_nyse_sold_today("SPCX", now=now)
    assert ps._nyse_entry_hygiene_skip(SimpleNamespace(), "SPCX", now=now) is None
