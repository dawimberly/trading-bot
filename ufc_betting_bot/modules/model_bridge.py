"""Thin adapter to ufc-predictor for features, model, and fight data."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from ufc_betting_bot.config.settings import (
    PREDICTOR_FEATURES_CSV,
    PREDICTOR_FIGHTS_CSV,
    PREDICTOR_MODEL_PATH,
    PREDICTOR_DIR,
)

if TYPE_CHECKING:
    from src.predictor import FightPredictor


def _ensure_predictor_path() -> Path:
    if not PREDICTOR_DIR.is_dir():
        raise FileNotFoundError(
            f"ufc-predictor not found at {PREDICTOR_DIR}. "
            "Set UFC_PREDICTOR_DIR in .env."
        )
    path = str(PREDICTOR_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)
    return PREDICTOR_DIR


def load_fights() -> pd.DataFrame:
    _ensure_predictor_path()
    from src.data_loader import load_fights as _load

    return _load(PREDICTOR_FIGHTS_CSV)


def load_features() -> pd.DataFrame:
    _ensure_predictor_path()
    from src.data_loader import load_processed_features

    return load_processed_features(PREDICTOR_FEATURES_CSV)


def get_predictor(model_path: Path | None = None) -> FightPredictor:
    _ensure_predictor_path()
    from src.predictor import FightPredictor

    path = model_path or PREDICTOR_MODEL_PATH
    if not path.is_file():
        raise FileNotFoundError(f"No model at {path}. Train ufc-predictor first.")
    return FightPredictor(path)


def model_exists() -> bool:
    return PREDICTOR_MODEL_PATH.is_file()


def save_fights(df: pd.DataFrame) -> None:
    _ensure_predictor_path()
    from src.data_loader import save_fights as _save

    _save(df, PREDICTOR_FIGHTS_CSV)


def rebuild_features(fights: pd.DataFrame) -> pd.DataFrame:
    _ensure_predictor_path()
    from src.feature_engineering import build_feature_matrix, save_features

    features = build_feature_matrix(fights)
    save_features(features, PREDICTOR_FEATURES_CSV)
    return features
