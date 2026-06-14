"""SHAP-based fight prediction explanations (LightGBM TreeExplainer)."""

from __future__ import annotations

import json
import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

try:
    import shap

    _SHAP_AVAILABLE = True
except ImportError:
    shap = None  # type: ignore[assignment]
    _SHAP_AVAILABLE = False


# Human-readable labels for differential features (fighter1 minus fighter2).
_FEATURE_LABELS: dict[str, str] = {
    "elo_diff": "Elo rating",
    "win_rate_diff": "career win rate",
    "last5_winrate_diff": "recent win rate (last 5)",
    "momentum_diff": "momentum",
    "striking_acc_diff": "striking accuracy",
    "sig_strike_acc_diff": "striking accuracy",
    "takedown_acc_diff": "takedown accuracy",
    "td_acc_diff": "takedown accuracy",
    "sub_avg_diff": "submission threat",
    "ko_rate_diff": "KO/finish rate",
    "sig_strikes_per_min_diff": "striking output (per min)",
    "td_defense_diff": "takedown defense",
    "control_time_diff": "control time",
    "age_diff": "age",
    "height_diff": "height",
    "reach_diff": "reach",
    "stance_matchup": "stance matchup",
    "southpaw_advantage": "southpaw edge",
    "striker_score_diff": "striker profile",
    "grappler_score_diff": "grappler profile",
    "striker_vs_grappler": "striker vs grappler style",
    "style_clash": "style clash",
    "days_since_last_fight_diff": "layoff / activity",
    "experience_diff": "UFC experience",
    "sentiment_diff": "news sentiment",
    "is_title_fight": "title fight",
    "is_main_event": "main event",
    "scheduled_rounds": "scheduled rounds",
}

_PERCENT_DIFFS = {
    "striking_acc_diff",
    "sig_strike_acc_diff",
    "takedown_acc_diff",
    "td_acc_diff",
    "win_rate_diff",
    "last5_winrate_diff",
    "ko_rate_diff",
    "td_defense_diff",
    "momentum_diff",
}


def shap_available() -> bool:
    return _SHAP_AVAILABLE


def resolve_lgbm_estimator(model: Any) -> Any | None:
    """Extract an underlying LightGBM booster for TreeExplainer."""
    if model is None:
        return None

    name = type(model).__name__
    if name == "LGBMClassifier":
        return model

    if hasattr(model, "estimator"):
        return resolve_lgbm_estimator(model.estimator)

    if hasattr(model, "calibrated_classifiers_"):
        ccs = model.calibrated_classifiers_
        if ccs:
            return resolve_lgbm_estimator(ccs[0].estimator)

    base_models = getattr(model, "base_models", None)
    if isinstance(base_models, dict) and "lgbm" in base_models:
        return resolve_lgbm_estimator(base_models["lgbm"])

    if hasattr(model, "models") and hasattr(model, "names"):
        for m, label in zip(model.models, model.names):
            if str(label).lower() == "lgbm":
                return resolve_lgbm_estimator(m)

    return None


def resolve_lgbm_from_artifact(artifact: dict[str, Any]) -> Any | None:
    base_models = artifact.get("base_models") or {}
    if isinstance(base_models, dict) and "lgbm" in base_models:
        return resolve_lgbm_estimator(base_models["lgbm"])
    return resolve_lgbm_estimator(artifact.get("base_model") or artifact.get("model"))


_EXPLAINER_CACHE: dict[str, Any] = {}


def get_explainer(artifact: dict[str, Any], *, cache_key: str | None = None) -> Any | None:
    if not _SHAP_AVAILABLE:
        return None
    key = cache_key or str(artifact.get("features_fingerprint", "default"))
    if key in _EXPLAINER_CACHE:
        return _EXPLAINER_CACHE[key]
    estimator = resolve_lgbm_from_artifact(artifact)
    if estimator is None:
        return None
    try:
        explainer = shap.TreeExplainer(estimator)
        _EXPLAINER_CACHE[key] = explainer
        return explainer
    except Exception as exc:
        logger.warning("Could not build SHAP TreeExplainer: %s", exc)
        return None


def _feature_label(name: str) -> str:
    return _FEATURE_LABELS.get(name, name.replace("_", " "))


