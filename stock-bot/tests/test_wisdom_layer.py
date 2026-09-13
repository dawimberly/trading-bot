"""Tests for WisdomAdvisor."""

from __future__ import annotations

import json

import config
from modules.wisdom_layer import WisdomAdvisor


def test_bear_regime_high_conviction(tmp_path, monkeypatch):
    log_file = tmp_path / "wisdom_log.jsonl"
    monkeypatch.setattr(config, "WISDOM_LOG_FILE", str(log_file))
    monkeypatch.setattr(config, "WISDOM_CONVICTION_THRESHOLD", 0.75)
    advisor = WisdomAdvisor()
    rec = advisor.recommend(
        regime="RHYME_E Steady_Bearish",
        vol="Normal",
        macro_stress=False,
    )
    assert rec.action == "lower_core"
    assert rec.conviction >= 0.75
    assert rec.accepted is True
    assert rec.core_target_delta < 0


def test_hold_when_low_conviction(tmp_path, monkeypatch):
    log_file = tmp_path / "wisdom_log.jsonl"
    monkeypatch.setattr(config, "WISDOM_LOG_FILE", str(log_file))
    advisor = WisdomAdvisor()
    rec = advisor.recommend(
        regime="Sideways",
        vol="Normal",
        macro_stress=False,
    )
    assert rec.action == "hold"
    assert rec.accepted is False


def test_apply_core_shift_clamped(monkeypatch):
    monkeypatch.setattr(config, "WISDOM_MAX_CORE_SHIFT_PCT", 0.10)
    advisor = WisdomAdvisor()
    rec = advisor.recommend(
        regime="RHYME_E Steady_Bearish",
        vol="Normal",
        macro_stress=False,
    )
    shifted = advisor.apply_core_shift(0.80, rec)
    assert 0.70 <= shifted <= 0.90
    assert shifted == 0.72


def test_logs_recommendation(tmp_path, monkeypatch):
    log_file = tmp_path / "wisdom_log.jsonl"
    monkeypatch.setattr(config, "WISDOM_LOG_FILE", str(log_file))
    advisor = WisdomAdvisor()
    advisor.recommend(regime="RHYME_C", vol="Low", macro_stress=False, rolling_sharpe=1.2)
    assert log_file.is_file()
    row = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert "conviction" in row
    assert "timestamp" in row
    assert row["regime"] == "RHYME_C"
