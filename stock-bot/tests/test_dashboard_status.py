"""VTI display and heartbeat vs broker freshness. No broker or bot process."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_header import header_tape_text, sleeve_mix_rows
from dashboard_status import (
    account_mode_text,
    format_risk_budget,
    format_source_freshness,
    health_display_tone,
    market_clock,
    primary_issue,
    vti_target_from_env,
)

_MEDIUM_OFF = {
    "VTI_CORE_ENABLED": "false",
    "PAPER_VTI_CORE_PCT": "0",
    "VTI_CORE_PCT": "0",
    "PAPER_DYNAMIC_VTI": "false",
}

_HEARTBEAT_33 = {
    "equity": 100_000.0,
    "cash": 10_000.0,
    "sleeve_caps": {"vti_core": 0.33, "nyse": 0.67},
    "sleeve_exposure": {
        "equity": 100_000.0,
        "vti_core_value": 33_000.0,
        "nyse_value": 40_000.0,
    },
}


def test_disabled_vti_is_zero_even_if_heartbeat_cap_is_33():
    vti = vti_target_from_env(_MEDIUM_OFF, paper=True)
    assert vti["enabled"] is False
    assert vti["target"] == 0.0
    assert "0%" in vti["label"]
    assert "33" not in vti["label"]
    rows = {
        r["key"]: r
        for r in sleeve_mix_rows(
            _HEARTBEAT_33,
            equity=100_000.0,
            cash=10_000.0,
            vti_target_frac=vti["target"],
            override_vti_target=True,
        )
    }
    assert rows["vti"]["target_pct"] == 0.0
    tape = header_tape_text(
        paper=True,
        heartbeat=_HEARTBEAT_33,
        book_id="alpaca_paper_v2",
        vti_label=vti["label"],
    )
    assert vti["label"] in tape
    assert "33%" not in tape


def test_enabled_vti_shows_configured_target_not_a_hardcoded_off():
    vti = vti_target_from_env(
        {
            "VTI_CORE_ENABLED": "true",
            "PAPER_VTI_CORE_PCT": "0.40",
            "PAPER_DYNAMIC_VTI": "false",
        },
        paper=True,
    )
    assert vti["enabled"] is True
    assert vti["target"] == 0.40
    assert "40%" in vti["label"]
    assert "OFF" not in vti["label"].upper()
    tape = header_tape_text(
        paper=True, book_id="alpaca_paper_v2", vti_label=vti["label"]
    )
    assert "40%" in tape
    assert "VTI CORE OFF" not in tape


def test_missing_vti_flag_is_not_invented():
    vti = vti_target_from_env({}, paper=True)
    assert vti["enabled"] is None
    assert vti["target"] is None
    assert "0%" not in vti["label"]
    assert "33" not in vti["label"]


def test_live_account_stopped_is_not_labeled_as_trading():
    assert account_mode_text(paper=False) == "Account: Live"
    assert "TRADING" not in account_mode_text(paper=False)
    assert "TRADING" not in account_mode_text(paper=True)
    assert (
        primary_issue(
            running=False,
            market_closed=True,
            heartbeat_stale=True,
        )
        == "Bot stopped"
    )


def test_market_closed_is_not_the_health_incident():
    assert (
        primary_issue(running=True, market_closed=True, heartbeat_stale=False) is None
    )
    assert (
        primary_issue(running=True, market_closed=True, heartbeat_stale=True)
        == "Stale heartbeat"
    )
    assert (
        primary_issue(running=True, thinking_error=True, market_closed=True)
        == "Thinking service error"
    )
    assert (
        health_display_tone(
            health_color="red",
            running=True,
            market_closed=True,
            heartbeat_stale=False,
            thinking_error=False,
        )
        == "alert"
    )
    assert (
        health_display_tone(
            health_color="red",
            running=False,
            market_closed=True,
            heartbeat_stale=True,
            thinking_error=False,
        )
        == "muted"
    )
    assert (
        health_display_tone(
            health_color="green",
            running=True,
            market_closed=True,
            heartbeat_stale=False,
            thinking_error=False,
        )
        == "calm"
    )


def test_stale_heartbeat_does_not_mark_broker_data_stale():
    fresh = format_source_freshness(
        heartbeat_raw="2026-10-11T18:00:00",
        heartbeat_stale=True,
        account_raw="2026-10-12T00:03:00",
        positions_raw="2026-10-12T00:03:00",
    )
    assert "stale" in fresh["heartbeat"].lower()
    assert "stale" not in fresh["broker"].lower()
    assert "Broker data as of" in fresh["broker"]
    assert "Heartbeat as of" in fresh["heartbeat"]
    assert fresh["heartbeat"] != fresh["broker"]


def test_market_clock_names_timezone_when_the_open_has_one():
    et = ZoneInfo("America/New_York")
    now = datetime(2026, 10, 12, 1, 3, tzinfo=et)
    hb = {
        "scan_schedule": {
            "market_open": False,
            "session_open": datetime(2026, 10, 12, 9, 30, tzinfo=et).isoformat(),
        }
    }
    clock = market_clock(hb, now=now)
    assert clock["is_open"] is False
    assert clock["level"] == "info"
    assert "8h 27m" in clock["value"]
    # isoformat keeps the offset; the label must show that zone, not drop it.
    assert "UTC-04:00" in clock["value"] or "ET" in clock["value"]
    assert "stale" not in clock["sentence"].lower()
    naive = market_clock(
        {"scan_schedule": {"market_open": False, "session_open": "2026-10-12T09:30:00"}},
        now=datetime(2026, 10, 12, 1, 3),
    )
    assert "ET" not in naive["value"]
    assert "UTC" not in naive["value"]


def test_risk_budget_uses_the_fraction_it_was_given():
    text = format_risk_budget(equity=10_000, risk_frac=0.018, order_cap=250)
    assert "1.8%" in text
    assert "Risk budget:" in text
    assert "Max order notional:" in text
    assert "(1% of" not in text


def test_rotating_insight_uses_book_vti_not_heartbeat_cap():
    from modules.status_metrics import rotating_insight_line

    line = rotating_insight_line(
        hb={"sleeve_caps": {"vti_core": 0.33}},
        paper_chase=True,
        slot=3,
        vti_label="VTI off · target 0%",
    )
    assert "33" not in line
    assert "VTI off · target 0%" in line


def _marquee_methods():
    """Load only animation methods; never import the dashboard or contact a broker."""
    import ast

    path = Path(__file__).resolve().parents[1] / "dashboard_app.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    app = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TradingDashboardApp")
    names = {"_start_tape_marquee", "_tick_tape_marquee"}
    methods = [n for n in app.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(methods) == 2
    namespace = {
        "header_tape_text": lambda **kwargs: "Medium strategy | VTI off",
        "_book_is_paper": lambda book: True,
        "tape_vti_label": lambda value: "VTI off",
    }
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


def _marquee_double():
    from types import SimpleNamespace

    methods = _marquee_methods()
    active = {}
    canceled = []
    rendered = []
    counter = [0]

    def after(delay, callback):
        counter[0] += 1
        token = f"timer-{counter[0]}"
        active[token] = (delay, callback)
        return token

    def cancel(token):
        canceled.append(token)
        active.pop(token, None)

    view = SimpleNamespace(
        _book_id="alpaca_paper_v2", _vti={},
        _header_tape=SimpleNamespace(configure=lambda **kw: rendered.append(kw["text"])),
        _tape_visible_chars=lambda: 48,
        after=after, after_cancel=cancel,
    )
    view._tick_tape_marquee = lambda: methods["_tick_tape_marquee"](view)
    return methods, view, active, canceled, rendered


def test_scrolling_tape_advances_with_one_scheduled_callback():
    methods, view, active, canceled, rendered = _marquee_double()
    methods["_start_tape_marquee"](view)
    assert len(active) == 1
    token = view._tape_marquee_job
    delay, callback = active.pop(token)  # Tk consumes a timer before invoking it.
    assert delay == 220
    first = rendered[-1]
    callback()
    assert len(active) == 1
    assert view._tape_marquee_job in active
    assert rendered[-1] != first
    assert len(rendered[-1]) == 48


def test_restarting_scrolling_tape_cancels_previous_callback():
    methods, view, active, canceled, rendered = _marquee_double()
    methods["_start_tape_marquee"](view)
    previous = view._tape_marquee_job
    methods["_start_tape_marquee"](view)
    assert canceled == [previous]
    assert previous not in active
    assert len(active) == 1
    assert view._tape_marquee_job in active


def test_tape_tick_does_not_schedule_when_widget_is_missing():
    methods, view, active, canceled, rendered = _marquee_double()
    del view._header_tape
    view._tick_tape_marquee()
    assert not active
    assert not rendered


def test_dashboard_control_labels_remain_clear():
    src = (Path(__file__).resolve().parents[1] / "dashboard_app.py").read_text(encoding="utf-8")
    assert "Update All 3 books" in src
    assert "LIVE TRADING" not in src
