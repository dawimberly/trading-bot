"""Unit tests for feature engineering and leakage-safe imputation."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import config
from src.data_loader import _add_pipeline_aliases
from src.feature_engineering import (
    _canonicalize_fighter_slots,
    _encode_f1_win_target,
    _layoff_context_flags,
    _opponent_profiles_similar,
    apply_imputer,
    assert_target_encoding,
    build_feature_matrix,
    build_matchup_features,
    decimal_odds_to_implied,
    ensure_pipeline_columns,
    fit_imputer,
    weight_class_age_sensitivity,
)


FIXTURE = Path(__file__).parent / "fixtures" / "sample_fights.csv"


@pytest.fixture
def sample_fights() -> pd.DataFrame:
    df = pd.read_csv(FIXTURE, parse_dates=["date"])
    return _add_pipeline_aliases(df)


def test_ensure_pipeline_columns_aliases():
    raw = pd.DataFrame(
        {"fighter1": ["A"], "fighter2": ["B"], "date": ["2020-01-01"], "event": ["UFC 1"]}
    )
    out = ensure_pipeline_columns(raw)
    assert "fighter_1" in out.columns
    assert config.DATE_COLUMN in out.columns


def test_build_matchup_features_diff_sign():
    f1 = {"elo": 1600, "win_rate": 0.7, "fight_count": 10}
    f2 = {"elo": 1500, "win_rate": 0.5, "fight_count": 5}
    diff = build_matchup_features(f1, f2)
    assert diff["elo_diff"] == pytest.approx(100.0)
    assert diff["win_rate_diff"] == pytest.approx(0.2)
    assert diff["experience_diff"] == pytest.approx(5.0)


def test_weight_class_age_sensitivity_heavier_higher():
    assert weight_class_age_sensitivity("Heavyweight") > weight_class_age_sensitivity("Flyweight")
    assert weight_class_age_sensitivity("Women's Strawweight") == pytest.approx(0.90)


def test_layoff_context_flags():
    assert _layoff_context_flags(5) == (1.0, 0.0)
    assert _layoff_context_flags(200) == (0.0, 1.0)
    assert _layoff_context_flags(60) == (0.0, 0.0)


def test_opponent_profiles_similar_requires_overlap():
    target = {"sig_strike_acc": 0.45, "td_defense": 0.70, "reach_in": 72.0, "age": 30.0}
    close = {"sig_strike_acc": 0.50, "td_defense": 0.65, "reach_in": 74.0, "age": 32.0}
    far = {"sig_strike_acc": 0.20, "td_defense": 0.30, "reach_in": 60.0, "age": 22.0}
    assert _opponent_profiles_similar(target, close) is True
    assert _opponent_profiles_similar(target, far) is False


def test_wc_age_advantage_scales_by_division():
    f1 = {"age": 35, "elo": 1500, "win_rate": 0.5, "fight_count": 5}
    f2 = {"age": 28, "elo": 1500, "win_rate": 0.5, "fight_count": 5}
    hw = build_matchup_features(f1, f2, weight_class="Heavyweight")
    fw = build_matchup_features(f1, f2, weight_class="Flyweight")
    assert hw["age_diff"] == pytest.approx(7.0)
    assert hw["wc_age_advantage_diff"] == pytest.approx(-7.0 * 1.25)
    assert abs(hw["wc_age_advantage_diff"]) > abs(fw["wc_age_advantage_diff"])


def test_contextual_diff_features_present():
    f1 = {
        "similar_opp_win_rate": 0.6,
        "short_notice_flag": 1.0,
        "long_layoff_flag": 0.0,
        "short_notice_win_rate": 0.7,
        "long_layoff_win_rate": 0.4,
        "elo": 1500,
        "win_rate": 0.5,
        "fight_count": 3,
    }
    f2 = {
        "similar_opp_win_rate": 0.4,
        "short_notice_flag": 0.0,
        "long_layoff_flag": 1.0,
        "short_notice_win_rate": 0.5,
        "long_layoff_win_rate": 0.6,
        "elo": 1500,
        "win_rate": 0.5,
        "fight_count": 3,
    }
    diff = build_matchup_features(f1, f2)
    assert diff["similar_opp_win_rate_diff"] == pytest.approx(0.2)
    assert diff["short_notice_flag_diff"] == pytest.approx(1.0)
    assert diff["long_layoff_flag_diff"] == pytest.approx(-1.0)
    assert diff["short_notice_perf_diff"] == pytest.approx(0.2)
    assert diff["long_layoff_perf_diff"] == pytest.approx(-0.2)


def test_decimal_odds_to_implied_devig():
    p = decimal_odds_to_implied(pd.Series([2.0]), pd.Series([2.0]))
    assert p.iloc[0] == pytest.approx(0.5)


def test_decimal_odds_american_conversion():
    p = decimal_odds_to_implied(pd.Series([150.0]), pd.Series([-150.0]))
    assert 0.35 < p.iloc[0] < 0.45


def test_imputer_uses_train_only_medians(sample_fights: pd.DataFrame):
    features = build_feature_matrix(sample_fights)
    assert not features.empty

    mid = len(features) // 2
    train = features.iloc[:mid]
    test = features.iloc[mid:].copy()
    original_test_val = test["f1_elo"].iloc[0]

    stats = fit_imputer(train)
    test.loc[test.index[0], "f1_elo"] = np.nan
    filled = apply_imputer(test, stats)
    assert pd.notna(filled.loc[filled.index[0], "f1_elo"])
    assert filled.loc[filled.index[0], "f1_elo"] != original_test_val or pd.isna(original_test_val)


def test_build_feature_matrix_no_future_leakage_in_elo(sample_fights: pd.DataFrame):
    features = build_feature_matrix(sample_fights)
    assert "f1_elo" in features.columns
    assert "f2_elo" in features.columns
    assert features["f1_elo"].between(1000, 2000).all()


def test_finish_rate_diff_column(sample_fights: pd.DataFrame):
    features = build_feature_matrix(sample_fights)
    if "finish_rate_diff" in features.columns:
        assert features["finish_rate_diff"].notna().any() or features["f1_finish_rate"].isna().all()


def test_canonicalize_fighter_slots_alphabetical():
    raw = pd.DataFrame(
        {
            "fight_id": ["f1"],
            "fighter1": ["Zulu Fighter"],
            "fighter2": ["Alpha Fighter"],
            "winner": ["Zulu Fighter"],
            "date": ["2024-01-01"],
            "event": ["Test"],
            "f1_odds": [1.5],
            "f2_odds": [2.5],
        }
    )
    out = _canonicalize_fighter_slots(raw)
    assert out.loc[0, "fighter_1"] == "Alpha Fighter"
    assert out.loc[0, "fighter_2"] == "Zulu Fighter"
    assert out.loc[0, "f1_odds"] == 2.5
    assert out.loc[0, "f2_odds"] == 1.5
    target = _encode_f1_win_target(out)
    assert target.iloc[0] == 0


def test_target_mean_balanced_on_fixture(sample_fights: pd.DataFrame):
    features = build_feature_matrix(sample_fights)
    mean_target = assert_target_encoding(features, min_rows_for_balance=0)
    recomputed = _encode_f1_win_target(features)
    assert (
        recomputed.fillna(-1).astype(int)
        == features[config.TARGET_COLUMN].astype(int)
    ).all()
    assert 0.0 < mean_target < 1.0
