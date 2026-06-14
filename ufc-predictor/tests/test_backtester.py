"""Tests for walk-forward backtest and classification metrics."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import config
from src.backtester import (
    _has_valid_odds,
    _market_probs,
    build_calibration_bins,
    evaluate_classification,
    simulate_value_bets,
    walk_forward_predict,
)
from src.data_loader import _add_pipeline_aliases
from src.feature_engineering import build_feature_matrix
from src.predictor import compute_style_matchup_bonus, rank_predictions_by_edge


FIXTURE = Path(__file__).parent / "fixtures" / "sample_fights.csv"


@pytest.fixture
def feature_frame() -> pd.DataFrame:
    df = pd.read_csv(FIXTURE, parse_dates=["date"])
    fights = _add_pipeline_aliases(df)
    features = build_feature_matrix(fights)
    if features.empty:
        pytest.skip("Fixture too small")
    return features


def test_evaluate_classification_includes_precision_recall():
    y = np.array([1, 0, 1, 0, 1])
    p = np.array([0.8, 0.2, 0.6, 0.4, 0.9])
    m = evaluate_classification(y, p)
    assert "precision" in m
    assert "recall" in m
    assert m["accuracy"] > 0


def test_build_calibration_bins():
    y = np.array([1, 0, 1, 0, 1, 0, 1, 0, 1, 0])
    p = np.linspace(0.1, 0.9, 10)
    bins = build_calibration_bins(y, p, n_bins=5)
    assert not bins.empty
    assert "mean_predicted" in bins.columns
    assert "fraction_positive" in bins.columns


def test_market_probs_requires_odds_not_implied_only():
    row = pd.Series(
        {
            "implied_prob_f1": 0.55,
            "implied_prob_f2": 0.45,
            "f1_odds": np.nan,
            "f2_odds": np.nan,
        }
    )
    assert _market_probs(row) is None
    assert not _has_valid_odds(row)


def test_market_probs_with_american_odds():
    row = pd.Series({"f1_odds": 200.0, "f2_odds": -245.0})
    market = _market_probs(row)
    assert market is not None
    assert abs(market[0] + market[1] - 1.0) < 1e-6


def test_simulate_value_bets_with_odds():
    preds = pd.DataFrame(
        {
            config.TARGET_COLUMN: [1, 0],
            "prob_f1_win": [0.65, 0.35],
            "prob_f2_win": [0.35, 0.65],
            "f1_odds": [1.8, 2.0],
            "f2_odds": [2.1, 1.7],
            "implied_prob_f1": [0.5, 0.45],
            "implied_prob_f2": [0.5, 0.55],
        }
    )
    trades, summary = simulate_value_bets(
        preds, min_edge=0.08, initial_bankroll=1000, flat_stake=10
    )
    assert "roi_pct" in summary
    assert summary["min_edge"] == pytest.approx(0.08)


def test_style_matchup_bonus_sign():
    row = pd.Series(
        {
            "striker_vs_grappler": 1.0,
            "striker_score_diff": 0.2,
            "southpaw_advantage": 0.08,
            "style_clash": 1.0,
            "grappler_score_diff": -0.1,
            "stance_matchup": 1.0,
        }
    )
    bonus = compute_style_matchup_bonus(row)
    assert bonus > 0
    assert bonus <= config.STYLE_BONUS_MAX


def test_rank_predictions_by_edge():
    preds = pd.DataFrame(
        {
            "fighter_1": ["A", "C"],
            "fighter_2": ["B", "D"],
            "prob_f1_win": [0.6, 0.4],
            "prob_f2_win": [0.4, 0.6],
            "implied_prob_f1": [0.5, 0.55],
            "implied_prob_f2": [0.5, 0.45],
            "predicted_winner": ["A", "D"],
        }
    )
    ranked = rank_predictions_by_edge(preds)
    assert "edge_rank" in ranked.columns
    assert ranked.iloc[0]["best_edge"] >= ranked.iloc[-1]["best_edge"]
