"""LightGBM + XGBoost ensemble with uncertainty helpers."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, ClassifierMixin


class EnsembleClassifier(BaseEstimator, ClassifierMixin):
    """Weighted average of calibrated binary classifiers."""

    def __init__(
        self,
        models: list[Any],
        *,
        weights: list[float] | None = None,
        names: list[str] | None = None,
    ) -> None:
        if not models:
            raise ValueError("Ensemble requires at least one model.")
        self.models = models
        self.names = names or [f"model_{i}" for i in range(len(models))]
        raw = np.asarray(weights if weights is not None else [1.0] * len(models), dtype=float)
        total = raw.sum()
        self.weights = raw / total if total > 0 else np.ones(len(models)) / len(models)
        self.classes_ = np.array([0, 1])

    def predict_proba(self, X: pd.DataFrame | np.ndarray) -> np.ndarray:
        component = self.predict_proba_components(X)
        stacked = np.vstack(list(component.values()))
        mean = np.average(stacked, axis=0, weights=self.weights)
        return np.column_stack([1.0 - mean, mean])

    def predict_proba_components(self, X: pd.DataFrame | np.ndarray) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        for name, model in zip(self.names, self.models):
            out[name] = model.predict_proba(X)[:, 1]
        return out

    def predict(self, X: pd.DataFrame | np.ndarray) -> np.ndarray:
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(int)


def fit_conformal_scores(y_true: np.ndarray | pd.Series, proba: np.ndarray) -> np.ndarray:
    """Nonconformity scores for split conformal intervals (true-class probability gap)."""
    y = np.asarray(y_true, dtype=int)
    p = np.asarray(proba, dtype=float)
    return np.where(y == 1, 1.0 - p, p)


def conformal_quantile(scores: np.ndarray, alpha: float) -> float:
    """(1-alpha) quantile with finite-sample correction."""
    n = len(scores)
    if n == 0:
        return 0.15
    level = min(1.0, np.ceil((n + 1) * (1.0 - alpha)) / n)
    return float(np.quantile(scores, level))


def prediction_interval(
    proba: np.ndarray,
    *,
    conformal_q: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Symmetric conformal interval around P(f1 win).

    Returns (ci_low, ci_high, interval_width).
    """
    p = np.asarray(proba, dtype=float)
    q = float(conformal_q)
    low = np.clip(p - q, 0.0, 1.0)
    high = np.clip(p + q, 0.0, 1.0)
    return low, high, high - low


def ensemble_disagreement(component_probs: dict[str, np.ndarray]) -> np.ndarray:
    """Std dev across ensemble members — higher means more model uncertainty."""
    if len(component_probs) < 2:
        return np.zeros(len(next(iter(component_probs.values()))))
    stacked = np.vstack(list(component_probs.values()))
    return np.std(stacked, axis=0)
