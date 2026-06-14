"""Tests for SHAP explainability helpers."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from lightgbm import LGBMClassifier

from src.explainability import (
    build_reasoning_text,
    explain_prediction,
    resolve_lgbm_from_artifact,
    shap_available,
)


@pytest.fixture
def tiny_lgbm():
    rng = np.random.default_rng(42)
    n = 80
    X = rng.random((n, 4))
    y = (X[:, 0] + X[:, 1] > 1.0).astype(int)
    names = ["elo_diff", "striking_acc_diff", "reach_diff", "td_defense_diff"]
    model = LGBMClassifier(n_estimators=20, max_depth=3, verbosity=-1)
    model.fit(X, y)
    return model, names, X[0]


def test_resolve_lgbm_from_artifact():
    model = LGBMClassifier(n_estimators=5, verbosity=-1)
    artifact = {"base_models": {"lgbm": model}}
    assert resolve_lgbm_from_artifact(artifact) is model


def test_explain_prediction_graceful_without_shap(monkeypatch):
    monkeypatch.setattr("src.explainability._SHAP_AVAILABLE", False)
    out = explain_prediction(
        None,
        np.zeros(3),
        ["a", "b", "c"],
        "Fighter A",
        "Fighter B",
    )
    assert out["available"] is False


@pytest.mark.skipif(not shap_available(), reason="shap not installed")
def test_explain_prediction_returns_drivers(tiny_lgbm):
    model, names, row = tiny_lgbm
    out = explain_prediction(
        model,
        row,
        names,
        "Fighter A",
        "Fighter B",
        prob_f1_win=0.62,
        top_k=4,
    )
    assert out["available"] is True
    assert len(out["top_features"]) == 4
    assert out["predicted_winner"] == "Fighter A"
    text = build_reasoning_text(out)
    assert "Fighter A" in text
    assert "Model favors" in text


@pytest.mark.skipif(not shap_available(), reason="shap not installed")
def test_build_reasoning_with_diff_features(tiny_lgbm):
    model, names, row = tiny_lgbm
    fight_row = pd.Series(dict(zip(names, row)))
    fight_row["fighter_1"] = "Jon Jones"
    fight_row["fighter_2"] = "Stipe Miocic"
    fight_row["prob_f1_win"] = 0.7
    out = explain_prediction(
        model,
        fight_row,
        names,
        "Jon Jones",
        "Stipe Miocic",
        prob_f1_win=0.7,
    )
    reasoning = build_reasoning_text(out)
    assert "Jon Jones" in reasoning or "Stipe Miocic" in reasoning
