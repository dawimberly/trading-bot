"""Cash-session gate for the paper 3h Telegram pulse (schedule only)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from modules.periodic_summary import in_periodic_summary_window, periodic_summary_due

_CT = ZoneInfo("America/Chicago")


def _ct(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=_CT)


# Friday 2026-08-21
def test_window_open_at_0930_ct_weekday():
    assert in_periodic_summary_window(_ct(2026, 8, 21, 9, 30)) is True


def test_window_closed_before_0930_ct():
    assert in_periodic_summary_window(_ct(2026, 8, 21, 9, 29)) is False
    assert in_periodic_summary_window(_ct(2026, 8, 21, 8, 0)) is False


def test_window_open_before_1500_ct():
    assert in_periodic_summary_window(_ct(2026, 8, 21, 14, 59)) is True


def test_window_closed_at_and_after_1500_ct():
    assert in_periodic_summary_window(_ct(2026, 8, 21, 15, 0)) is False
    assert in_periodic_summary_window(_ct(2026, 8, 21, 15, 30)) is False
    assert in_periodic_summary_window(_ct(2026, 8, 21, 22, 0)) is False


def test_window_closed_weekend():
    assert in_periodic_summary_window(_ct(2026, 8, 22, 12, 0)) is False  # Sat
    assert in_periodic_summary_window(_ct(2026, 8, 23, 12, 0)) is False  # Sun


def test_window_open_monday_midday():
    assert in_periodic_summary_window(_ct(2026, 8, 17, 12, 0)) is True


def test_due_false_outside_window_even_if_never_sent(monkeypatch):
    monkeypatch.setattr("config.TELEGRAM_ALERT_PERIODIC_SUMMARY", True, raising=False)
    monkeypatch.setattr(
        "modules.periodic_summary._load_summary_ts", lambda: {}, raising=True
    )
    assert periodic_summary_due(3.0, now=_ct(2026, 8, 21, 15, 30)) is False
    assert periodic_summary_due(3.0, now=_ct(2026, 8, 21, 8, 0)) is False


def test_due_true_in_window_when_never_sent(monkeypatch):
    monkeypatch.setattr("config.TELEGRAM_ALERT_PERIODIC_SUMMARY", True, raising=False)
    monkeypatch.setattr(
        "modules.periodic_summary._load_summary_ts", lambda: {}, raising=True
    )
    assert periodic_summary_due(3.0, now=_ct(2026, 8, 21, 10, 0)) is True


def test_due_respects_interval_inside_window(monkeypatch):
    monkeypatch.setattr("config.TELEGRAM_ALERT_PERIODIC_SUMMARY", True, raising=False)
    last = _ct(2026, 8, 21, 10, 0).isoformat()
    monkeypatch.setattr(
        "modules.periodic_summary._load_summary_ts",
        lambda: {"last_sent_at": last},
        raising=True,
    )
    assert periodic_summary_due(3.0, now=_ct(2026, 8, 21, 12, 0)) is False
    assert periodic_summary_due(3.0, now=_ct(2026, 8, 21, 13, 0)) is True
