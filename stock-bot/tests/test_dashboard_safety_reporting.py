"""Selected-book breaker and Thinking labels. No broker, safety writes, or network."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from modules.dashboard_accounting import daily_text, investment_text
from modules.dashboard_safety import (
    format_daily_breaker,
    read_safety_snapshot,
    thinking_rotation_text,
)
from modules.status_metrics import dashboard_stats_lines, daily_breaker_banner

ET = ZoneInfo("America/New_York")
FRIDAY_OPEN = "2026-10-16T09:30:00-04:00"
FRIDAY_CLOSE = "2026-10-16T16:00:00-04:00"
MONDAY_OPEN = "2026-10-19T09:30:00-04:00"
MONDAY_CLOSE = "2026-10-19T16:00:00-04:00"


def _state(path: Path, books: dict) -> None:
    path.write_text(json.dumps({"paper": {}, "live": {}, **books}), encoding="utf-8")


def _snap(path, book, now, equity, bounds, limit):
    return read_safety_snapshot(
        book_id=book,
        state_path=path,
        now=now,
        equity=equity,
        session_bounds=bounds,
        limit_ratio=limit,
    )


def test_medium_ignores_process_book_and_generic_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_BOOK_ID", "alpaca_live")
    path = tmp_path / "trading_safety_state.json"
    before = None
    _state(
        path,
        {
            "paper": {"daily_equity_date": "2026-10-16", "daily_equity_open": 1},
            "alpaca_live": {"daily_equity_date": "2026-10-16", "daily_equity_open": 295.17},
            "alpaca_paper_v2": {
                "daily_equity_date": "2026-10-16",
                "daily_equity_open": 100568.60,
            },
            "alpaca_paper": {"daily_equity_date": "2026-10-16", "daily_equity_open": 95126.24},
        },
    )
    before = path.read_bytes()
    now = datetime(2026, 10, 17, 0, 32, tzinfo=ET)
    text = format_daily_breaker(
        _snap(
            path,
            "alpaca_paper_v2",
            now,
            100000,
            {"session_open": MONDAY_OPEN, "session_close": MONDAY_CLOSE},
            0.04,
        )
    )
    assert path.read_bytes() == before
    assert "100,568.60" in text
    assert "295.17" not in text
    assert "95,126.24" not in text
    assert "OK" not in text
    assert "not the broker close" in text
    assert "previous" in text
    assert "no current-session anchor" in text
    assert "weekend" in text


def test_book_switch_does_not_reuse_another_anchor(tmp_path):
    path = tmp_path / "state.json"
    _state(
        path,
        {
            "alpaca_live": {"daily_equity_date": "2026-10-16", "daily_equity_open": 295.17},
            "alpaca_paper_v2": {
                "daily_equity_date": "2026-10-16",
                "daily_equity_open": 100568.60,
            },
            "alpaca_paper": {"daily_equity_date": "2026-10-16", "daily_equity_open": 95126.24},
        },
    )
    now = datetime(2026, 10, 16, 15, 0, tzinfo=ET)
    bounds = {"session_open": FRIDAY_OPEN, "session_close": FRIDAY_CLOSE}
    live = format_daily_breaker(_snap(path, "alpaca_live", now, 290, bounds, 0.02))
    medium = format_daily_breaker(_snap(path, "alpaca_paper_v2", now, 100000, bounds, 0.04))
    lab = format_daily_breaker(_snap(path, "alpaca_paper", now, 94000, bounds, 0.04))
    assert "295.17" in live and "100,568.60" not in live
    assert "100,568.60" in medium and "95,126.24" not in medium
    assert "95,126.24" in lab and "100,568.60" not in lab
    assert "configured limit 2%" in live
    assert "configured limit 4%" in medium
    assert "configured limit 4%" in lab


def test_missing_anchor_is_not_ok(tmp_path):
    path = tmp_path / "state.json"
    _state(path, {"alpaca_paper_v2": {}})
    now = datetime(2026, 10, 16, 15, 0, tzinfo=ET)
    text = format_daily_breaker(
        _snap(
            path,
            "alpaca_paper_v2",
            now,
            100000,
            {"session_open": FRIDAY_OPEN, "session_close": FRIDAY_CLOSE},
            0.04,
        )
    )
    assert "OK" not in text
    assert "unavailable" in text
    assert "not verified" in text
    assert "no trip recorded" in text


def test_malformed_unread_and_missing_book(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    now = datetime(2026, 10, 17, 0, 32, tzinfo=ET)
    assert "malformed" in format_daily_breaker(
        _snap(bad, "alpaca_paper_v2", now, 1, None, 0.04)
    )
    missing = tmp_path / "missing.json"
    _state(missing, {})
    assert "no safety state for this book" in format_daily_breaker(
        _snap(missing, "alpaca_paper_v2", now, 1, None, 0.04)
    )
    blocked = tmp_path / "blocked.json"
    blocked.write_text("{}", encoding="utf-8")
    real = Path.read_text

    def _boom(self, *args, **kwargs):
        if self == blocked:
            raise OSError("denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _boom)
    assert "unreadable" in format_daily_breaker(
        _snap(blocked, "alpaca_paper_v2", now, 1, None, 0.04)
    )


def test_previous_session_midnight_and_weekend(tmp_path):
    path = tmp_path / "state.json"
    _state(
        path,
        {
            "alpaca_paper_v2": {
                "daily_equity_date": "2026-10-15",
                "daily_equity_open": 100000,
            }
        },
    )
    midnight = datetime(2026, 10, 16, 0, 10, tzinfo=ET)
    text = format_daily_breaker(
        _snap(
            path,
            "alpaca_paper_v2",
            midnight,
            100000,
            {"session_open": FRIDAY_OPEN, "session_close": FRIDAY_CLOSE},
            0.04,
        )
    )
    assert "previous" in text
    assert "2026-10-15" in text
    assert "before the regular session" in text
    assert "OK" not in text

    path.write_text(
        json.dumps(
            {
                "alpaca_paper": {
                    "daily_equity_date": "2026-10-16",
                    "daily_equity_open": 95126.24,
                }
            }
        ),
        encoding="utf-8",
    )
    weekend = format_daily_breaker(
        _snap(
            path,
            "alpaca_paper",
            datetime(2026, 10, 17, 12, 0, tzinfo=ET),
            95000,
            {"session_open": MONDAY_OPEN, "session_close": MONDAY_CLOSE},
            0.04,
        )
    )
    assert "weekend" in weekend
    assert "previous" in weekend
    assert "no current-session anchor" in weekend


def test_trip_recorded_is_not_the_same_as_a_verified_calculation(tmp_path):
    path = tmp_path / "state.json"
    now = datetime(2026, 10, 16, 15, 0, tzinfo=ET)
    bounds = {"session_open": FRIDAY_OPEN, "session_close": FRIDAY_CLOSE}
    _state(
        path,
        {
            "alpaca_live": {
                "daily_equity_date": "2026-10-16",
                "daily_equity_open": 300,
                "circuit_tripped": False,
            }
        },
    )
    over = format_daily_breaker(_snap(path, "alpaca_live", now, 270, bounds, 0.02))
    assert "no trip recorded in the state file" in over
    assert "at or over" in over
    assert "OK" not in over

    _state(
        path,
        {
            "alpaca_live": {
                "daily_equity_date": "2026-10-16",
                "daily_equity_open": 300,
                "circuit_tripped": True,
            }
        },
    )
    recorded = format_daily_breaker(_snap(path, "alpaca_live", now, 300, bounds, 0.02))
    assert recorded.startswith("Daily Breaker: Live — trip recorded;")
    assert "under configured limit" in recorded


def test_unverified_limit_is_not_a_default(tmp_path):
    path = tmp_path / "state.json"
    _state(
        path,
        {"alpaca_paper_v2": {"daily_equity_date": "2026-10-16", "daily_equity_open": 100}},
    )
    text = format_daily_breaker(
        _snap(
            path,
            "alpaca_paper_v2",
            datetime(2026, 10, 16, 15, tzinfo=ET),
            100,
            {"session_open": FRIDAY_OPEN, "session_close": FRIDAY_CLOSE},
            None,
        )
    )
    assert "limit is unverified" in text
    assert "4%" not in text
    assert "OK" not in text


def test_stale_global_thinking_is_not_a_current_allocation():
    stale = "vti 59% | spy 11% | crypto 9% | cash 7% | energy 5% | gold 5%"
    absent = thinking_rotation_text({}, book_id="alpaca_paper_v2")
    assert absent == "no current recommendation for this book"
    assert stale not in absent

    historical = thinking_rotation_text(
        {
            "thinking_engine": {
                "book_id": "alpaca_paper_v2",
                "timestamp": "2026-08-14T15:00:00-04:00",
                "suggested_tilt": {
                    "vti": 0.59,
                    "spy": 0.11,
                    "crypto": 0.09,
                    "cash": 0.07,
                    "energy": 0.05,
                    "gold": 0.05,
                },
            }
        },
        book_id="alpaca_paper_v2",
        now=datetime(2026, 10, 16, 15, tzinfo=ET),
        session_bounds={"session_open": FRIDAY_OPEN, "session_close": FRIDAY_CLOSE},
    )
    assert "historical recommendation 2026-08-14" in historical
    assert "not a holding" in historical
    assert "application unknown" in historical
    assert "allocation" not in historical


def test_thinking_provenance_timestamp_and_application():
    assert (
        thinking_rotation_text(
            {"thinking_engine": {"book_id": "alpaca_live", "timestamp": "2026-10-16T15:00:00-04:00"}},
            book_id="alpaca_paper_v2",
        )
        == "no current recommendation for this book"
    )
    assert "no timestamp" in thinking_rotation_text(
        {"thinking_engine": {"book_id": "alpaca_paper", "suggested_tilt": {"vti": 0.5}}},
        book_id="alpaca_paper",
    )
    current = thinking_rotation_text(
        {
            "thinking_engine": {
                "book_id": "alpaca_paper",
                "timestamp": "2026-10-16T15:00:00-04:00",
                "applied": False,
                "suggested_tilt": {"cash": 0.2},
            },
            "scan_schedule": {"session_open": FRIDAY_OPEN, "session_close": FRIDAY_CLOSE},
        },
        book_id="alpaca_paper",
        now=datetime(2026, 10, 16, 15, tzinfo=ET),
        session_bounds={"session_open": FRIDAY_OPEN, "session_close": FRIDAY_CLOSE},
    )
    assert "not applied" in current
    assert "historical" not in current
    assert thinking_rotation_text(None, book_id="alpaca_live").endswith(
        "no current recommendation for this book"
    )


def test_banner_keeps_accounting_identities_separate(tmp_path, monkeypatch):
    def _forbid(*_args, **_kwargs):
        raise AssertionError("engine safety status must not be called")

    monkeypatch.setattr(
        "modules.trading_safety.get_daily_loss_status", _forbid
    )
    path = tmp_path / "state.json"
    _state(
        path,
        {
            "alpaca_paper_v2": {
                "daily_equity_date": "2026-10-16",
                "daily_equity_open": 100568.60,
            }
        },
    )
    performance = {
        "book_id": "alpaca_live",
        "daily_pnl": 5,
        "daily_pct": 1,
        "investment_pnl": 9,
        "baseline_date": "2026-06-08",
    }
    line1, line2, detail = dashboard_stats_lines(
        equity=100000,
        heartbeat={},
        paper_chase=True,
        live_only=False,
        book_id="alpaca_paper_v2",
        account_performance=performance,
        safety_now=datetime(2026, 10, 17, 0, 32, tzinfo=ET),
        safety_state=path,
        insight_slot=4,
    )
    assert daily_text({}) in line1
    assert "prior official close" not in line1
    assert "Investment P&L: unavailable" in line1
    assert detail.startswith("Investment P&L: unavailable")
    assert "100,568.60" in line2
    assert "no current recommendation for this book" in line2
    assert "vti 59%" not in line2
    assert "OK" not in line2
    assert daily_text(
        {"daily_pnl": 1.5, "daily_pct": 0.5}
    ).endswith("vs prior official close)")
    assert "external cash flows excluded" in investment_text(
        {"investment_pnl": 10, "baseline_date": "2026-09-01"}
    )


def test_other_book_heartbeat_does_not_supply_the_calendar(tmp_path):
    path = tmp_path / "state.json"
    _state(
        path,
        {
            "alpaca_paper_v2": {
                "daily_equity_date": "2026-10-16",
                "daily_equity_open": 100568.60,
            }
        },
    )
    line1, line2, _detail = dashboard_stats_lines(
        equity=100000,
        heartbeat={
            "book_id": "alpaca_paper",
            "scan_schedule": {"session_open": MONDAY_OPEN, "session_close": MONDAY_CLOSE},
        },
        paper_chase=True,
        live_only=False,
        book_id="alpaca_paper_v2",
        safety_now=datetime(2026, 10, 17, 0, 32, tzinfo=ET),
        safety_state=path,
        insight_slot=4,
    )
    assert "session not verified as current" in line2
    assert "previous" not in line2
    assert "Account Total" in line1


def test_shared_banner_no_longer_calls_a_missing_anchor_ok():
    text = daily_breaker_banner(
        live_active=False,
        live_dl={},
        paper_dl={"limit_pct": 4, "tripped": False, "loss_pct": None},
    )
    assert "OK" not in text
    assert "not verified" in text
