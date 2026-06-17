"""Inference: load model, score features, predict upcoming cards."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

import config
from src.data_loader import ensure_data_dirs, load_fights, load_processed_features
from src.ensemble import (
    EnsembleClassifier,
    ensemble_disagreement,
    prediction_interval,
)
from src.feature_engineering import apply_imputer, apply_interaction_specs, build_feature_matrix
from src.explainability import (
    build_reasoning_text,
    explain_fight_row,
    explanation_to_json,
    get_explainer,
    shap_available,
)
from src.model_trainer import load_trained_model
from src.sentiment import attach_sentiment_features

logger = logging.getLogger(__name__)


class OddsAPIError(Exception):
    """The Odds API request or parsing failed."""


@dataclass
class PredictionResult:
    fight_id: str
    fighter_1: str
    fighter_2: str
    prob_f1_win: float
    prob_f2_win: float
    predicted_winner: str
    confidence: float
    confidence_label: str


def _confidence_score(prob_f1: float) -> float:
    """0.5 = coin flip, 1.0 = maximum conviction."""
    return float(0.5 + abs(prob_f1 - 0.5))


def _confidence_label(score: float) -> str:
    if score >= config.CONFIDENCE_HIGH:
        return "high"
    if score >= config.CONFIDENCE_MEDIUM:
        return "medium"
    return "low"


def _safe_logit(p: float, eps: float = 1e-6) -> float:
    p = float(np.clip(p, eps, 1.0 - eps))
    return float(np.log(p / (1.0 - p)))


def _inv_logit(x: float) -> float:
    return float(1.0 / (1.0 + np.exp(-x)))


def compute_style_matchup_bonus(row: pd.Series | dict[str, Any]) -> float:
    """
    Rule-based log-odds bonus for F1 from style matchup features.

    Complements learned model features (striker vs grappler, southpaw edge).
    """
    if isinstance(row, dict):
        row = pd.Series(row)

    bonus = 0.0
    max_bonus = config.STYLE_BONUS_MAX

    striker_diff = float(row.get("striker_score_diff", 0) or 0)
    grappler_diff = float(row.get("grappler_score_diff", 0) or 0)
    southpaw_adv = float(row.get("southpaw_advantage", 0) or 0)
    style_clash = float(row.get("style_clash", 0) or 0)
    striker_vs_grappler = float(row.get("striker_vs_grappler", 0) or 0)

    if striker_vs_grappler >= 0.5:
        bonus += 0.04 * np.sign(striker_diff)
    if style_clash >= 0.5:
        bonus += 0.02 * np.sign(grappler_diff)
    bonus += southpaw_adv * 0.5
    if float(row.get("stance_matchup", 0) or 0) >= 0.5:
        bonus += 0.01

    return float(np.clip(bonus, -max_bonus, max_bonus))


def apply_style_calibration(
    frame: pd.DataFrame,
    proba: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Apply style-rule log-odds adjustment then clip to valid probability range."""
    adjusted = []
    bonuses = []
    for i, p in enumerate(proba):
        row = frame.iloc[i]
        bonus = compute_style_matchup_bonus(row)
        bonuses.append(bonus)
        adjusted.append(_inv_logit(_safe_logit(float(p)) + bonus))
    return np.asarray(adjusted, dtype=float), np.asarray(bonuses, dtype=float)


def attach_edge_columns(preds: pd.DataFrame) -> pd.DataFrame:
    """Compute model vs implied edge for both sides."""
    out = preds.copy()
    for col in ("edge_f1", "edge_f2", "edge_pct", "best_edge"):
        if col not in out.columns:
            out[col] = np.nan
    if "best_edge_side" not in out.columns:
        out["best_edge_side"] = ""

    for idx, row in out.iterrows():
        p1 = float(row.get("prob_f1_win", 0.5))
        p2 = float(row.get("prob_f2_win", 1.0 - p1))
        imp1 = imp2 = np.nan
        if pd.notna(row.get("implied_prob_f1")):
            imp1 = float(row["implied_prob_f1"])
            imp2 = float(row.get("implied_prob_f2", 1.0 - imp1))
        elif pd.notna(row.get("f1_odds")) and pd.notna(row.get("f2_odds")):
            from src.feature_engineering import decimal_odds_to_implied

            imp1 = float(
                decimal_odds_to_implied(
                    pd.Series([row["f1_odds"]]), pd.Series([row["f2_odds"]])
                ).iloc[0]
            )
            imp2 = 1.0 - imp1

        if np.isfinite(imp1):
            e1, e2 = p1 - imp1, p2 - imp2
            out.at[idx, "edge_f1"] = e1
            out.at[idx, "edge_f2"] = e2
            best_side = "f1" if e1 >= e2 else "f2"
            best_edge = e1 if best_side == "f1" else e2
            out.at[idx, "best_edge"] = best_edge
            out.at[idx, "best_edge_side"] = best_side
            pick_f1 = p1 >= 0.5
            out.at[idx, "edge_pct"] = (e1 if pick_f1 else e2) * 100.0

    return out


