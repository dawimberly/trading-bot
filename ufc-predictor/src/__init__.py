"""UFC fight outcome prediction pipeline."""

from src.backtester import (
    BacktestResult,
    evaluate_classification,
    run_backtest,
    run_holdout_backtest,
    simulate_value_bets,
    sweep_edge_thresholds,
)
from src.data_loader import (
    get_upcoming_card,
    load_fights,
    load_historical_data,
    load_processed_features,
)
from src.feature_engineering import (
    build_feature_matrix,
    build_matchup_features,
    save_features,
)
from src.model_trainer import (
    load_trained_model,
    prepare_time_splits,
    run_training_backtest,
    train_model,
    tune_hyperparameters,
)
from src.predictor import (
    FightPredictor,
    OddsAPIError,
    build_card_features,
    fetch_ufc_odds,
    get_fight_explanation,
    load_features,
    merge_predictions_with_odds,
    predict_fight,
    predict_upcoming_card,
)

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
