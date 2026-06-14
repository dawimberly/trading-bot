"""Tests for ported ops/safety modules."""

from __future__ import annotations

import pandas as pd

from src.circuit_breaker import check_alerts_allowed, daily_loss_circuit_tripped
from src.fight_brief import build_fight_brief
from src.safe_io import read_json_file, write_json_atomic


def test_fight_brief_heuristic():
    row = pd.Series(
        {
            "fighter_1": "A",
            "fighter_2": "B",
            "predicted_winner": "A",
            "predicted_prob": 0.62,
            "best_edge": 0.09,
            "confidence_label": "high",
            "reasoning": "Model favors A due to striking edge.",
        }
    )
    brief = build_fight_brief(row, edge_pct=9.0)
    assert "A" in brief
    assert "edge" in brief.lower()


def test_safe_io_atomic(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    assert write_json_atomic(path, {"ok": True})
    assert read_json_file(path)["ok"] is True


def test_circuit_breaker_trips(tmp_path, monkeypatch):
    import config
    from src.circuit_breaker import update_session_bankroll_anchor

    monkeypatch.setattr(config, "CIRCUIT_BREAKER_STATE_PATH", tmp_path / "cb.json")
    monkeypatch.setattr(config, "CIRCUIT_BREAKER_ENABLED", True)
    monkeypatch.setattr(config, "UFC_PROFILE", "live")
    monkeypatch.setattr(
        config,
        "profile_value",
        lambda k: 0.02 if k == "daily_loss_limit_fraction" else 0.1,
    )
    update_session_bankroll_anchor(1000.0)
    tripped, reason, _ = daily_loss_circuit_tripped(950.0)
    assert tripped
    allowed, _ = check_alerts_allowed(950.0)
    assert not allowed
