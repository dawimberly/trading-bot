from types import SimpleNamespace

import config
from modules import h1d_trend_mix as h


class FakeExec:
    def __init__(self, cash, bp, equity=300.0):
        self.cash, self.bp, self.equity = cash, bp, equity
        self.calls = []
        self.client = None

    def _get_account(self):
        return SimpleNamespace(cash=self.cash, buying_power=self.bp, equity=self.equity)

    def execute_order(self, symbol, side, notional=None, **kw):
        self.calls.append(("order", symbol, side, notional))
        return SimpleNamespace(id=None)

    def execute_full_exit(self, symbol, **kw):
        self.calls.append(("exit", symbol))
        return SimpleNamespace(id=None)


def _min(monkeypatch, value=1.0):
    monkeypatch.setattr(config, "effective_min_notional", lambda: value)


def test_size_buy_to_cash_caps_with_cushion():
    assert h.size_buy_to_cash(12.50, 12.37, 0.25) == 12.12
    assert h.size_buy_to_cash(10.0, 50.0, 0.25) == 10.0
    assert h.size_buy_to_cash(10.0, None, 0.25) == 10.0
    assert h.size_buy_to_cash(10.0, 0.1, 0.25) == 0.0


def test_cash_cushion_floor_and_pct(monkeypatch):
    monkeypatch.delenv("H1D_TREND_MIX_CASH_CUSHION", raising=False)
    assert h.cash_cushion(200.0) == 0.25
    assert h.cash_cushion(300.0) == 0.30
    assert h.cash_cushion(95_000.0) == 95.0


def test_available_cash_never_uses_margin():
    assert h._available_cash(FakeExec(cash=100.0, bp=200.0)) == 100.0
    assert h._available_cash(FakeExec(cash=-50.0, bp=200.0)) == 0.0


def test_last_buy_shrunk_not_rejected(monkeypatch):
    _min(monkeypatch)
    ex = FakeExec(cash=12.37, bp=12.37, equity=200.0)
    orders = [{"symbol": "XLY", "side": "buy", "notional": 12.50, "target_weight": 0.0417}]
    assert h._submit_orders(ex, orders, "monthly") is True
    assert ex.calls == [("order", "XLY", "buy", 12.12)]
    assert orders[0]["requested_notional"] == 12.50


def test_buy_skipped_when_no_cash_is_not_failure(monkeypatch):
    _min(monkeypatch)
    ex = FakeExec(cash=0.5, bp=0.5)
    orders = [{"symbol": "XLY", "side": "buy", "notional": 12.50, "target_weight": 0.0417}]
    assert h._submit_orders(ex, orders, "monthly") is True
    assert ex.calls == [] and orders[0]["status"] == "skipped_cash"


def test_wind_down_and_zero_weight_sells_are_full_exits():
    rows = h.wind_down_orders({"GMAB": 1000.0, "HAFN": 800.0}, dust=1.0)
    assert all(r["full_exit"] for r in rows)
    weights = {"SSO": 1.0}
    plan = h.plan_rebalance(weights, {"SSO": 50.0, "BIL": 50.0}, 100.0, dust=1.0) if "BIL" in h.STRATEGY_SYMBOLS else []
    for r in plan:
        if r["symbol"] == "BIL":
            assert r["full_exit"] is True
        if r["symbol"] == "SSO":
            assert not r.get("full_exit")


def test_wind_down_submits_full_exit_not_notional(monkeypatch):
    _min(monkeypatch)
    ex = FakeExec(cash=1e6, bp=1e6)
    rows = h.wind_down_orders({"GMAB": 1000.0}, dust=1.0)
    assert h._submit_orders(ex, rows, "wind_down") is True
    assert ex.calls == [("exit", "GMAB")]

