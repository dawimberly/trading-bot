"""Per-book stop experiment (2026-10-25). No broker, no secrets."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import config
from modules.position_exits import run_position_exits


def _pos(entry: float, current: float, *, created_at, plpc: float | None = None):
    if plpc is None:
        plpc = (current - entry) / entry
    return SimpleNamespace(
        symbol="ARM",
        qty=10.0,
        avg_entry_price=entry,
        current_price=current,
        unrealized_plpc=plpc,
        created_at=created_at,
    )


class _Exec:
    def __init__(self, pos):
        self._pos = pos
        self.orders: list[tuple] = []

    def _get_positions(self):
        return [self._pos]

    def _get_account(self):
        return SimpleNamespace(equity=10_000.0)

    def execute_full_exit(self, symbol, reason="exit", sleeve=None, exit_meta=None):
        self.orders.append((symbol, reason, sleeve))
        return {"id": "1"}

    def order_filled(self, order):
        return bool(order)


class _Risk:
    def _log_event(self, _msg):
        return None


def _run(pos):
    ex = _Exec(pos)
    n = run_position_exits(ex, _Risk(), equity_session_open=True)
    return n, ex.orders


def test_stop_table(monkeypatch):
    monkeypatch.delenv("STOP_LOSS_PCT", raising=False)
    monkeypatch.delenv("PAPER_LAB_HOLD_ONLY", raising=False)
    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_paper")
    monkeypatch.setenv("PAPER_LAB_CONCENTRATED", "true")
    monkeypatch.setenv("PAPER_LAB_HOLD_ONLY", "true")
    assert config.book_hard_stop_pct() is None

    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_paper_v2")
    monkeypatch.setenv("PAPER_MEDIUM_STRATEGY", "true")
    assert config.book_hard_stop_pct() == 0.10

    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_live")
    monkeypatch.delenv("PAPER_MEDIUM_STRATEGY", raising=False)
    monkeypatch.delenv("PAPER_LAB_CONCENTRATED", raising=False)
    assert config.book_hard_stop_pct() == 0.10

    monkeypatch.delenv("TRADING_BOOK_ID", raising=False)
    assert config.book_hard_stop_pct() == 0.05

    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_live")
    monkeypatch.setenv("STOP_LOSS_PCT", "0.12")
    assert config.book_hard_stop_pct() == 0.12
    monkeypatch.setenv("STOP_LOSS_PCT", "off")
    assert config.book_hard_stop_pct() is None


def test_lab_hold_only_ignores_a_loss_and_exits_at_30_days(monkeypatch):
    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_paper")
    monkeypatch.setenv("PAPER_LAB_CONCENTRATED", "true")
    monkeypatch.setenv("PAPER_LAB_HOLD_ONLY", "true")
    monkeypatch.setenv("PAPER_NYSE_A2B1_ENABLED", "true")
    monkeypatch.setenv("STOP_LOSS_PCT", "0.10")
    young = datetime.now(timezone.utc) - timedelta(days=2)
    n, orders = _run(_pos(100.0, 89.0, created_at=young))
    assert n == 0
    assert orders == []

    # A winner that A2B1 would have flattened still waits for the hold.
    n, orders = _run(_pos(100.0, 112.0, created_at=young))
    assert n == 0
    assert orders == []

    old = datetime.now(timezone.utc) - timedelta(days=31)
    n, orders = _run(_pos(100.0, 89.0, created_at=old))
    assert n == 1
    assert orders == [("ARM", "lab_time", "NYSE")]


def test_lab_without_hold_only_still_uses_the_disaster_stop(monkeypatch):
    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_paper")
    monkeypatch.setenv("PAPER_LAB_CONCENTRATED", "true")
    monkeypatch.setenv("PAPER_LAB_HOLD_ONLY", "false")
    monkeypatch.setenv("PAPER_NYSE_A2B1_ENABLED", "false")
    young = datetime.now(timezone.utc) - timedelta(days=2)
    n, orders = _run(_pos(100.0, 89.0, created_at=young))
    assert n == 1
    assert orders == [("ARM", "lab_disaster", "NYSE")]


def test_medium_and_live_stop_at_10_not_7(monkeypatch):
    monkeypatch.setenv("STOP_LOSS_PCT", "0.10")
    monkeypatch.setattr(config, "effective_smart_stops_enabled", lambda: False)
    monkeypatch.setattr(config, "effective_exit_optimization_enabled", lambda: False)
    young = datetime.now(timezone.utc) - timedelta(days=2)
    for book in ("alpaca_paper_v2", "alpaca_live"):
        monkeypatch.setenv("TRADING_BOOK_ID", book)
        if book == "alpaca_paper_v2":
            monkeypatch.setenv("PAPER_MEDIUM_STRATEGY", "true")
        else:
            monkeypatch.delenv("PAPER_MEDIUM_STRATEGY", raising=False)
        monkeypatch.delenv("PAPER_LAB_CONCENTRATED", raising=False)
        n, orders = _run(_pos(100.0, 93.0, created_at=young))
        assert (book, n, orders) == (book, 0, [])
        n, orders = _run(_pos(100.0, 89.0, created_at=young))
        assert n == 1
        assert orders[0][1] == "stop_loss"


def test_tighter_atr_inside_10_percent_does_not_exit(monkeypatch):
    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_paper_v2")
    monkeypatch.setenv("PAPER_MEDIUM_STRATEGY", "true")
    monkeypatch.setenv("STOP_LOSS_PCT", "0.10")
    monkeypatch.setattr(config, "effective_smart_stops_enabled", lambda: True)
    monkeypatch.setattr(config, "effective_exit_optimization_enabled", lambda: False)

    def _atr(*_a, **_k):
        return 2.0, 0.5

    def _smart(**_k):
        return {
            "action": "exit",
            "exit_code": "smart_atr_stop",
            "reason": "tight",
            "meta": {},
            "stop_price": 95.0,
        }

    monkeypatch.setattr(
        "modules.exit_management.resolve_symbol_atr_and_conviction", _atr
    )
    monkeypatch.setattr("modules.smart_atr_stops.evaluate_smart_stop", _smart)
    young = datetime.now(timezone.utc) - timedelta(days=2)
    n, orders = _run(_pos(100.0, 93.0, created_at=young))
    assert n == 0
    assert orders == []

    n, orders = _run(_pos(100.0, 89.0, created_at=young))
    assert n == 1
    assert orders[0][1] == "smart_atr_stop"


def test_live_atr_inside_10_percent_does_not_exit(monkeypatch):
    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_live")
    monkeypatch.setenv("STOP_LOSS_PCT", "0.10")
    monkeypatch.delenv("PAPER_MEDIUM_STRATEGY", raising=False)
    monkeypatch.delenv("PAPER_LAB_CONCENTRATED", raising=False)
    monkeypatch.setattr(config, "effective_smart_stops_enabled", lambda: False)
    monkeypatch.setattr(config, "effective_exit_optimization_enabled", lambda: True)

    monkeypatch.setattr(
        "modules.exit_management.resolve_symbol_atr_and_conviction",
        lambda *_a, **_k: (2.0, 0.5),
    )
    monkeypatch.setattr(
        "modules.exit_management.compute_trailing_stop_plan",
        lambda *_a, **_k: {
            "stop_price": 99.0,
            "arm_pct": 0.50,
            "pullback_pct": 0.35,
        },
    )
    monkeypatch.setattr(
        "modules.exit_management.should_partial_exit", lambda *_a, **_k: False
    )
    young = datetime.now(timezone.utc) - timedelta(days=2)
    n, orders = _run(_pos(100.0, 93.0, created_at=young))
    assert n == 0
    assert orders == []
    n, orders = _run(_pos(100.0, 89.0, created_at=young))
    assert n == 1
    assert orders[0][1] in {"atr_stop", "stop_loss"}
