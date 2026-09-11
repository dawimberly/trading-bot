"""Desktop header chrome: live stamp is LIVE, never NYSE 100% or VTI 85%."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_header import header_kicker_text, header_stamp_text, header_tape_text


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
    assert "NYSE 100" not in stamp


def test_live_tape_without_heartbeat_does_not_claim_vti_pct():
    tape = header_tape_text(paper=False)
    assert "LIVE" in tape
    assert "VTI CORE 85" not in tape
    assert "NYSE 100%" not in tape


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
    assert "NYSE 100%" not in tape


def test_kickers():
    assert "LIVE" in header_kicker_text(paper=False)
    assert "PAPER" in header_kicker_text(paper=True)
