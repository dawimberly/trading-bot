"""Tests for style matchup features and ensemble utilities."""

from __future__ import annotations

import numpy as np
import pytest

import config
from src.ensemble import (
    EnsembleClassifier,
    conformal_quantile,
    ensemble_disagreement,
    fit_conformal_scores,
    prediction_interval,
)
from src.feature_engineering import build_matchup_features, compute_style_scores


def test_compute_style_scores_striker_dominant():
    striker, grappler = compute_style_scores(
        sig_strikes_per_min=5.5,
        sig_strike_acc=0.55,
        ko_rate=0.35,
        td_acc=0.2,
        sub_avg=0.1,
        td_defense=0.6,
    )
    assert striker > grappler
    assert 0 <= striker <= 1


def test_build_matchup_style_features():
    f1 = {
        "sig_strikes_per_min": 5.0,
        "sig_strike_acc": 0.5,
        "ko_rate": 0.3,
        "td_acc": 0.2,
        "sub_avg": 0.1,
        "td_defense": 0.6,
        "stance_southpaw": 1.0,
        "stance_orthodox": 0.0,
        "stance_switch": 0.0,
    }
    f2 = {
        "sig_strikes_per_min": 2.0,
        "sig_strike_acc": 0.4,
        "ko_rate": 0.1,
        "td_acc": 0.5,
        "sub_avg": 0.8,
        "td_defense": 0.7,
        "stance_southpaw": 0.0,
        "stance_orthodox": 1.0,
        "stance_switch": 0.0,
    }
    for k in (
        "age", "height_in", "reach_in", "win_rate", "last5_win_rate", "momentum",
        "control_time_per_min", "elo", "days_since_last_fight", "fight_count",
        "striker_score", "grappler_score",
        "similar_opp_win_rate", "short_notice_flag", "long_layoff_flag",
        "short_notice_win_rate", "long_layoff_win_rate",
    ):
        f1.setdefault(k, 0.5)
        f2.setdefault(k, 0.4)

    diff = build_matchup_features(f1, f2)
    assert "striker_vs_grappler" in diff.index
    assert diff["style_clash"] == pytest.approx(1.0)
    assert diff["southpaw_advantage"] == pytest.approx(0.08)
    assert diff["stance_matchup"] == pytest.approx(1.0)


class _FixedProbaClassifier:
    def __init__(self, p: float):
        self.p = p

    def predict_proba(self, X):
        n = len(X)
        p = np.full(n, self.p)
        return np.column_stack([1 - p, p])


def test_ensemble_classifier_blend():
    m1 = _FixedProbaClassifier(0.6)
    m2 = _FixedProbaClassifier(0.4)
    ens = EnsembleClassifier([m1, m2], weights=[0.5, 0.5], names=["a", "b"])
    X = np.zeros((3, 2))
    proba = ens.predict_proba(X)[:, 1]
    assert np.allclose(proba, 0.5)


def test_conformal_interval_width():
    scores = fit_conformal_scores(np.array([1, 0, 1, 0]), np.array([0.7, 0.3, 0.8, 0.2]))
    q = conformal_quantile(scores, alpha=0.1)
    low, high, width = prediction_interval(np.array([0.65]), conformal_q=q)
    assert low[0] <= 0.65 <= high[0]
    assert width[0] > 0


def test_ensemble_disagreement():
    comps = {"lgbm": np.array([0.7, 0.6]), "xgb": np.array([0.5, 0.55])}
    d = ensemble_disagreement(comps)
    assert d[0] > d[1]


def test_config_includes_style_features():
    assert "striker_vs_grappler" in config.FEATURE_COLUMNS
    assert "sentiment_diff" in config.FEATURE_COLUMNS
    assert "wc_age_advantage_diff" in config.FEATURE_COLUMNS
    assert "similar_opp_win_rate_diff" in config.FEATURE_COLUMNS
    assert "short_notice_perf_diff" in config.FEATURE_COLUMNS
    assert "long_layoff_perf_diff" in config.FEATURE_COLUMNS
