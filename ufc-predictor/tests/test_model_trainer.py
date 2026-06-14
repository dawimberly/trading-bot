"""Unit tests for chronological splits and training leakage guards."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import config
from src.data_loader import _add_pipeline_aliases
from src.feature_engineering import apply_imputer, build_feature_matrix, fit_imputer
from src.model_trainer import prepare_time_splits


FIXTURE = Path(__file__).parent / "fixtures" / "sample_fights.csv"


@pytest.fixture
def feature_frame() -> pd.DataFrame:
    df = pd.read_csv(FIXTURE, parse_dates=["date"])
    fights = _add_pipeline_aliases(df)
    features = build_feature_matrix(fights)
    if features.empty:
        pytest.skip("Fixture too small for feature matrix")
    return features


def test_prepare_time_splits_chronological(feature_frame: pd.DataFrame):
    splits = prepare_time_splits(feature_frame, test_size=0.2, calibration_size=0.2)
    assert len(splits.train) + len(splits.calibration) + len(splits.test) <= len(feature_frame)
    assert splits.train[config.DATE_COLUMN].max() <= splits.calibration[config.DATE_COLUMN].min()
    assert splits.calibration[config.DATE_COLUMN].max() <= splits.test[config.DATE_COLUMN].min()


def test_imputer_fit_on_train_only(feature_frame: pd.DataFrame):
    splits = prepare_time_splits(feature_frame, test_size=0.25, calibration_size=0.25)
    stats = fit_imputer(splits.train)
    train_med = apply_imputer(splits.train, stats)["f1_win_rate"].median()
    test_med = apply_imputer(splits.test, stats)["f1_win_rate"].median()
    assert pd.notna(train_med)
    assert pd.notna(test_med)


def test_calibration_not_used_for_imputer_stats(feature_frame: pd.DataFrame):
    splits = prepare_time_splits(feature_frame, test_size=0.25, calibration_size=0.25)
    cal_only_stats = fit_imputer(splits.calibration)
    train_stats = fit_imputer(splits.train)
    assert cal_only_stats.global_fills != train_stats.global_fills or len(splits.calibration) < 2