def resolve_edge_thresholds(
    predictions_df: pd.DataFrame,
    *,
    bankroll: float | None = None,
    recent_win_rate: float | None = None,
    hours_to_event: float | None = None,
    use_dynamic: bool | None = None,
) -> dict[str, float | bool | None]:
    """Resolve single/parlay edge floors (static profile or dynamic adjustment)."""
    import config as _cfg
    from ufc_betting_bot.modules.dynamic_thresholds import (
        get_profile_thresholds,
        hours_to_event_from_row,
        model_confidence_from_predictions,
    )

    enabled = _cfg.DYNAMIC_THRESHOLDS_ENABLED if use_dynamic is None else use_dynamic
    if not enabled:
        ps = _cfg.profile_settings()
        return {
            "use_dynamic": False,
            "alert_min_edge": ps["alert_min_edge"],
            "parlay_min_edge": ps["parlay_min_edge"],
            "parlay_min_combined_prob": ps["parlay_min_combined_prob"],
            "parlay_min_ev": ps["parlay_min_ev"],
            "thresholds": None,
        }

    br = bankroll if bankroll is not None else _cfg.INITIAL_BANKROLL
    hte = hours_to_event
    if hte is None and predictions_df is not None and not predictions_df.empty:
        hte = hours_to_event_from_row(predictions_df.iloc[0])
    conf = model_confidence_from_predictions(predictions_df)
    thresholds = get_profile_thresholds(
        br,
        recent_win_rate,
        conf,
        hours_to_event=hte,
        profile=_cfg.UFC_PROFILE,
    )
    return {
        "use_dynamic": True,
        "alert_min_edge": thresholds.alert_min_edge,
        "parlay_min_edge": thresholds.parlay_min_edge,
        "parlay_min_combined_prob": thresholds.parlay_min_combined_prob,
        "parlay_min_ev": thresholds.parlay_min_ev,
        "model_confidence": conf,
        "hours_to_event": hte,
        "thresholds": thresholds.as_dict(),
    }


def rank_predictions_by_edge(
    preds: pd.DataFrame,
    *,
    min_edge: float | None = None,
    ascending: bool = False,
) -> pd.DataFrame:
    """Sort fights by best available edge (model_prob - implied_prob)."""
    work = attach_edge_columns(preds)
    edge_floor = config.EDGE_RANK_MIN if min_edge is None else min_edge
    if "best_edge" in work.columns:
        work["rank_edge"] = work["best_edge"]
    else:
        work["rank_edge"] = np.nan

    ranked = work.sort_values(
        "rank_edge",
        ascending=ascending,
        na_position="last",
    ).reset_index(drop=True)
    ranked["edge_rank"] = range(1, len(ranked) + 1)
    if edge_floor > 0 and "best_edge" in ranked.columns:
        ranked = ranked[ranked["best_edge"].fillna(-1) >= edge_floor].reset_index(drop=True)
    return ranked


def _fighter_name(row: pd.Series, n: int) -> str:
    for key in (f"fighter_{n}", f"fighter{n}"):
        if key in row and pd.notna(row[key]) and str(row[key]).strip():
            return str(row[key]).strip()
    return f"Fighter {n}"


def _make_card_fight_id(row: pd.Series) -> str:
    existing = row.get(config.FIGHT_ID_COLUMN) or row.get("fight_id")
    if existing and str(existing).strip():
        return str(existing).strip()
    event = str(row.get("event", row.get("event_name", "")))
    date = str(row.get(config.DATE_COLUMN, row.get("date", "")))
    f1 = _fighter_name(row, 1)
    f2 = _fighter_name(row, 2)
    key = f"{event}|{date}|{f1}|{f2}".lower()
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _infer_event_date(row: pd.Series) -> pd.Timestamp:
    """Resolve fight date from row fields; parse UFC event URLs when date is missing."""
    for col in (config.DATE_COLUMN, "date"):
        if col in row.index and pd.notna(row[col]) and str(row[col]).strip():
            ts = pd.to_datetime(row[col], errors="coerce")
            if pd.notna(ts):
                return ts.normalize()

    for col in ("event_url", "url"):
        url = row.get(col)
        if url and pd.notna(url):
            m = re.search(
                r"(january|february|march|april|may|june|july|august|september|october|november|december)"
                r"-(\d{1,2})-(\d{4})",
                str(url).lower(),
            )
            if m:
                month_name, day, year = m.group(1), int(m.group(2)), int(m.group(3))
                month = pd.Timestamp(f"{month_name} 1 2000").month
                return pd.Timestamp(year=year, month=month, day=day)

    return pd.Timestamp.utcnow().normalize()


