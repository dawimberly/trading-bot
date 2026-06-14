"""Tests for UFC prop betting module."""

from __future__ import annotations

import pandas as pd
import pytest

import config
from src.props import (
    method_flags,
    method_probs_from_row,
    prop_display_label,
    rank_prop_singles,
    settle_prop,
    simulate_prop_bets,
    synthetic_market_odds,
)


@pytest.fixture(autouse=True)
def enable_props(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_PROPS", True)


def test_method_flags_ko_sub_dec():
    assert method_flags("KO/TKO") == (1, 0, 0)
    assert method_flags("SUB Armbar") == (0, 1, 0)
    assert method_flags("Decision - Unanimous") == (0, 0, 1)


def test_settle_goes_to_decision():
    row = pd.Series({"method": "Decision - Unanimous", "round": 3, "f1_win": 1})
    assert settle_prop("goes_to_decision", row) is True
    row2 = pd.Series({"method": "KO/TKO", "round": 1, "f1_win": 1})
    assert settle_prop("goes_to_decision", row2) is False


def test_settle_round_1_finish():
    row = pd.Series({"method": "KO/TKO", "round": 1})
    assert settle_prop("round_1_finish", row) is True
    row2 = pd.Series({"method": "KO/TKO", "round": 2})
    assert settle_prop("round_1_finish", row2) is False


def test_method_probs_sum_reasonable():
    row = pd.Series(
        {
            "fighter_1": "Alice",
            "fighter_2": "Bob",
            "prob_f1_win": 0.6,
            "f1_ko_rate": 0.25,
            "f2_ko_rate": 0.15,
            "f1_sub_avg": 0.4,
            "f2_sub_avg": 0.2,
            "f1_finish_rate": 0.5,
            "f2_finish_rate": 0.4,
            "ko_rate_diff": 0.1,
            "sub_avg_diff": 0.05,
        }
    )
    probs = method_probs_from_row(row)
    assert 0.9 < probs["ko"] + probs["sub"] + probs["dec"] < 1.1
    assert probs["pick_name"] == "Alice"


def test_synthetic_market_odds():
    odds = synthetic_market_odds(0.4, vig=0.08)
    assert odds > 2.0


def test_prop_display_label():
    row = pd.Series({"fighter_1": "Chandler", "fighter_2": "Ruffy", "prob_f1_win": 0.55})
    label = prop_display_label("fighter_ko", row)
    assert "KO/TKO" in label
    assert "Chandler" in label


def test_simulate_prop_bets_empty_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_PROPS", False)
    preds = pd.DataFrame()
    trades, summary = simulate_prop_bets(preds)
    assert trades.empty
    assert summary["trades"] == 0.0


def test_rank_prop_singles_includes_synthetic_when_no_live(monkeypatch):
    """Synthetic props populate when live lines are absent (model prob threshold)."""
    monkeypatch.setattr(config, "PROP_MIN_MODEL_PROB", 0.25)
    preds = pd.DataFrame(
        [
            {
                "fight_id": "f1",
                "event_name": "UFC Test",
                "fighter_1": "Alice",
                "fighter_2": "Bob",
                "prob_f1_win": 0.62,
                "f1_ko_rate": 0.30,
                "f2_ko_rate": 0.12,
                "f1_sub_avg": 0.5,
                "f2_sub_avg": 0.2,
                "f1_finish_rate": 0.55,
                "f2_finish_rate": 0.40,
            }
        ]
    )
    ranked = rank_prop_singles(preds, book="BetNow.eu", prop_odds=pd.DataFrame())
    assert ranked
    assert all(r["odds_source"] == "synthetic" for r in ranked)
    assert ranked[0]["prob"] >= config.PROP_MIN_MODEL_PROB
