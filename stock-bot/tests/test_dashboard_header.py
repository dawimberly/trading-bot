"""Desktop header chrome: LIVE/PAPER stamps, never NYSE 100% or VTI 85%."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_header import (
    header_kicker_text,
    header_stamp_text,
    header_tape_text,
    holdings_are_nyse_only_slogan,
    sleeve_mix_rows,
)


def _assert_no_nyse_100(text: str) -> None:
    compact = text.upper().replace("%", "")
    assert "NYSE 100" not in compact
    assert "NYSE100" not in compact.replace(" ", "")


def test_live_stamp_is_live_not_a_mix():
    stamp = header_stamp_text(paper=False)
    assert "LIVE" in stamp
    assert "NYSE" not in stamp
    assert "VTI" not in stamp
    assert "85" not in stamp
    assert "100" not in stamp


def test_paper_stamp_is_paper():
    stamp = header_stamp_text(paper=True)
    assert "PAPER" in stamp
    _assert_no_nyse_100(stamp)
    assert "LIVE" not in stamp


def test_live_tape_without_heartbeat_does_not_claim_vti_pct():
    tape = header_tape_text(paper=False)
    assert "LIVE" in tape
    assert "VTI CORE 85" not in tape
    _assert_no_nyse_100(tape)


def test_paper_tape_without_heartbeat_is_research_not_nyse_100():
    tape = header_tape_text(paper=True)
    assert "PAPER RESEARCH" in tape
    assert "SMART DYNAMIC VTI 40–75%" in tape
    assert "NYSE SLEEVE" in tape
    assert "NYSE MOMENTUM" not in tape
    _assert_no_nyse_100(tape)


def test_live_tape_uses_heartbeat_holdings():
    hb = {
        "equity": 320.0,
        "cash": 40.0,
        "sleeve_exposure": {
            "equity": 320.0,
            "vti_core_value": 96.0,
            "spy_value": 32.0,
            "nyse_value": 152.0,
        },
    }
    tape = header_tape_text(paper=False, heartbeat=hb)
    assert "LIVE HOLDINGS" in tape
    assert "VTI 30%" in tape
    assert "SPY 10%" in tape
    assert "NYSE 48%" in tape
    assert "CASH 12%" in tape
    _assert_no_nyse_100(tape)


def test_paper_tape_uses_mixed_holdings_not_nyse_slogan():
    hb = {
        "equity": 100_000.0,
        "cash": 10_000.0,
        "sleeve_exposure": {
            "equity": 100_000.0,
            "vti_core_value": 55_000.0,
            "nyse_value": 22_000.0,
            "spy_value": 8_000.0,
        },
    }
    tape = header_tape_text(paper=True, heartbeat=hb)
    assert "PAPER RESEARCH" in tape
    assert "VTI 55%" in tape
    assert "NYSE 22%" in tape
    assert "SPY 8%" in tape
    _assert_no_nyse_100(tape)


def test_paper_nyse_only_heartbeat_does_not_print_nyse_100():
    hb = {
        "equity": 97_000.0,
        "cash": 2_000.0,
        "sleeve_exposure": {
            "equity": 97_000.0,
            "vti_core_value": 0.0,
            "spy_value": 0.0,
            "nyse_value": 95_000.0,
        },
    }
    assert holdings_are_nyse_only_slogan(hb)
    tape = header_tape_text(paper=True, heartbeat=hb)
    assert "PAPER RESEARCH" in tape
    assert "SMART DYNAMIC VTI 40–75%" in tape
    assert "NYSE 100%" not in tape
    assert "NYSE 98%" not in tape
    _assert_no_nyse_100(tape)


def test_live_nyse_only_heartbeat_does_not_print_nyse_100():
    hb = {
        "equity": 320.0,
        "cash": 20.0,
        "sleeve_exposure": {
            "equity": 320.0,
            "nyse_value": 300.0,
        },
    }
    tape = header_tape_text(paper=False, heartbeat=hb)
    assert "LIVE" in tape
    _assert_no_nyse_100(tape)
    assert "NYSE 94%" not in tape


def test_kickers():
    assert "LIVE" in header_kicker_text(paper=False)
    assert "PAPER" in header_kicker_text(paper=True)


def test_dashboard_app_source_has_no_nyse_100_chrome():
    src = Path(__file__).resolve().parents[1] / "dashboard_app.py"
    text = src.read_text(encoding="utf-8")
    assert "NYSE 100" not in text


def test_look_helper_strips_hardcoded_nyse_100():
    from scripts.apply_paqinhaus_look import strip_nyse_100_chrome

    leftover = (
        '            text="  NYSE 100  ",\n'
        '            text="NYSE 100%   ·   VTI CORE OFF   ·   SPY SLEEVE 0%   ·   '
        'CRYPTO 0%   ·   STAT-ARB 0%   ·   JOURNAL = FILL   ·   '
        'ATR COOLDOWN ON   ·   SURVIVAL NOT P95",\n'
    )
    cleaned = strip_nyse_100_chrome(leftover)
    assert "NYSE 100" not in cleaned
    assert "header_stamp_text" in cleaned
    assert "header_tape_text" in cleaned


def test_sleeve_mix_rows_match_display_layout():
    hb = {
        "equity": 96_967.81,
        "cash": 12_097.18,
        "sleeve_caps": {"vti_core": 0.33, "nyse": 0.67, "cash": 0.0},
        "sleeve_exposure": {
            "equity": 96_967.81,
            "vti_core_value": 48_448.0,
            "nyse_value": 36_423.0,
        },
    }
    rows = {r["key"]: r for r in sleeve_mix_rows(hb, equity=96_967.81, cash=12_097.18)}
    assert abs(rows["vti"]["actual_pct"] - 49.96) < 0.1
    assert rows["vti"]["target_pct"] == 33.0
    assert abs(rows["nyse"]["actual_pct"] - 37.56) < 0.1
    assert rows["nyse"]["target_pct"] == 67.0
    assert abs(rows["cash"]["actual_pct"] - 12.48) < 0.1
    assert rows["cash"]["target_pct"] == 0.0