def _normalize_card(card: pd.DataFrame) -> pd.DataFrame:
    """Map upcoming-card columns onto the fights pipeline schema."""
    work = card.copy()
    rename = {
        "fighter1": "fighter_1",
        "fighter2": "fighter_2",
        "date": config.DATE_COLUMN,
        "event": "event_name",
    }
    for src, dst in rename.items():
        if src in work.columns and dst not in work.columns:
            work[dst] = work[src]

    if "event_name" not in work.columns and "event" in work.columns:
        work["event_name"] = work["event"]

    if config.DATE_COLUMN in work.columns:
        work[config.DATE_COLUMN] = pd.to_datetime(work[config.DATE_COLUMN], errors="coerce")
    else:
        work[config.DATE_COLUMN] = pd.NaT

    work[config.DATE_COLUMN] = work.apply(_infer_event_date, axis=1)

    work["fighter_1"] = work.apply(lambda r: _fighter_name(r, 1), axis=1)
    work["fighter_2"] = work.apply(lambda r: _fighter_name(r, 2), axis=1)
    work[config.FIGHT_ID_COLUMN] = work.apply(_make_card_fight_id, axis=1)
    work["winner"] = ""
    work["weight_class"] = work.get("weight_class", pd.Series("Unknown", index=work.index))
    return work


def build_card_features(
    card: pd.DataFrame,
    *,
    historical_fights: pd.DataFrame | None = None,
    attach_sentiment: bool | None = None,
) -> pd.DataFrame:
    """
    Engineer model features for an upcoming card using fight history.

    Appends card rows to history, runs leakage-safe feature engineering,
    and returns only the upcoming matchups.
    """
    history = historical_fights if historical_fights is not None else load_fights()
    normalized = _normalize_card(card)
    card_ids = set(normalized[config.FIGHT_ID_COLUMN].tolist())

    hist_cols = set(history.columns)
    rows: list[dict[str, Any]] = []
    for _, row in normalized.iterrows():
        payload = {k: row.get(k) for k in hist_cols if k in row.index}
        for col in (
            config.FIGHT_ID_COLUMN,
            config.DATE_COLUMN,
            "fighter_1",
            "fighter_2",
            "winner",
            "weight_class",
            "event_name",
            "is_title_fight",
            "is_main_event",
        ):
            if col in row.index:
                payload[col] = row[col]
        rows.append(payload)

    upcoming = pd.DataFrame(rows)
    if config.DATE_COLUMN in upcoming.columns:
        upcoming[config.DATE_COLUMN] = pd.to_datetime(
            upcoming[config.DATE_COLUMN], errors="coerce"
        )
        fill_val = pd.Timestamp.utcnow().normalize()
        if upcoming[config.DATE_COLUMN].isna().all():
            upcoming[config.DATE_COLUMN] = fill_val
        else:
            upcoming[config.DATE_COLUMN] = upcoming[config.DATE_COLUMN].fillna(fill_val)

    combined = pd.concat([history, upcoming], ignore_index=True)
    features = build_feature_matrix(combined, keep_unlabeled=True)
    card_features = features[features[config.FIGHT_ID_COLUMN].isin(card_ids)].copy()
    do_sentiment = (
        attach_sentiment
        if attach_sentiment is not None
        else config.ATTACH_SENTIMENT_ON_INFERENCE
    )
    if do_sentiment and config.NEWS_API_KEY:
        card_features = attach_sentiment_features(card_features)

    from src.card_cache import touch_history_cache

    touch_history_cache()
    return card_features


