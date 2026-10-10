"""Retired SPY sleeve: no dedicated cap/fill; SPY stays a regular NYSE name."""

from __future__ import annotations

from types import SimpleNamespace

import config
from modules.alpaca_executor import AlpacaExecutor
from modules import holdings_rebalance as hb
from modules.holdings_rebalance import _deploy_spy, rebalance_to_targets
from modules.holdings_reconcile import trim_over_cap_sleeves
from modules.paper_risk_controls import _is_tactical_long, _per_name_cap_exempt
from modules.pipeline_strategies import (
    _is_nyse_momentum_position,
    _spy_buy_intent,
    run_spy_strategy,
)


def test_env_zero_disables_spy_sleeve():
    assert float(config.SPY_SLEEVE_CAP_PCT) == 0.0
    assert config.spy_sleeve_enabled() is False
    assert config.effective_sleeve_cap(config.SPY_SLEEVE_CAP_PCT, sleeve="spy") == 0.0
    assert config.fund_allocation_pct()["spy"] == 0.0


def test_paper_hard_cap_zero_blocks_even_if_base_set(monkeypatch):
    monkeypatch.setattr(config, "SPY_SLEEVE_CAP_PCT", 0.45)
    monkeypatch.setattr(config, "PAPER_SPY_MAX_EXPOSURE_PCT", 0.0)
    monkeypatch.setattr(config, "PAPER_TRADING", True)
    assert config.spy_sleeve_enabled() is False
    assert config.effective_sleeve_cap(0.45, sleeve="spy") == 0.0


def test_dynamic_caps_cannot_revive_retired_spy(monkeypatch):
    monkeypatch.setattr(config, "SPY_SLEEVE_CAP_PCT", 0.0)
    monkeypatch.setattr(config, "PAPER_SPY_MAX_EXPOSURE_PCT", 0.0)
    monkeypatch.setattr(config, "PAPER_TRADING", True)
    ex = AlpacaExecutor.__new__(AlpacaExecutor)
    ex._dynamic_sleeve_caps = {"spy": 0.27}
    ex._get_account = lambda: SimpleNamespace(equity=100_000.0, cash=5_000.0)
    ex._current_regime = None
    ex.pod_risk_scale = lambda _k: 1.0
    assert ex._sleeve_cap_pct("spy", 0.45) == 0.0


def test_spy_sleeve_buy_paths_noop_when_retired(monkeypatch):
    monkeypatch.setattr(config, "SPY_SLEEVE_CAP_PCT", 0.0)
    monkeypatch.setattr(config, "PAPER_TRADING", True)
    assert _spy_buy_intent(None, "RHYME_D: Calm", None, {}) is False
    assert run_spy_strategy(None, None, "RHYME_D: Calm", None, {}) == 0
    assert _deploy_spy(None, None, "RHYME_D: Calm", 10_000.0, dry_run=True) == []


def test_spy_is_nyse_name_when_sleeve_retired(monkeypatch):
    monkeypatch.setattr(config, "SPY_SLEEVE_CAP_PCT", 0.0)
    monkeypatch.setattr(config, "PAPER_SPY_MAX_EXPOSURE_PCT", 0.0)
    monkeypatch.setattr(config, "PAPER_TRADING", True)
    assert config._nyse_eligible_symbol("SPY") is True
    assert _is_nyse_momentum_position("SPY") is True
    pos = SimpleNamespace(symbol="SPY")
    assert AlpacaExecutor._is_spy_position(pos) is False
    assert AlpacaExecutor._is_nyse_sleeve_position(pos) is True
    ex = AlpacaExecutor.__new__(AlpacaExecutor)
    assert ex._infer_sleeve("SPY") == "NYSE"
    assert ex._is_core_exempt("SPY") is False
    assert _per_name_cap_exempt("SPY") is False
    assert _is_tactical_long("SPY") is True


def test_spy_stays_own_sleeve_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "SPY_SLEEVE_CAP_PCT", 0.45)
    monkeypatch.setattr(config, "PAPER_SPY_MAX_EXPOSURE_PCT", 0.46)
    monkeypatch.setattr(config, "PAPER_TRADING", False)
    monkeypatch.setattr(config, "PAPER_AGGRESSIVE_ENABLED", False)
    monkeypatch.setattr(config, "_paper_aggressive_ctx", False)
    assert config.spy_sleeve_enabled() is True
    assert config._nyse_eligible_symbol("SPY") is False
    assert _is_nyse_momentum_position("SPY") is False
    pos = SimpleNamespace(symbol="SPY")
    assert AlpacaExecutor._is_spy_position(pos) is True
    assert AlpacaExecutor._is_nyse_sleeve_position(pos) is False


def test_rebalance_does_not_liquidate_leftover_spy(monkeypatch):
    monkeypatch.setattr(config, "SPY_SLEEVE_CAP_PCT", 0.0)
    monkeypatch.setattr(config, "PAPER_SPY_MAX_EXPOSURE_PCT", 0.0)
    monkeypatch.setattr(config, "PAPER_TRADING", True)

    spy_pos = SimpleNamespace(
        symbol="SPY",
        qty=50,
        current_price=400.0,
        avg_entry_price=400.0,
        market_value=20_000.0,
    )
    account = SimpleNamespace(equity=100_000.0, cash=5_000.0)
    ex = AlpacaExecutor.__new__(AlpacaExecutor)
    ex.client = SimpleNamespace(
        get_account=lambda: account,
        get_all_positions=lambda: [spy_pos],
    )
    ex.spy_sleeve_value = lambda: 20_000.0
    ex.crypto_sleeve_value = lambda: 0.0
    ex.nyse_sleeve_value = lambda: 0.0
    ex.sleeve_snapshot = lambda: {
        "spy_value": 20_000.0,
        "spy_cap": 0.0,
        "crypto_value": 0.0,
        "crypto_cap": 0.0,
        "nyse_value": 0.0,
        "nyse_cap": 0.0,
    }
    ex.execute_full_exit = lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("must not liquidate leftover SPY as a retired sleeve")
    )
    ex.execute_reduce_notional = lambda *_a, **_k: SimpleNamespace(id="DRY")
    ex.execute_order = lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("sleeve rebalance must not buy SPY")
    )
    monkeypatch.setattr(hb, "_deploy_nyse", lambda *_a, **_k: [])

    assert trim_over_cap_sleeves(ex) == []
    result = rebalance_to_targets(
        ex,
        None,
        regime="RHYME_D: Calm",
        volatility="Low",
        market_open=True,
        dry_run=False,
        should_rebuild_ledger=False,
    )
    spy_actions = [a for a in result["actions"] if a.get("sleeve") == "spy"]
    assert spy_actions == []
