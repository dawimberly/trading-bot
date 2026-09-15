"""Weekly VTI resize cadence for Live + Medium."""

from __future__ import annotations

from types import SimpleNamespace

import config
from modules import vti_core


class _FakeEx:
    def __init__(self, equity=1000.0, cash=400.0, vti_value=200.0):
        self.equity = equity
        self.cash = cash
        self.vti_value = vti_value
        self.orders = []

    def _get_account(self):
        return SimpleNamespace(equity=self.equity, cash=self.cash)

    def _find_position(self, symbol):
        if symbol == "VTI" and self.vti_value > 0:
            return SimpleNamespace(market_value=self.vti_value)
        return None

    def _position_market_value(self, pos):
        return float(pos.market_value)

    def execute_order(self, symbol, side, notional=None):
        self.orders.append(("buy", symbol, notional))
        return {"id": "1"}

    def execute_reduce_notional(self, symbol, notional):
        self.orders.append(("sell", symbol, notional))
        return {"id": "2"}

    def order_filled(self, order, max_wait=3.0):
        return True

    def refresh_cache(self):
        pass


def test_weekly_skips_second_call_same_week(monkeypatch, tmp_path):
    state = tmp_path / "vti_rebalance_weekly.json"
    monkeypatch.setattr(config, "vti_core_enabled", lambda: True)
    monkeypatch.setattr(config, "effective_vti_rebalance_cadence", lambda: "weekly")
    monkeypatch.setattr(config, "vti_core_allocation_pct", lambda **kw: 0.33)
    monkeypatch.setattr(config, "effective_min_notional", lambda eq: 1.0)
    monkeypatch.setattr(config, "effective_vti_rebalance_drift_pct", lambda: 0.02)
    monkeypatch.setattr(config, "paper_aggressive_context", lambda: False)
    monkeypatch.setattr(vti_core, "_weekly_state_path", lambda: state)
    monkeypatch.setattr(vti_core, "_iso_week_id", lambda d=None: "2026-W37")

    ex = _FakeEx(equity=1000.0, cash=500.0, vti_value=200.0)  # 20% vs 33% target
    r1 = vti_core.rebalance_vti_core(ex, market_open=True)
    assert r1.get("action") == "buy"
    assert r1.get("ok") is True
    assert state.is_file()

    ex2 = _FakeEx(equity=1000.0, cash=500.0, vti_value=200.0)
    r2 = vti_core.rebalance_vti_core(ex2, market_open=True)
    assert r2.get("skipped") is True
    assert "weekly resize already done" in r2.get("reason", "")
    assert ex2.orders == []


def test_drift_cadence_does_not_write_weekly_state(monkeypatch, tmp_path):
    state = tmp_path / "vti_rebalance_weekly.json"
    monkeypatch.setattr(config, "vti_core_enabled", lambda: True)
    monkeypatch.setattr(config, "effective_vti_rebalance_cadence", lambda: "drift")
    monkeypatch.setattr(config, "vti_core_allocation_pct", lambda **kw: 0.33)
    monkeypatch.setattr(config, "effective_min_notional", lambda eq: 1.0)
    monkeypatch.setattr(config, "effective_vti_rebalance_drift_pct", lambda: 0.02)
    monkeypatch.setattr(config, "paper_aggressive_context", lambda: False)
    monkeypatch.setattr(vti_core, "_weekly_state_path", lambda: state)

    ex = _FakeEx(equity=1000.0, cash=500.0, vti_value=330.0)  # within band
    r = vti_core.rebalance_vti_core(ex, market_open=True)
    assert r.get("skipped") is True
    assert not state.is_file()