class FightPredictor:
    """Load a trained model and run batch or card inference."""

    def __init__(self, model_path: Path | str | None = None) -> None:
        artifact = load_trained_model(model_path)
        self.artifact = artifact
        self.model = artifact["model"]
        self.feature_columns: list[str] = artifact["feature_columns"]
        self.metrics: dict[str, float] = artifact.get("metrics", {})
        self.calibration_method: str = artifact.get("calibration_method", "isotonic")
        self.imputer = artifact.get("imputer")
        self.interaction_specs = artifact.get("interaction_specs") or []
        self.conformal_q = float(artifact.get("conformal_q", 0.15))
        self.ensemble_weights = artifact.get("ensemble_weights")
        self.model_type = artifact.get("model_type", "lgbm")
        self.model_path = Path(model_path) if model_path else config.DEFAULT_MODEL_PATH
        self._shap_explainer = None
        self._shap_cache_key = str(artifact.get("features_fingerprint", str(self.model_path)))

    def _prepare_features(self, X: pd.DataFrame) -> pd.DataFrame:
        frame = X.copy()
        if self.interaction_specs:
            frame = apply_interaction_specs(frame, self.interaction_specs)
        missing = [c for c in self.feature_columns if c not in frame.columns]
        if missing:
            raise ValueError(f"Feature matrix missing columns: {missing}")
        if self.imputer is not None:
            frame = apply_imputer(frame, self.imputer)
        elif frame[self.feature_columns].isna().any().any():
            from src.feature_engineering import _impute_feature_matrix

            logger.warning(
                "Model artifact has no imputer; using legacy on-the-fly fill (retrain recommended)."
            )
            frame = _impute_feature_matrix(frame)
        still_missing = frame[self.feature_columns].isna().any(axis=1)
        if still_missing.any():
            logger.warning(
                "Dropping %s rows with unresolved NaN features at inference",
                int(still_missing.sum()),
            )
            frame = frame.loc[~still_missing].copy()
        return frame

    def _score_proba(self, X: pd.DataFrame) -> np.ndarray:
        prepared = self._prepare_features(X)
        if prepared.empty:
            raise ValueError("No rows left after feature imputation for scoring.")
        return self.model.predict_proba(prepared[self.feature_columns])[:, 1]

    def _uncertainty_label(self, interval_width: float, disagreement: float) -> str:
        if interval_width >= config.UNCERTAINTY_HIGH_WIDTH or disagreement >= 0.08:
            return "high"
        if interval_width >= config.UNCERTAINTY_HIGH_WIDTH * 0.6 or disagreement >= 0.04:
            return "medium"
        return "low"

    def _attach_predictions(
        self,
        frame: pd.DataFrame,
        proba: np.ndarray,
        *,
        prepared: pd.DataFrame | None = None,
    ) -> pd.DataFrame:
        out = frame.copy()
        out["prob_f1_win"] = proba
        out["prob_f2_win"] = 1.0 - proba

        ci_low, ci_high, ci_width = prediction_interval(proba, conformal_q=self.conformal_q)
        out["prob_ci_low"] = ci_low
        out["prob_ci_high"] = ci_high
        out["interval_width"] = ci_width

        disagree = np.zeros(len(proba))
        score_frame = prepared if prepared is not None else frame
        if isinstance(self.model, EnsembleClassifier):
            components = self.model.predict_proba_components(
                score_frame[self.feature_columns]
            )
            disagree = ensemble_disagreement(components)
            out["prob_lgbm"] = components.get("lgbm", proba)
            out["prob_xgb"] = components.get("xgb", proba)
        out["ensemble_disagreement"] = disagree

        out["confidence"] = [_confidence_score(p) for p in proba]
        out["confidence_label"] = [_confidence_label(c) for c in out["confidence"]]
        out["uncertainty_label"] = [
            self._uncertainty_label(w, d) for w, d in zip(ci_width, disagree)
        ]
        f1 = out.get("fighter_1", out.get("fighter1", pd.Series("Fighter 1", index=out.index)))
        f2 = out.get("fighter_2", out.get("fighter2", pd.Series("Fighter 2", index=out.index)))
        out["predicted_winner"] = np.where(out["prob_f1_win"] >= 0.5, f1, f2)
        out["predicted_prob"] = np.where(
            out["prob_f1_win"] >= 0.5,
            out["prob_f1_win"],
            out["prob_f2_win"],
        )
        pick_f1 = out["prob_f1_win"] >= 0.5
        out["predicted_ci_low"] = np.where(pick_f1, ci_low, 1.0 - ci_high)
        out["predicted_ci_high"] = np.where(pick_f1, ci_high, 1.0 - ci_low)
        return out

    def _shap_explainer_instance(self):
        if self._shap_explainer is None:
            self._shap_explainer = get_explainer(
                self.artifact,
                cache_key=self._shap_cache_key,
            )
        return self._shap_explainer

    def explain_row(
        self,
        row: pd.Series | dict[str, Any],
        *,
        top_k: int = 8,
    ) -> dict[str, Any]:
        """SHAP explanation dict for one scored feature row."""
        return explain_fight_row(
            row,
            self.artifact,
            self.feature_columns,
            explainer=self._shap_explainer_instance(),
            top_k=top_k,
        )

    def get_fight_explanation(self, fight_row: pd.Series | dict[str, Any]) -> dict[str, Any]:
        """
        Human-readable reasoning for a prediction row.

        Returns keys: reasoning, top_features, toward_pick, prob_f1_win, predicted_winner, ...
        """
        explanation = self.explain_row(fight_row)
        if not explanation.get("available"):
            explanation.setdefault(
                "reasoning",
                "Explanation unavailable — install shap or retrain model with LightGBM base.",
            )
        return explanation

    def attach_shap_explanations(
        self,
        preds: pd.DataFrame,
        *,
        top_k: int = 8,
    ) -> pd.DataFrame:
        """Add shap_explanation (JSON) and reasoning columns to predictions."""
        out = preds.copy()
        if not shap_available():
            out["shap_explanation"] = None
            out["reasoning"] = "SHAP not installed (pip install shap)."
            return out

        explainer = self._shap_explainer_instance()
        if explainer is None:
            out["shap_explanation"] = None
            out["reasoning"] = "SHAP unavailable for this model artifact."
            return out

        explanations: list[str] = []
        reasonings: list[str] = []
        for _, row in out.iterrows():
            exp = explain_fight_row(
                row,
                self.artifact,
                self.feature_columns,
                explainer=explainer,
                top_k=top_k,
            )
            explanations.append(explanation_to_json(exp))
            reasonings.append(exp.get("reasoning") or build_reasoning_text(exp))
        out["shap_explanation"] = explanations
        out["reasoning"] = reasonings
        return out

    def predict_row(self, row: pd.Series | dict[str, Any]) -> PredictionResult:
        """Predict outcome for one feature row."""
        if isinstance(row, dict):
            row = pd.Series(row)

        prob_f1 = float(self._score_proba(pd.DataFrame([row]))[0])
        prob_f2 = 1.0 - prob_f1
        conf = _confidence_score(prob_f1)
        fighter_1 = _fighter_name(row, 1)
        fighter_2 = _fighter_name(row, 2)

        return PredictionResult(
            fight_id=str(row.get(config.FIGHT_ID_COLUMN, "")),
            fighter_1=fighter_1,
            fighter_2=fighter_2,
            prob_f1_win=prob_f1,
            prob_f2_win=prob_f2,
            predicted_winner=fighter_1 if prob_f1 >= 0.5 else fighter_2,
            confidence=conf,
            confidence_label=_confidence_label(conf),
        )

    def predict_batch(
        self,
        features: pd.DataFrame,
        *,
        apply_style_bonus: bool = True,
        explain: bool = False,
    ) -> pd.DataFrame:
        """Score a feature matrix; returns input rows plus prediction columns."""
        prepared = self._prepare_features(features)
        if prepared.empty:
            return features.iloc[0:0].copy()
        proba = self.model.predict_proba(prepared[self.feature_columns])[:, 1]
        if apply_style_bonus:
            proba, bonuses = apply_style_calibration(prepared, proba)
            out = self._attach_predictions(prepared, proba, prepared=prepared)
            out["style_bonus"] = bonuses
            out["prob_f1_win_raw"] = self.model.predict_proba(
                prepared[self.feature_columns]
            )[:, 1]
        else:
            out = self._attach_predictions(prepared, proba, prepared=prepared)
        if explain:
            out = self.attach_shap_explanations(out)
        return out

    def predict_upcoming_card(
        self,
        card: pd.DataFrame,
        *,
        historical_fights: pd.DataFrame | None = None,
        attach_odds: bool = False,
        attach_sentiment: bool | None = None,
        force_refresh_odds: bool = False,
        explain: bool = False,
        use_cache: bool = True,
        event_name: str | None = None,
    ) -> pd.DataFrame:
        """
        Build features for an upcoming card and return predictions.

        Output columns include event, fighters, predicted_winner, prob, confidence.
        When ``attach_odds=True``, merges The Odds API lines and edge columns.
        Uses per-event cache when ``use_cache=True`` (fast repeat runs).
        """
        fights = historical_fights if historical_fights is not None else load_fights()
        ev_name = event_name
        if not ev_name:
            normalized = _normalize_card(card)
            if "event_name" in normalized.columns and normalized["event_name"].notna().any():
                ev_name = str(normalized["event_name"].dropna().iloc[0])
            elif "event" in normalized.columns and normalized["event"].notna().any():
                ev_name = str(normalized["event"].dropna().iloc[0])
            else:
                ev_name = "Upcoming card"

        if use_cache:
            from src.card_cache import predict_card_cached

            out = predict_card_cached(
                card,
                fights,
                ev_name,
                explain=explain,
                use_cache=True,
            )
        else:
            card_features = build_card_features(
                card,
                historical_fights=fights,
                attach_sentiment=attach_sentiment,
            )
            if card_features.empty:
                raise ValueError(
                    "No upcoming fights could be featurized. "
                    "Fighters may lack enough UFC history."
                )
            out = self.predict_batch(card_features, explain=explain)
            normalized = _normalize_card(card)
            meta = normalized[
                [
                    c
                    for c in [
                        config.FIGHT_ID_COLUMN,
                        "event",
                        "event_name",
                        config.DATE_COLUMN,
                        "date",
                        "location",
                        "weight_class",
                        "bout_order",
                    ]
                    if c in normalized.columns
                ]
            ]
            out = out.merge(meta, on=config.FIGHT_ID_COLUMN, how="left", suffixes=("", "_card"))
            if "event_name" not in out.columns:
                out["event_name"] = ev_name

        if "event" not in out.columns:
            out["event"] = out.get("event_name", "")
        if config.DATE_COLUMN not in out.columns and "date" in out.columns:
            out[config.DATE_COLUMN] = out["date"]

        if attach_odds:
            out = merge_predictions_with_odds(
                out,
                force_refresh=force_refresh_odds,
            )
        out = rank_predictions_by_edge(out)
        return out.reset_index(drop=True)


