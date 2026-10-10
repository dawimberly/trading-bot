"""Telegram fill alerts: paper + live above the notional floor."""

from __future__ import annotations

import config
from modules.trade_notifier import should_notify_fill


def _details(**kw):
    base = {
        "symbol": "XOM",
        "side": "Buy",
        "quantity": 10,
        "price": 100.0,
        "notional": 1000.0,
    }
    base.update(kw)
    return base


def test_paper_fill_alerts_when_flag_on(monkeypatch):
    monkeypatch.setattr(config, "PAPER_TRADING", True)
    monkeypatch.setattr(config, "TELEGRAM_ALERT_FILLS", True)
    monkeypatch.setattr(config, "get_telegram_config", lambda: ("tok", "chat"))
    monkeypatch.setattr(config, "telegram_fill_min_usd", lambda: 5.0)
    assert should_notify_fill(_details()) is True


def test_live_fill_alerts_when_flag_on(monkeypatch):
    monkeypatch.setattr(config, "PAPER_TRADING", False)
    monkeypatch.setattr(config, "TELEGRAM_ALERT_FILLS", True)
    monkeypatch.setattr(config, "get_telegram_config", lambda: ("tok", "chat"))
    monkeypatch.setattr(config, "telegram_fill_min_usd", lambda: 5.0)
    assert should_notify_fill(_details(notional=5.0)) is True


def test_fill_below_min_is_skipped(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALERT_FILLS", True)
    monkeypatch.setattr(config, "get_telegram_config", lambda: ("tok", "chat"))
    monkeypatch.setattr(config, "telegram_fill_min_usd", lambda: 5.0)
    assert should_notify_fill(_details(notional=1.0, quantity=0, price=0)) is False


def test_fills_off_skips(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_ALERT_FILLS", False)
    monkeypatch.setattr(config, "get_telegram_config", lambda: ("tok", "chat"))
    monkeypatch.setattr(config, "telegram_fill_min_usd", lambda: 0.0)
    assert should_notify_fill(_details()) is False