def _format_feature_value(name: str, value: float) -> str:
    if name in _PERCENT_DIFFS:
        return f"{value * 100:+.1f}%"
    if name in {"reach_diff", "height_diff"}:
        return f"{value:+.1f} in"
    if name == "age_diff":
        return f"{value:+.1f} yr"
    if name == "elo_diff":
        return f"{value:+.0f}"
    if name == "experience_diff":
        return f"{value:+.0f} fights"
    return f"{value:+.3f}"


def explain_prediction(
    model: Any,
    feature_vector: np.ndarray | pd.Series | dict[str, Any],
    feature_names: list[str],
    fighter1_name: str,
    fighter2_name: str,
    *,
    explainer: Any | None = None,
    prob_f1_win: float | None = None,
    top_k: int = 8,
) -> dict[str, Any]:
    """
    SHAP explanation for a single fight (P(fighter1 wins)).

    Returns top positive/negative contributors and waterfall-ready rows.
    """
    if not _SHAP_AVAILABLE:
        return {
            "available": False,
            "error": "shap package not installed",
            "fighter1": fighter1_name,
            "fighter2": fighter2_name,
        }

    estimator = resolve_lgbm_estimator(model)
    if estimator is None:
        return {
            "available": False,
            "error": "no LightGBM estimator found for SHAP",
            "fighter1": fighter1_name,
            "fighter2": fighter2_name,
        }

    if isinstance(feature_vector, dict):
        row = pd.Series(feature_vector)
    elif isinstance(feature_vector, pd.Series):
        row = feature_vector
    else:
        row = pd.Series(feature_vector, index=feature_names)

    x = row[feature_names].astype(float).to_numpy().reshape(1, -1)
    base_value = None
    shap_values = None

    try:
        exp = explainer or shap.TreeExplainer(estimator)
        shap_values = exp.shap_values(x)
        if isinstance(shap_values, list):
            shap_values = shap_values[1] if len(shap_values) > 1 else shap_values[0]
        shap_values = np.asarray(shap_values).reshape(-1)
        base_value = float(np.asarray(exp.expected_value).reshape(-1)[0])
    except Exception as exc:
        logger.debug("SHAP explain failed: %s", exc)
        return {
            "available": False,
            "error": str(exc),
            "fighter1": fighter1_name,
            "fighter2": fighter2_name,
        }

    predicted_f1 = prob_f1_win >= 0.5 if prob_f1_win is not None else None
    predicted_winner = fighter1_name if predicted_f1 else fighter2_name
    if predicted_f1 is None:
        predicted_winner = fighter1_name

    rows: list[dict[str, Any]] = []
    for name, val, impact in zip(feature_names, x.flatten(), shap_values):
        rows.append(
            {
                "feature": name,
                "label": _feature_label(name),
                "value": float(val),
                "value_display": _format_feature_value(name, float(val)),
                "shap": float(impact),
                "abs_shap": float(abs(impact)),
                "direction": "positive" if impact >= 0 else "negative",
            }
        )

    rows.sort(key=lambda r: r["abs_shap"], reverse=True)
    top = rows[:top_k]
    positive = [r for r in rows if r["shap"] > 0][:top_k]
    negative = [r for r in rows if r["shap"] < 0][:top_k]

    if prob_f1_win is not None:
        predicted_f1 = prob_f1_win >= 0.5
        predicted_winner = fighter1_name if predicted_f1 else fighter2_name
        # Contributors toward predicted winner: positive SHAP favors F1, negative favors F2
        if predicted_f1:
            toward_pick = sorted(rows, key=lambda r: r["shap"], reverse=True)[:top_k]
        else:
            toward_pick = sorted(rows, key=lambda r: r["shap"])[:top_k]
    else:
        toward_pick = top

    return {
        "available": True,
        "fighter1": fighter1_name,
        "fighter2": fighter2_name,
        "predicted_winner": predicted_winner,
        "prob_f1_win": prob_f1_win,
        "base_value": base_value,
        "output_value": float(base_value + shap_values.sum()) if base_value is not None else None,
        "top_features": top,
        "top_positive": positive,
        "top_negative": negative,
        "toward_pick": toward_pick,
        "waterfall": _waterfall_rows(base_value, toward_pick),
    }


