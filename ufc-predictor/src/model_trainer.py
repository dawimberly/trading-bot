"""Train, tune, calibrate, and persist UFC fight outcome models."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from typing import Any, Literal

import joblib
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.calibration import CalibratedClassifierCV
from xgboost import XGBClassifier
from sklearn.metrics import (
    accuracy_score,
    brier_score_loss,
    log_loss,
    roc_auc_score,
)

import config
from src.data_loader import ensure_data_dirs
from src.ensemble import (
    EnsembleClassifier,
    conformal_quantile,
    ensemble_disagreement,
    fit_conformal_scores,
    prediction_interval,
)
from src.feature_engineering import apply_imputer, assert_target_encoding, fit_imputer

logger = logging.getLogger(__name__)

TuningMethod = Literal["none", "optuna", "grid"]
CalibrationMethod = Literal["isotonic", "sigmoid"]

# Compact grid when Optuna is unavailable.
GRID_SEARCH_SPACE: dict[str, list[Any]] = {
    "num_leaves": [15, 31, 63],
    "learning_rate": [0.03, 0.05, 0.1],
    "min_child_samples": [10, 20, 40],
    "feature_fraction": [0.7, 0.9],
    "bagging_fraction": [0.7, 0.9],
    "reg_lambda": [0.0, 1.0, 5.0],
}


@dataclass
class TrainingResult:
    model_path: Path
    metrics: dict[str, float]
    feature_columns: list[str]
    best_params: dict[str, Any] = field(default_factory=dict)
    calibration_method: str = "isotonic"
    feature_importance: dict[str, float] = field(default_factory=dict)
    backtest_summary: dict[str, float] | None = None


@dataclass
class TimeSplitData:
    train: pd.DataFrame
    calibration: pd.DataFrame
    test: pd.DataFrame
    feature_columns: list[str]


def _available_features(df: pd.DataFrame) -> list[str]:
    cols = [c for c in config.FEATURE_COLUMNS if c in df.columns]
    return [c for c in cols if df[c].notna().any()]


def _training_fingerprint(features: pd.DataFrame) -> str:
    from src.model_freshness import features_fingerprint

    save_features_path = config.PROCESSED_FEATURES_CSV
    if save_features_path.is_file():
        return features_fingerprint(save_features_path)
    payload = f"{len(features)}|{sorted(features.columns)}"
    import hashlib

    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _enrichment_timestamp_iso() -> str | None:
    from src.model_freshness import enrichment_timestamp

    ts = enrichment_timestamp()
    return ts.isoformat() if ts else None


def _sort_chronologically(df: pd.DataFrame) -> pd.DataFrame:
    if config.DATE_COLUMN in df.columns:
        return df.sort_values(config.DATE_COLUMN).reset_index(drop=True)
    return df.reset_index(drop=True)


def prepare_time_splits(
    features: pd.DataFrame,
    *,
    test_size: float | None = None,
    calibration_size: float | None = None,
) -> TimeSplitData:
    """
    Chronological train / calibration / test split (past → future).

    Train: earliest fights
    Calibration: middle chunk (for Platt / isotonic — no test leakage)
    Test: most recent holdout
    """
    feature_cols = _available_features(features)
    if not feature_cols:
        raise ValueError("No configured feature columns found in dataframe.")

    df = features.dropna(subset=[config.TARGET_COLUMN]).copy()
    if df.empty:
        raise ValueError("No rows with valid target available for splitting.")

    df = _sort_chronologically(df)
    n = len(df)
    test_ratio = test_size if test_size is not None else config.TEST_SIZE
    cal_ratio = calibration_size if calibration_size is not None else config.CALIBRATION_SIZE

    test_count = max(1, int(n * test_ratio))
    cal_count = max(1, int(n * cal_ratio))
    train_end = n - test_count - cal_count
    if train_end < 1:
        raise ValueError(
            f"Not enough rows ({n}) for train/cal/test split. "
            "Reduce test_size or calibration_size."
        )

    train = df.iloc[:train_end].copy()
    calibration = df.iloc[train_end : train_end + cal_count].copy()
    test = df.iloc[train_end + cal_count :].copy()
    return TimeSplitData(
        train=train,
        calibration=calibration,
        test=test,
        feature_columns=feature_cols,
    )


def _build_lgbm(params: dict[str, Any] | None = None, *, n_estimators: int | None = None) -> LGBMClassifier:
    merged = {**config.LGBM_PARAMS, **(params or {})}
    trees = n_estimators or int(merged.pop("n_estimators", 300))
    return LGBMClassifier(n_estimators=trees, **merged)


def _build_xgb(params: dict[str, Any] | None = None, *, n_estimators: int | None = None) -> XGBClassifier:
    merged = {**config.XGB_PARAMS, **(params or {})}
    trees = n_estimators or int(merged.pop("n_estimators", 300))
    return XGBClassifier(n_estimators=trees, **merged)


def _compute_metrics(y_true: pd.Series | np.ndarray, proba: np.ndarray) -> dict[str, float]:
    y = np.asarray(y_true)
    p = np.asarray(proba)
    preds = (p >= 0.5).astype(int)
    return {
        "accuracy": float(accuracy_score(y, preds)),
        "roc_auc": float(roc_auc_score(y, p)),
        "log_loss": float(log_loss(y, p)),
        "brier_score": float(brier_score_loss(y, p)),
    }


def _fit_base_model(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame | None,
    y_val: pd.Series | None,
    params: dict[str, Any] | None,
    *,
    backend: Literal["lgbm", "xgb"] = "lgbm",
) -> LGBMClassifier | XGBClassifier:
    if backend == "xgb":
        model = _build_xgb(params)
        fit_kwargs: dict[str, Any] = {"verbose": False}
        if X_val is not None and y_val is not None and len(X_val) > 0:
            fit_kwargs["eval_set"] = [(X_val, y_val)]
        model.fit(X_train, y_train, **fit_kwargs)
        return model

    model = _build_lgbm(params)
    lgbm_kwargs: dict[str, Any] = {}
    if X_val is not None and y_val is not None and len(X_val) > 0:
        lgbm_kwargs["eval_set"] = [(X_val, y_val)]
        lgbm_kwargs["eval_metric"] = "binary_logloss"
    model.fit(X_train, y_train, **lgbm_kwargs)
    return model


def _wrap_calibrated_model(
    base_model: LGBMClassifier | XGBClassifier,
    X_cal: pd.DataFrame,
    y_cal: pd.Series,
    *,
    method: CalibrationMethod,
) -> CalibratedClassifierCV:
    """Wrap a pre-fit booster with isotonic or Platt (sigmoid) calibration."""
    try:
        from sklearn.frozen import FrozenEstimator

        estimator = FrozenEstimator(base_model)
        calibrated = CalibratedClassifierCV(estimator=estimator, method=method)
    except ImportError:
        calibrated = CalibratedClassifierCV(
            estimator=base_model,
            method=method,
            cv="prefit",
        )
    calibrated.fit(X_cal, y_cal)
    return calibrated


def tune_hyperparameters(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    *,
    method: TuningMethod = "optuna",
    n_trials: int | None = None,
    random_state: int | None = None,
) -> dict[str, Any]:
    """
    Tune LightGBM hyperparameters on a chronological validation fold.

    Uses Optuna when available; falls back to grid search otherwise.
    """
    if method == "none":
        return {}

    rs = random_state if random_state is not None else config.RANDOM_STATE
    trials = n_trials if n_trials is not None else config.OPTUNA_TRIALS

    if method == "optuna":
        try:
            import optuna

            optuna.logging.set_verbosity(optuna.logging.WARNING)

            def objective(trial: optuna.Trial) -> float:
                trial_params = {
                    "num_leaves": trial.suggest_int("num_leaves", 15, 96),
                    "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
                    "min_child_samples": trial.suggest_int("min_child_samples", 5, 80),
                    "feature_fraction": trial.suggest_float("feature_fraction", 0.5, 1.0),
                    "bagging_fraction": trial.suggest_float("bagging_fraction", 0.5, 1.0),
                    "bagging_freq": trial.suggest_int("bagging_freq", 1, 10),
                    "reg_alpha": trial.suggest_float("reg_alpha", 1e-3, 10.0, log=True),
                    "reg_lambda": trial.suggest_float("reg_lambda", 1e-3, 10.0, log=True),
                    "n_estimators": trial.suggest_int("n_estimators", 100, 600),
                }
                model = _build_lgbm(trial_params)
                model.fit(
                    X_train,
                    y_train,
                    eval_set=[(X_val, y_val)],
                    eval_metric="binary_logloss",
                )
                proba = model.predict_proba(X_val)[:, 1]
                return log_loss(y_val, proba)

            study = optuna.create_study(
                direction="minimize",
                sampler=optuna.samplers.TPESampler(seed=rs),
            )
            study.optimize(objective, n_trials=trials, show_progress_bar=False)
            logger.info("Optuna best log_loss=%.4f params=%s", study.best_value, study.best_params)
            return study.best_params
        except ImportError:
            logger.warning("Optuna not installed; falling back to grid search.")

    return _grid_search(X_train, y_train, X_val, y_val, backend="lgbm")


def tune_ensemble_hyperparameters(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    *,
    method: TuningMethod = "optuna",
    n_trials: int | None = None,
    random_state: int | None = None,
) -> dict[str, Any]:
    """
    Optuna study for LightGBM + XGBoost + ensemble blend weight.

    Trading-bot style: minimize validation log loss across the blended model.
    """
    if method == "none":
        return {"lgbm": {}, "xgb": {}, "ensemble_weights": config.DEFAULT_ENSEMBLE_WEIGHTS}

    rs = random_state if random_state is not None else config.RANDOM_STATE
    trials = n_trials if n_trials is not None else config.OPTUNA_TRIALS

    if method == "optuna":
        try:
            import optuna

            optuna.logging.set_verbosity(optuna.logging.WARNING)

            def objective(trial: optuna.Trial) -> float:
                lgbm_params = {
                    "num_leaves": trial.suggest_int("lgbm_num_leaves", 15, 96),
                    "learning_rate": trial.suggest_float("lgbm_learning_rate", 0.01, 0.2, log=True),
                    "min_child_samples": trial.suggest_int("lgbm_min_child_samples", 5, 80),
                    "feature_fraction": trial.suggest_float("lgbm_feature_fraction", 0.5, 1.0),
                    "bagging_fraction": trial.suggest_float("lgbm_bagging_fraction", 0.5, 1.0),
                    "reg_lambda": trial.suggest_float("lgbm_reg_lambda", 1e-3, 10.0, log=True),
                    "n_estimators": trial.suggest_int("lgbm_n_estimators", 100, 500),
                }
                xgb_params = {
                    "max_depth": trial.suggest_int("xgb_max_depth", 3, 10),
                    "learning_rate": trial.suggest_float("xgb_learning_rate", 0.01, 0.2, log=True),
                    "subsample": trial.suggest_float("xgb_subsample", 0.5, 1.0),
                    "colsample_bytree": trial.suggest_float("xgb_colsample_bytree", 0.5, 1.0),
                    "reg_lambda": trial.suggest_float("xgb_reg_lambda", 1e-3, 10.0, log=True),
                    "n_estimators": trial.suggest_int("xgb_n_estimators", 100, 500),
                }
                w_lgbm = trial.suggest_float("ensemble_lgbm_weight", 0.25, 0.75)

                lgbm = _build_lgbm(lgbm_params)
                lgbm.fit(X_train, y_train, eval_set=[(X_val, y_val)], eval_metric="binary_logloss")
                xgb = _build_xgb(xgb_params)
                xgb.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=False)

                p = (
                    w_lgbm * lgbm.predict_proba(X_val)[:, 1]
                    + (1.0 - w_lgbm) * xgb.predict_proba(X_val)[:, 1]
                )
                return log_loss(y_val, p)

            study = optuna.create_study(
                direction="minimize",
                sampler=optuna.samplers.TPESampler(seed=rs),
            )
            study.optimize(objective, n_trials=trials, show_progress_bar=False)
            best = study.best_params
            lgbm_best = {k.replace("lgbm_", ""): v for k, v in best.items() if k.startswith("lgbm_")}
            xgb_best = {k.replace("xgb_", ""): v for k, v in best.items() if k.startswith("xgb_")}
            weights = [best.get("ensemble_lgbm_weight", 0.55), 1.0 - best.get("ensemble_lgbm_weight", 0.55)]
            logger.info(
                "Optuna ensemble best log_loss=%.4f lgbm_w=%.2f",
                study.best_value,
                weights[0],
            )
            return {"lgbm": lgbm_best, "xgb": xgb_best, "ensemble_weights": weights}
        except ImportError:
            logger.warning("Optuna not installed; falling back to single-model grid search.")
            lgbm = tune_hyperparameters(X_train, y_train, X_val, y_val, method="grid")
            return {"lgbm": lgbm, "xgb": {}, "ensemble_weights": config.DEFAULT_ENSEMBLE_WEIGHTS}

    lgbm = _grid_search(X_train, y_train, X_val, y_val, backend="lgbm")
    xgb = _grid_search(X_train, y_train, X_val, y_val, backend="xgb")
    return {"lgbm": lgbm, "xgb": xgb, "ensemble_weights": config.DEFAULT_ENSEMBLE_WEIGHTS}


def _grid_search(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    *,
    backend: Literal["lgbm", "xgb"] = "lgbm",
) -> dict[str, Any]:
    """Exhaustive grid over ``GRID_SEARCH_SPACE`` using validation log loss."""
    keys = list(GRID_SEARCH_SPACE.keys())
    best_loss = float("inf")
    best_params: dict[str, Any] = {}

    combos = list(product(*[GRID_SEARCH_SPACE[k] for k in keys]))
    logger.info("Grid search (%s): %s combinations", backend, len(combos))

    for values in combos:
        trial_params = dict(zip(keys, values))
        n_estimators = int(
            trial_params.pop(
                "n_estimators",
                config.LGBM_PARAMS.get("n_estimators", 300)
                if backend == "lgbm"
                else config.XGB_PARAMS.get("n_estimators", 300),
            )
        )
        if backend == "xgb":
            mapped = {
                "max_depth": 6,
                "subsample": trial_params.get("bagging_fraction", 0.8),
                "colsample_bytree": trial_params.get("feature_fraction", 0.85),
                "reg_lambda": trial_params.get("reg_lambda", 1.0),
                "learning_rate": trial_params.get("learning_rate", 0.05),
            }
            model = _build_xgb(mapped, n_estimators=n_estimators)
        else:
            model = _build_lgbm(trial_params, n_estimators=n_estimators)
        model.fit(X_train, y_train)
        proba = model.predict_proba(X_val)[:, 1]
        loss = log_loss(y_val, proba)
        if loss < best_loss:
            best_loss = loss
            best_params = {**trial_params, "n_estimators": n_estimators} if backend == "lgbm" else {
                **mapped,
                "n_estimators": n_estimators,
            }

    logger.info("Grid search (%s) best log_loss=%.4f", backend, best_loss)
    return best_params


def _feature_importance(
    model: LGBMClassifier | CalibratedClassifierCV,
    feature_columns: list[str],
) -> dict[str, float]:
    """Extract LightGBM gain-based importances."""
    estimator = model
    if isinstance(model, CalibratedClassifierCV):
        estimator = model.calibrated_classifiers_[0].estimator
        if hasattr(estimator, "estimator"):
            estimator = estimator.estimator

    if not hasattr(estimator, "feature_importances_"):
        return {}

    values = np.asarray(estimator.feature_importances_, dtype=float)
    total = values.sum()
    if total > 0:
        values = values / total
    importance = dict(sorted(
        zip(feature_columns, values),
        key=lambda kv: kv[1],
        reverse=True,
    ))
    return {k: float(v) for k, v in importance.items()}


def _save_feature_importance(importance: dict[str, float]) -> Path:
    ensure_data_dirs()
    out = config.FEATURE_IMPORTANCE_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "importance": importance,
        "top_features": list(importance.keys())[:15],
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out


def run_training_backtest(
    test_features: pd.DataFrame,
    *,
    model_path: Path | str | None = None,
) -> dict[str, float] | None:
    """
    Backtesting hook: run edge simulation on the chronological test slice.

    Returns None when odds columns are unavailable.
    """
    if test_features.empty:
        return None
    has_odds = (
        {"f1_odds", "f2_odds"}.issubset(test_features.columns)
        or "implied_prob_f1" in test_features.columns
    )
    if not has_odds:
        logger.info("Skipping training backtest: no odds columns in test set.")
        return None

    from src.backtester import run_backtest

    result = run_backtest(
        test_features,
        model_path=str(model_path) if model_path else None,
        holdout_only=False,
    )
    return {**result.classification, **{f"bt_{k}": v for k, v in result.summary.items()}}


def save_model(artifact: dict[str, Any], path: Path | str | None = None) -> Path:
    """Persist a full model artifact with joblib."""
    ensure_data_dirs()
    out = Path(path) if path else config.DEFAULT_MODEL_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, out)
    return out


def load_trained_model(path: Path | str | None = None) -> dict[str, Any]:
    """Load model artifact written by train_model."""
    model_path = Path(path) if path else config.DEFAULT_MODEL_PATH
    if not model_path.is_file() and config.LEGACY_MODEL_PATH.is_file():
        logger.info("Using legacy model path: %s", config.LEGACY_MODEL_PATH)
        model_path = config.LEGACY_MODEL_PATH
    if not model_path.is_file():
        raise FileNotFoundError(f"Model not found: {model_path}")
    return joblib.load(model_path)


def train_model(
    features: pd.DataFrame,
    *,
    model_path: Path | str | None = None,
    test_size: float | None = None,
    calibration_size: float | None = None,
    params: dict[str, Any] | None = None,
    tune: TuningMethod | bool = "none",
    calibration_method: CalibrationMethod | None = None,
    run_backtest_hook: bool | None = None,
) -> TrainingResult:
    """
    Train a calibrated ensemble (LightGBM + XGBoost) on past fights only.

    Pipeline:
    1. Chronological train / calibration / test split
    2. Train-only imputation (no leakage)
    3. Optuna tuning of both boosters + blend weight (when ``tune='optuna'``)
    4. Fit + calibrate each model; blend into ``EnsembleClassifier``
    5. Conformal uncertainty bands from calibration split
    6. Evaluate on held-out test set + optional backtest hook
    """
    ensure_data_dirs()
    out_path = Path(model_path) if model_path else config.DEFAULT_MODEL_PATH
    assert_target_encoding(features)

    if isinstance(tune, bool):
        tune_method: TuningMethod = "optuna" if tune else "none"
    else:
        tune_method = tune

    cal_method: CalibrationMethod = (
        calibration_method
        if calibration_method is not None
        else config.CALIBRATION_METHOD
    )
    do_backtest = (
        run_backtest_hook
        if run_backtest_hook is not None
        else config.RUN_BACKTEST_ON_TRAIN
    )

    splits = prepare_time_splits(
        features,
        test_size=test_size,
        calibration_size=calibration_size,
    )
    feature_cols = splits.feature_columns

    imputer = fit_imputer(splits.train)
    train_df = apply_imputer(splits.train, imputer)
    cal_df = apply_imputer(splits.calibration, imputer)
    test_df = apply_imputer(splits.test, imputer)

    for name, part in (("train", train_df), ("calibration", cal_df), ("test", test_df)):
        missing = int(part[feature_cols].isna().any(axis=1).sum())
        if missing:
            logger.debug(
                "Dropping %s rows with NaN features after imputation (%s split)",
                missing,
                name,
            )
    train_df = train_df.dropna(subset=feature_cols).copy()
    cal_df = cal_df.dropna(subset=feature_cols).copy()
    test_df = test_df.dropna(subset=feature_cols).copy()
    if train_df.empty or cal_df.empty or test_df.empty:
        raise ValueError("Empty split after imputation — check feature coverage.")

    X_train = train_df[feature_cols]
    y_train = train_df[config.TARGET_COLUMN]
    X_cal = cal_df[feature_cols]
    y_cal = cal_df[config.TARGET_COLUMN]
    X_test = test_df[feature_cols]
    y_test = test_df[config.TARGET_COLUMN]

    # Tuning / early stopping use a tail of train only — calibration set stays holdout.
    tune_val_count = max(1, int(len(X_train) * 0.15))
    X_tune_train = X_train.iloc[:-tune_val_count]
    y_tune_train = y_train.iloc[:-tune_val_count]
    X_tune_val = X_train.iloc[-tune_val_count:]
    y_tune_val = y_train.iloc[-tune_val_count:]

    use_ensemble = config.USE_ENSEMBLE
    best_params: dict[str, Any] = dict(params or {})
    ensemble_weights = list(config.DEFAULT_ENSEMBLE_WEIGHTS)

    if tune_method != "none" and use_ensemble:
        tuned = tune_ensemble_hyperparameters(
            X_tune_train,
            y_tune_train,
            X_tune_val,
            y_tune_val,
            method=tune_method,
        )
        best_params = {**best_params, **tuned}
        ensemble_weights = tuned.get("ensemble_weights", ensemble_weights)
    elif tune_method != "none":
        lgbm_tuned = tune_hyperparameters(
            X_tune_train,
            y_tune_train,
            X_tune_val,
            y_tune_val,
            method=tune_method,
        )
        best_params = {**best_params, "lgbm": lgbm_tuned}

    lgbm_params = best_params.get("lgbm", best_params if not use_ensemble else {})
    xgb_params = best_params.get("xgb", {})

    base_lgbm = _fit_base_model(
        X_train, y_train, X_tune_val, y_tune_val, lgbm_params, backend="lgbm"
    )
    cal_lgbm = _wrap_calibrated_model(base_lgbm, X_cal, y_cal, method=cal_method)

    if use_ensemble:
        base_xgb = _fit_base_model(
            X_train, y_train, X_tune_val, y_tune_val, xgb_params, backend="xgb"
        )
        cal_xgb = _wrap_calibrated_model(base_xgb, X_cal, y_cal, method=cal_method)
        calibrated = EnsembleClassifier(
            [cal_lgbm, cal_xgb],
            weights=ensemble_weights,
            names=["lgbm", "xgb"],
        )
        base_model = base_lgbm
    else:
        base_xgb = None
        cal_xgb = None
        calibrated = cal_lgbm
        base_model = base_lgbm

    proba = calibrated.predict_proba(X_test)[:, 1]
    metrics = _compute_metrics(y_test, proba)
    metrics.update(
        {
            "train_rows": float(len(splits.train)),
            "calibration_rows": float(len(splits.calibration)),
            "test_rows": float(len(splits.test)),
            "calibration_method": cal_method,
            "tuning_method": tune_method,
            "model_type": "ensemble" if use_ensemble else "lgbm",
        }
    )

    cal_proba = calibrated.predict_proba(X_cal)[:, 1]
    conformal_scores = fit_conformal_scores(y_cal.to_numpy(), cal_proba)
    conformal_q = conformal_quantile(conformal_scores, config.CONFORMAL_ALPHA)
    ci_low, ci_high, ci_width = prediction_interval(proba, conformal_q=conformal_q)
    metrics["conformal_q"] = conformal_q
    metrics["mean_interval_width"] = float(np.mean(ci_width))

    if use_ensemble and isinstance(calibrated, EnsembleClassifier):
        disagree = ensemble_disagreement(calibrated.predict_proba_components(X_test))
        metrics["mean_ensemble_disagreement"] = float(np.mean(disagree))

    importance = _feature_importance(base_model, feature_cols)
    _save_feature_importance(importance)

    artifact = {
        "model": calibrated,
        "base_model": base_model,
        "base_models": {"lgbm": base_lgbm, "xgb": base_xgb},
        "calibrated_models": {"lgbm": cal_lgbm, "xgb": cal_xgb},
        "feature_columns": feature_cols,
        "target_column": config.TARGET_COLUMN,
        "imputer": imputer,
        "metrics": metrics,
        "best_params": best_params,
        "ensemble_weights": ensemble_weights if use_ensemble else None,
        "calibration_method": cal_method,
        "conformal_q": conformal_q,
        "conformal_alpha": config.CONFORMAL_ALPHA,
        "feature_importance": importance,
        "backtest_summary": None,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "model_type": "ensemble" if use_ensemble else "lgbm",
        "feature_rows": len(features),
        "features_fingerprint": _training_fingerprint(features),
        "enrichment_at": _enrichment_timestamp_iso(),
    }
    save_model(artifact, out_path)

    backtest_summary = None
    if do_backtest:
        backtest_summary = run_training_backtest(test_df, model_path=out_path)
        if backtest_summary:
            metrics.update(backtest_summary)
            artifact["metrics"] = metrics
            artifact["backtest_summary"] = backtest_summary
            save_model(artifact, out_path)

    config.METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.METRICS_PATH.write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    return TrainingResult(
        model_path=out_path,
        metrics=metrics,
        feature_columns=feature_cols,
        best_params=best_params,
        calibration_method=cal_method,
        feature_importance=importance,
        backtest_summary=backtest_summary,
    )