def _american_to_decimal(price: float) -> float:
    if price >= 0:
        return 1.0 + price / 100.0
    return 1.0 + 100.0 / abs(price)


def _to_decimal_odds(price: float, odds_format: str) -> float:
    if odds_format == "american":
        return _american_to_decimal(price)
    return float(price)


def _fighter_name_key(name: str) -> str:
    text = str(name or "").lower().strip()
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = re.sub(r"[^a-z0-9'\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _names_match(a: str, b: str) -> bool:
    """Loose fighter name match (handles missing first names / punctuation)."""
    ka, kb = _fighter_name_key(a), _fighter_name_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    if ka in kb or kb in ka:
        return True
    a_parts = set(ka.split())
    b_parts = set(kb.split())
    if len(a_parts.intersection(b_parts)) >= 2:
        return True
    # Last-name match when one side is single token
    a_last = ka.split()[-1]
    b_last = kb.split()[-1]
    return a_last == b_last and len(a_last) > 3


def _match_fighters_to_event(
    fighter_1: str,
    fighter_2: str,
    home_team: str,
    away_team: str,
) -> tuple[bool, bool]:
    """
    Return whether fighter_1 maps to home_team and fighter_2 to away_team.

    Second value is False when fighters are swapped vs home/away.
    """
    if _names_match(fighter_1, home_team) and _names_match(fighter_2, away_team):
        return True, True
    if _names_match(fighter_1, away_team) and _names_match(fighter_2, home_team):
        return False, True
    return True, False


def _implied_probs(f1_odds: float, f2_odds: float) -> tuple[float, float]:
    if f1_odds <= 1 or f2_odds <= 1:
        return np.nan, np.nan
    p1 = 1.0 / f1_odds
    p2 = 1.0 / f2_odds
    total = p1 + p2
    if total <= 0:
        return np.nan, np.nan
    return p1 / total, p2 / total


def _odds_cache_fresh() -> bool:
    path = config.ODDS_CACHE_PATH
    if not path.is_file() or config.ODDS_CACHE_TTL_HOURS <= 0:
        return False
    age_h = (time.time() - path.stat().st_mtime) / 3600
    return age_h < config.ODDS_CACHE_TTL_HOURS


def fetch_ufc_odds(*, force_refresh: bool = False) -> pd.DataFrame:
    """
    Fetch current UFC/MMA h2h odds from The Odds API.

    Requires ``THE_ODDS_API_KEY`` (or ``ODDS_API_KEY``) in environment / .env.
    Results are cached to ``data/cache/ufc_odds_api.csv``.

    Returns columns: event_id, commence_time, fighter_1, fighter_2,
    f1_odds, f2_odds, implied_prob_f1, implied_prob_f2, bookmaker_count.
    """
    ensure_data_dirs()
    if not config.ODDS_API_KEY:
        raise OddsAPIError(
            "Missing Odds API key. Set THE_ODDS_API_KEY in your .env file."
        )

    if not force_refresh and _odds_cache_fresh():
        cached = pd.read_csv(config.ODDS_CACHE_PATH, parse_dates=["commence_time"])
        if not cached.empty:
            logger.info("Using cached Odds API data (%s rows)", len(cached))
            return cached

    url = f"{config.ODDS_API_BASE_URL}/sports/{config.ODDS_API_SPORT}/odds"
    params = {
        "apiKey": config.ODDS_API_KEY,
        "regions": config.ODDS_API_REGIONS,
        "markets": config.ODDS_API_MARKETS,
        "oddsFormat": config.ODDS_API_ODDS_FORMAT,
    }

    try:
        response = requests.get(url, params=params, timeout=config.REQUEST_TIMEOUT_SEC)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise OddsAPIError(f"Odds API request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise OddsAPIError("Odds API returned invalid JSON") from exc

    if not isinstance(payload, list):
        raise OddsAPIError(f"Unexpected Odds API response: {type(payload)}")

    rows: list[dict[str, Any]] = []
    for event in payload:
        home = str(event.get("home_team", "")).strip()
        away = str(event.get("away_team", "")).strip()
        if not home or not away:
            continue

        f1_prices: list[float] = []
        f2_prices: list[float] = []

        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                prices: dict[str, float] = {}
                for outcome in market.get("outcomes", []):
                    name = str(outcome.get("name", "")).strip()
                    price = outcome.get("price")
                    if name and price is not None:
                        prices[name] = _to_decimal_odds(
                            float(price), config.ODDS_API_ODDS_FORMAT
                        )
                home_px = next(
                    (px for nm, px in prices.items() if _names_match(nm, home)), None
                )
                away_px = next(
                    (px for nm, px in prices.items() if _names_match(nm, away)), None
                )
                if home_px and away_px and home_px > 1 and away_px > 1:
                    f1_prices.append(home_px)
                    f2_prices.append(away_px)

        if not f1_prices:
            continue

        f1_odds = float(np.mean(f1_prices))
        f2_odds = float(np.mean(f2_prices))
        imp1, imp2 = _implied_probs(f1_odds, f2_odds)
        rows.append(
            {
                "event_id": event.get("id", ""),
                "commence_time": event.get("commence_time"),
                "fighter_1": home,
                "fighter_2": away,
                "f1_odds": round(f1_odds, 3),
                "f2_odds": round(f2_odds, 3),
                "implied_prob_f1": imp1,
                "implied_prob_f2": imp2,
                "bookmaker_count": len(f1_prices),
            }
        )

    odds_df = pd.DataFrame(rows)
    if odds_df.empty:
        raise OddsAPIError("No UFC h2h odds returned from The Odds API.")

    if "commence_time" in odds_df.columns:
        odds_df["commence_time"] = pd.to_datetime(odds_df["commence_time"], errors="coerce")

    odds_df.to_csv(config.ODDS_CACHE_PATH, index=False)
    logger.info("Fetched %s UFC odds lines from The Odds API", len(odds_df))
    return odds_df


def _lookup_odds_row(
    fighter_1: str,
    fighter_2: str,
    odds: pd.DataFrame,
) -> pd.Series | None:
    for _, row in odds.iterrows():
        aligned, matched = _match_fighters_to_event(
            fighter_1,
            fighter_2,
            str(row["fighter_1"]),
            str(row["fighter_2"]),
        )
        if matched:
            out = row.copy()
            if not aligned:
                out["f1_odds"], out["f2_odds"] = row["f2_odds"], row["f1_odds"]
                out["implied_prob_f1"], out["implied_prob_f2"] = (
                    row["implied_prob_f2"],
                    row["implied_prob_f1"],
                )
            return out
    return None


def merge_predictions_with_odds(
    predictions: pd.DataFrame,
    odds: pd.DataFrame | None = None,
    *,
    fetch_if_missing: bool = True,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Attach market odds and edge (model prob − implied prob) to predictions.

    Adds: f1_odds, f2_odds, implied_prob_f1, implied_prob_f2,
    edge_f1, edge_f2, edge_pct, best_edge_side, odds_matched.
    """
    if predictions.empty:
        return predictions.copy()

    market = odds
    if market is None or market.empty:
        if not fetch_if_missing:
            out = predictions.copy()
            out["odds_matched"] = False
            return out
        try:
            market = fetch_ufc_odds(force_refresh=force_refresh)
        except OddsAPIError as exc:
            logger.warning("Odds merge skipped: %s", exc)
            out = predictions.copy()
            out["odds_matched"] = False
            return out

    out = predictions.copy()
    extra_cols = [
        "f1_odds",
        "f2_odds",
        "implied_prob_f1",
        "implied_prob_f2",
        "edge_f1",
        "edge_f2",
        "edge_pct",
        "best_edge_side",
        "bookmaker_count",
        "odds_matched",
    ]
    for col in extra_cols:
        if col == "odds_matched":
            out[col] = False
        elif col == "best_edge_side":
            out[col] = pd.Series([None] * len(out), dtype=object, index=out.index)
        else:
            out[col] = np.nan

    for idx, row in out.iterrows():
        f1 = _fighter_name(row, 1)
        f2 = _fighter_name(row, 2)
        match = _lookup_odds_row(f1, f2, market)
        if match is None:
            continue

        f1_odds = float(match["f1_odds"])
        f2_odds = float(match["f2_odds"])
        imp1 = float(match["implied_prob_f1"])
        imp2 = float(match["implied_prob_f2"])
        model_p1 = float(row.get("prob_f1_win", 0.5))
        model_p2 = float(row.get("prob_f2_win", 1.0 - model_p1))
        edge_f1 = model_p1 - imp1
        edge_f2 = model_p2 - imp2

        winner_is_f1 = model_p1 >= 0.5
        edge_pct = (edge_f1 if winner_is_f1 else edge_f2) * 100.0
        best_side = "f1" if edge_f1 >= edge_f2 else "f2"

        out.at[idx, "f1_odds"] = f1_odds
        out.at[idx, "f2_odds"] = f2_odds
        out.at[idx, "implied_prob_f1"] = imp1
        out.at[idx, "implied_prob_f2"] = imp2
        out.at[idx, "edge_f1"] = edge_f1
        out.at[idx, "edge_f2"] = edge_f2
        out.at[idx, "edge_pct"] = edge_pct
        out.at[idx, "best_edge_side"] = best_side
        out.at[idx, "bookmaker_count"] = match.get("bookmaker_count", np.nan)
        out.at[idx, "odds_matched"] = True

    return out


def load_features(path: Path | str | None = None) -> pd.DataFrame:
    """Load processed feature matrix from disk."""
    return load_processed_features(path)


def predict_fight(
    features: pd.DataFrame | pd.Series | dict[str, Any],
    *,
    model_path: Path | str | None = None,
) -> PredictionResult | pd.DataFrame:
    """Convenience wrapper for one row or many."""
    predictor = FightPredictor(model_path)
    if isinstance(features, pd.DataFrame):
        return predictor.predict_batch(features)
    return predictor.predict_row(features)


def get_fight_explanation(
    fight_row: pd.Series | dict[str, Any],
    *,
    model_path: Path | str | None = None,
) -> dict[str, Any]:
    """Module-level wrapper for natural-language fight explanation."""
    return FightPredictor(model_path).get_fight_explanation(fight_row)


def predict_upcoming_card(
    card: pd.DataFrame,
    *,
    model_path: Path | str | None = None,
    historical_fights: pd.DataFrame | None = None,
    attach_odds: bool = False,
    attach_sentiment: bool | None = None,
    force_refresh_odds: bool = False,
    explain: bool = False,
) -> pd.DataFrame:
    """Score an upcoming card end-to-end."""
    return FightPredictor(model_path).predict_upcoming_card(
        card,
        historical_fights=historical_fights,
        attach_odds=attach_odds,
        attach_sentiment=attach_sentiment,
        force_refresh_odds=force_refresh_odds,
        explain=explain,
    )


def resolve_analysis_targets(
    event_query: str | list[str] | None = None,
    *,
    next_two: bool = False,
    last_two: bool = False,
    include_adjacent_week: bool = True,
) -> list[tuple[int, str]]:
    """Delegate to main.resolve_event_targets (multi-card CLI / EXE)."""
    from main import resolve_event_targets

    targets = resolve_event_targets(
        event_query,
        next_two=next_two,
        last_two=last_two,
        include_adjacent_week=include_adjacent_week,
    )
    deduped: list[tuple[int, str]] = []
    seen_idx: set[int] = set()
    seen_names: set[str] = set()
    for event_index, event_name in targets:
        if event_index in seen_idx:
            logger.warning("resolve_analysis_targets: skip duplicate index %d (%r)", event_index, event_name)
            continue
        if event_name in seen_names:
            logger.warning("resolve_analysis_targets: skip duplicate name %r", event_name)
            continue
        seen_idx.add(event_index)
        seen_names.add(event_name)
        deduped.append((event_index, event_name))
    for idx, (event_index, event_name) in enumerate(deduped):
        logger.info(
            "Loaded Card %d: %s - (resolved, index %d)",
            idx,
            event_name,
            event_index,
        )
    return deduped


def predict_analysis_cards(
    targets: list[tuple[int, str]],
    *,
    historical_fights: pd.DataFrame | None = None,
    model_path: Path | str | None = None,
    attach_odds: bool = False,
    force_refresh_odds: bool = False,
    explain: bool = False,
    refresh_cards: bool = False,
) -> list[dict[str, Any]]:
    """Score multiple upcoming cards; returns per-card prediction frames."""
    from src.data_loader import get_upcoming_card

    hist = historical_fights if historical_fights is not None else load_fights()
    predictor = FightPredictor(model_path)
    out: list[dict[str, Any]] = []
    for event_index, event_name in targets:
        card = get_upcoming_card(event_index=event_index, force_refresh=refresh_cards)
        preds = predictor.predict_upcoming_card(
            card,
            historical_fights=hist,
            attach_odds=attach_odds,
            force_refresh_odds=force_refresh_odds,
            explain=explain,
        )
        if "event_name" not in preds.columns:
            preds["event_name"] = event_name
        else:
            preds["event_name"] = preds["event_name"].fillna(event_name)
        out.append(
            {
                "event_index": event_index,
                "event_name": event_name,
                "predictions": preds,
            }
        )
    return out
