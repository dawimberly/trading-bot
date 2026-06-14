"""Classification and calibration metrics for fight backtests."""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.calibration import calibration_curve
from sklearn.metrics import (
    accuracy_score,
    brier_score_loss,
    log_loss,
    precision_score,
    recall_score,
    roc_auc_score,
)


def evaluate_classification(y_true, proba) -> dict[str, float]:
    y = np.asarray(y_true)
    p = np.asarray(proba)
    mask = np.isfinite(p) & np.isfinite(y)
    if mask.sum() == 0:
        return {
            "accuracy": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "log_loss": float("nan"),
            "brier_score": float("nan"),
            "roc_auc": float("nan"),
            "n_fights": 0.0,
        }

    y = y[mask].astype(int)
    p = p[mask]
    preds = (p >= 0.5).astype(int)
    result = {
        "accuracy": float(accuracy_score(y, preds)),
        "precision": float(precision_score(y, preds, zero_division=0)),
        "recall": float(recall_score(y, preds, zero_division=0)),
        "brier_score": float(brier_score_loss(y, p)),
        "n_fights": float(len(y)),
    }
    if len(np.unique(y)) > 1:
        result["log_loss"] = float(log_loss(y, p))
        result["roc_auc"] = float(roc_auc_score(y, p))
    else:
        result["log_loss"] = float("nan")
        result["roc_auc"] = float("nan")
    return result


def build_calibration_bins(y_true, proba, *, n_bins: int = 10) -> pd.DataFrame:
    y = np.asarray(y_true, dtype=int)
    p = np.asarray(proba, dtype=float)
    mask = np.isfinite(y) & np.isfinite(p)
    y, p = y[mask], p[mask]
    if len(y) < n_bins:
        n_bins = max(2, len(y) // 2)

    try:
        frac_pos, mean_pred = calibration_curve(y, p, n_bins=n_bins, strategy="quantile")
    except ValueError:
        frac_pos, mean_pred = calibration_curve(y, p, n_bins=max(2, n_bins))

    counts = np.zeros(len(mean_pred), dtype=int)
    edges = np.quantile(p, np.linspace(0, 1, len(mean_pred) + 1))
    for i in range(len(mean_pred)):
        lo = edges[i]
        hi = edges[i + 1] if i + 1 < len(edges) else 1.0
        if i == len(mean_pred) - 1:
            counts[i] = int(((p >= lo) & (p <= hi)).sum())
        else:
            counts[i] = int(((p >= lo) & (p < hi)).sum())

    return pd.DataFrame(
        {
            "bin": range(1, len(mean_pred) + 1),
            "mean_predicted": mean_pred,
            "fraction_positive": frac_pos,
            "count": counts,
            "calibration_gap": mean_pred - frac_pos,
        }
    )


def segment_metrics(predictions: pd.DataFrame, mask: pd.Series, target: str, prob_col: str):
    subset = predictions.loc[mask]
    if subset.empty or target not in subset.columns:
        return {"n_fights": 0.0, "accuracy": 0.0, "log_loss": float("nan"), "brier_score": float("nan")}
    return evaluate_classification(subset[target], subset[prob_col])
