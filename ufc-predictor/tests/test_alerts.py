"""Tests for live alerting."""

from __future__ import annotations

import pandas as pd
import pytest

from src.alerts import (
    alert_fingerprint,
    format_alert_text,
    generate_alerts,
    record_alert_sent,
    send_discord_alert,
    should_send_alert,
)


def _preds_with_edge() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "fight_id": "f1",
                "event_name": "UFC Test 999",
                "fighter_1": "Jon Jones",
                "fighter_2": "Stipe Miocic",
                "prob_f1_win": 0.65,
                "prob_f2_win": 0.35,
                "predicted_winner": "Jon Jones",
                "predicted_prob": 0.65,
                "f1_odds": 1.55,
                "f2_odds": 2.80,
                "best_edge": 0.09,
                "edge_f1": 0.09,
                "reasoning": "Model favors Jon Jones due to Elo edge.",
                "confidence_label": "high",
            }
        ]
    )


def test_generate_alerts_finds_singles(monkeypatch):
    monkeypatch.setattr(
        "src.alerts.assess_upcoming_card_risk",
        lambda *a, **k: {"available": True, "card_pnl": {"mean_pnl": 50, "prob_loss": 0.4}, "suggested_max_risk_pct": 6.0},
        raising=False,
    )
    out = generate_alerts(_preds_with_edge(), min_edge=0.07, risk_metrics={"available": False})
    assert out["available"] is True
    assert len(out["singles"]) == 1
    assert out["singles"][0]["pick"] == "Jon Jones"
    text = format_alert_text(out)
    assert "Jon Jones" in text
    assert "MC card" in text or "unavailable" in text


def test_alert_fingerprint_stable():
    a = generate_alerts(_preds_with_edge(), min_edge=0.05, risk_metrics={"available": False})
    b = generate_alerts(_preds_with_edge(), min_edge=0.05, risk_metrics={"available": False})
    assert alert_fingerprint(a) == alert_fingerprint(b)


def test_cooldown_blocks_duplicate(tmp_path, monkeypatch):
    state_path = tmp_path / "alert_state.json"
    monkeypatch.setattr("config.ALERT_STATE_PATH", state_path)
    monkeypatch.setattr("config.ALERT_COOLDOWN_MINUTES", 60)
    monkeypatch.setattr(
        "src.risk_manager.check_bankroll_safety",
        lambda b: (True, ""),
    )

    alert = generate_alerts(_preds_with_edge(), min_edge=0.05, risk_metrics={"available": False})
    ok1, _ = should_send_alert(alert)
    assert ok1 is True
    record_alert_sent(alert)
    ok2, reason = should_send_alert(alert)
    assert ok2 is False
    assert "duplicate" in reason or "cooldown" in reason


def test_discord_dry_run(monkeypatch):
    alert = generate_alerts(_preds_with_edge(), min_edge=0.05, risk_metrics={"available": False})
    assert send_discord_alert(alert, "https://example.com/hook", dry_run=True) is True
