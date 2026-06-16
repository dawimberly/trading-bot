"""UFC fight outcome prediction pipeline.

Heavy ML modules are lazy-loaded so the dashboard EXE can bootstrap without
pulling LightGBM/sklearn/scipy into memory at ``import src.*`` time.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "BacktestResult",
    "FightPredictor",
    "OddsAPIError",
    "build_card_features",
    "fetch_ufc_odds",
    "get_fight_explanation",
    "build_feature_matrix",
    "build_matchup_features",
    "evaluate_classification",
    "get_upcoming_card",
    "load_features",
    "load_fights",
    "merge_predictions_with_odds",
    "load_historical_data",
    "load_processed_features",
    "load_trained_model",
    "predict_fight",
    "predict_upcoming_card",
    "prepare_time_splits",
    "run_backtest",
    "run_holdout_backtest",
    "run_training_backtest",
    "simulate_value_bets",
    "sweep_edge_thresholds",
    "save_features",
    "train_model",
    "tune_hyperparameters",
]

_LAZY: dict[str, tuple[str, str]] = {
    "BacktestResult": ("src.backtester", "BacktestResult"),
    "evaluate_classification": ("src.backtester", "evaluate_classification"),
    "run_backtest": ("src.backtester", "run_backtest"),
    "run_holdout_backtest": ("src.backtester", "run_holdout_backtest"),
    "simulate_value_bets": ("src.backtester", "simulate_value_bets"),
    "sweep_edge_thresholds": ("src.backtester", "sweep_edge_thresholds"),
    "get_upcoming_card": ("src.data_loader", "get_upcoming_card"),
    "load_fights": ("src.data_loader", "load_fights"),
    "load_historical_data": ("src.data_loader", "load_historical_data"),
    "load_processed_features": ("src.data_loader", "load_processed_features"),
    "build_feature_matrix": ("src.feature_engineering", "build_feature_matrix"),
    "build_matchup_features": ("src.feature_engineering", "build_matchup_features"),
    "save_features": ("src.feature_engineering", "save_features"),
    "load_trained_model": ("src.model_trainer", "load_trained_model"),
    "prepare_time_splits": ("src.model_trainer", "prepare_time_splits"),
    "run_training_backtest": ("src.model_trainer", "run_training_backtest"),
    "train_model": ("src.model_trainer", "train_model"),
    "tune_hyperparameters": ("src.model_trainer", "tune_hyperparameters"),
    "FightPredictor": ("src.predictor", "FightPredictor"),
    "OddsAPIError": ("src.predictor", "OddsAPIError"),
    "build_card_features": ("src.predictor", "build_card_features"),
    "fetch_ufc_odds": ("src.predictor", "fetch_ufc_odds"),
    "get_fight_explanation": ("src.predictor", "get_fight_explanation"),
    "load_features": ("src.predictor", "load_features"),
    "merge_predictions_with_odds": ("src.predictor", "merge_predictions_with_odds"),
    "predict_fight": ("src.predictor", "predict_fight"),
    "predict_upcoming_card": ("src.predictor", "predict_upcoming_card"),
}


def __getattr__(name: str) -> Any:
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    mod_path, attr = _LAZY[name]
    value = getattr(importlib.import_module(mod_path), attr)
    globals()[name] = value
    return value