def _waterfall_rows(
    base_value: float | None,
    contributors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Cumulative waterfall steps for plotting."""
    steps: list[dict[str, Any]] = []
    running = float(base_value or 0.0)
    steps.append({"label": "Base", "value": running, "delta": 0.0})
    for row in contributors:
        delta = float(row["shap"])
        running += delta
        steps.append(
            {
                "label": row["label"],
                "feature": row["feature"],
                "value": running,
                "delta": delta,
            }
        )
    return steps


def _favors_text(
    feature: str,
    value: float,
    *,
    fighter1_name: str,
    fighter2_name: str,
) -> str:
    """Which fighter the raw diff value favors (positive diff -> fighter1)."""
    if abs(value) < 1e-9:
        return "even"
    fav = fighter1_name if value > 0 else fighter2_name
    return fav


def build_reasoning_text(
    explanation: dict[str, Any],
    *,
    max_points: int = 4,
) -> str:
    """Natural-language summary for betting decisions."""
    if not explanation.get("available"):
        return "Explanation unavailable (SHAP not installed or model unsupported)."

    f1 = explanation["fighter1"]
    f2 = explanation["fighter2"]
    pick = explanation.get("predicted_winner", f1)
    prob = explanation.get("prob_f1_win")
    prob_txt = f" ({prob:.0%} model prob)" if prob is not None else ""

    toward = explanation.get("toward_pick") or explanation.get("top_features") or []
    if not toward:
        return f"Model favors {pick}{prob_txt} — no dominant feature drivers identified."

    clauses: list[str] = []
    for row in toward[:max_points]:
        label = row["label"]
        val = float(row["value"])
        impact = float(row["shap"])
        if abs(impact) < 0.005:
            continue
        fav = _favors_text(row["feature"], val, fighter1_name=f1, fighter2_name=f2)
        if fav == "even":
            continue
        if (pick == f1 and impact > 0) or (pick == f2 and impact < 0):
            if row["feature"] in _PERCENT_DIFFS:
                clauses.append(f"{label} edge for {fav} ({row['value_display']})")
            elif row["feature"] in {"reach_diff", "height_diff", "age_diff", "elo_diff"}:
                clauses.append(f"{label} advantage for {fav} ({row['value_display']})")
            else:
                clauses.append(f"stronger {label} for {fav}")

    if not clauses:
        return f"Model favors {pick}{prob_txt} based on combined small feature edges."

    joined = ", ".join(clauses[:max_points])
    return f"Model favors {pick}{prob_txt} due to {joined}."


def explain_fight_row(
    fight_row: pd.Series | dict[str, Any],
    artifact: dict[str, Any],
    feature_names: list[str],
    *,
    explainer: Any | None = None,
    top_k: int = 8,
) -> dict[str, Any]:
    """Convenience wrapper using a scored prediction / feature row."""
    if isinstance(fight_row, dict):
        row = pd.Series(fight_row)
    else:
        row = fight_row

    f1 = str(row.get("fighter_1", row.get("fighter1", "Fighter 1")))
    f2 = str(row.get("fighter_2", row.get("fighter2", "Fighter 2")))
    prob = row.get("prob_f1_win")
    prob_f1 = float(prob) if prob is not None and pd.notna(prob) else None

    estimator = resolve_lgbm_from_artifact(artifact)
    exp = explainer or get_explainer(artifact)

    result = explain_prediction(
        estimator,
        row,
        feature_names,
        f1,
        f2,
        explainer=exp,
        prob_f1_win=prob_f1,
        top_k=top_k,
    )
    result["reasoning"] = build_reasoning_text(result)
    return result


def explanation_to_json(explanation: dict[str, Any]) -> str:
    """Serialize explanation for CSV storage."""
    payload = {
        k: v
        for k, v in explanation.items()
        if k not in {"waterfall"}
    }
    return json.dumps(payload, default=str)


def parse_explanation_json(raw: str | None) -> dict[str, Any]:
    if not raw or (isinstance(raw, float) and pd.isna(raw)):
        return {"available": False}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"available": False}
