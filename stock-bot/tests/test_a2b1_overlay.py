"""Hold A2B1 overlay — Lab 8 paper only (no Alpaca)."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import config
from modules.position_exits import (
    _et_weekday,
    a2b1_should_flatten,
    prior_session_close_return,
    run_position_exits,
)

_ET = ZoneInfo("America/New_York")


def test_a2b1_rule_skip_monday():
    assert a2b1_should_flatten(1, 0.08) is True  # Tuesday
    assert a2b1_should_flatten(4, 0.12) is True  # Friday
    assert a2b1_should_flatten(0, 0.12) is False  # Monday
    assert a2b1_should_flatten(2, 0.079) is False
    assert a2b1_should_flatten(2, None) is False
    assert a2b1_should_flatten(2, 0.10, entered_today=True) is False
    assert a2b1_should_flatten(3, 0.08, mode="thu_only") is True
    assert a2b1_should_flatten(2, 0.08, mode="thu_only") is False


def test_prior_session_drops_today_bar():
    as_of = date(2026, 9, 25)  # Friday
    closes = {
        date(2026, 9, 22): 100.0,
        date(2026, 9, 23): 100.0,
        date(2026, 9, 24): 108.0,
        date(2026, 9, 25): 110.0,  # today — ignore
    }
    ret = prior_session_close_return(closes, as_of_date=as_of)
    assert ret is not None
    assert abs(ret - 0.08) < 1e-9


def test_prior_session_uses_last_completed_when_today_missing():
    as_of = date(2026, 9, 25)
    closes = {
        date(2026, 9, 23): 100.0,
        date(2026, 9, 24): 108.0,
    }
    ret = prior_session_close_return(closes, as_of_date=as_of)
    assert ret is not None
    assert abs(ret - 0.08) < 1e-9


def test_a2b1_enabled_only_on_lab(monkeypatch):
    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_paper")
    monkeypatch.setenv("PAPER_LAB_CONCENTRATED", "true")
    monkeypatch.setenv("PAPER_NYSE_A2B1_ENABLED", "true")
    assert config.paper_nyse_a2b1_enabled() is True
    assert abs(config.paper_nyse_gain_exit_pct() - 0.08) < 1e-9
    assert config.paper_nyse_gain_exit_mode() == "skip_monday"

    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_paper_v2")
    monkeypatch.setenv("PAPER_MEDIUM_STRATEGY", "true")
    monkeypatch.setenv("PAPER_NYSE_A2B1_ENABLED", "true")
    assert config.paper_nyse_a2b1_enabled() is False

    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_live")
    monkeypatch.setenv("PAPER_LAB_CONCENTRATED", "true")
    assert config.paper_nyse_a2b1_enabled() is False


def test_lab_banner_mentions_a2b1_when_on(monkeypatch):
    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_paper")
    monkeypatch.setenv("PAPER_LAB_CONCENTRATED", "true")
    monkeypatch.setenv("PAPER_NYSE_A2B1_ENABLED", "true")
    banner = config.format_paper_lab_banner() or ""
    assert "A2B1" in banner


def test_et_weekday_monday():
    monday = datetime(2026, 9, 28, 10, 0, tzinfo=_ET)
    assert _et_weekday(monday) == 0


def _lab_exit_harness(monkeypatch, *, weekday_dt, prior_ret, created_at):
    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_paper")
    monkeypatch.setenv("PAPER_LAB_CONCENTRATED", "true")
    monkeypatch.setenv("PAPER_NYSE_A2B1_ENABLED", "true")
    monkeypatch.setattr(
        "modules.position_exits._et_weekday", lambda now=None: weekday_dt.weekday()
    )

    orders = []

    class Exec:
        _a2b1_prior_returns = {"ARM": prior_ret}

        def _get_positions(self):
            return [
                SimpleNamespace(
                    symbol="ARM",
                    qty=10.0,
                    avg_entry_price=100.0,
                    current_price=108.0,
                    unrealized_plpc=0.08,
                    created_at=created_at,
                )
            ]

        def _get_account(self):
            return SimpleNamespace(equity=100_000.0)

        def execute_full_exit(self, symbol, reason="exit", sleeve=None):
            orders.append((symbol, reason, sleeve))
            return {"id": "1"}

        def order_filled(self, order):
            return bool(order)

    class Risk:
        def _log_event(self, _msg):
            return None

    n = run_position_exits(Exec(), Risk(), equity_session_open=True)
    return n, orders


def test_a2b1_flattens_on_tuesday(monkeypatch):
    created = datetime(2026, 9, 22, 15, 0, tzinfo=_ET)
    n, orders = _lab_exit_harness(
        monkeypatch,
        weekday_dt=datetime(2026, 9, 29, 10, 0, tzinfo=_ET),
        prior_ret=0.09,
        created_at=created,
    )
    assert n == 1
    assert orders == [("ARM", "a2b1_gain_fade", "NYSE")]


def test_a2b1_skips_monday(monkeypatch):
    created = datetime(2026, 9, 22, 15, 0, tzinfo=_ET)
    n, orders = _lab_exit_harness(
        monkeypatch,
        weekday_dt=datetime(2026, 9, 28, 10, 0, tzinfo=_ET),
        prior_ret=0.09,
        created_at=created,
    )
    assert n == 0
    assert orders == []
