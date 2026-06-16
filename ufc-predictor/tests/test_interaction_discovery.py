"""Tests for automatic interaction feature generation and discovery."""

from __future__ import annotations

import pandas as pd
import pytest

from src.feature_engineering import (
    INTERACTION_SPECS,
    apply_interaction_specs,
    build_interaction_candidates,
    build_matchup_features,
    interaction_candidate_names,
)
from src.model_trainer import discover_interaction_features


def test_build_interaction_candidates_products():
    f1 = {
        "age": 30,
        "reach_in": 72,
        "elo": 1500,
        "win_rate": 0.6,
        "fight_count": 5,
        "last5_win_rate": 0.8,
        "days_since_last_fight": 200,
        "sig_strike_acc": 0.5,
        "td_defense": 0.7,
        "striker_score": 0.6,
        "grappler_score": 0.3,
        "stance_southpaw": 1.0,
        "stance_orthodox": 0.0,
        "long_layoff_flag": 1.0,
        "short_notice_flag": 0.0,
        "short_notice_win_rate": 0.5,
        "long_layoff_win_rate": 0.6,
        "similar_opp_win_rate": 0.55,
        "momentum": 0.5,
        "control_time_per_min": 1.0,
        "sig_strikes_per_min": 4.0,
        "td_acc": 0.3,
        "sub_avg": 0.1,
        "ko_rate": 0.2,
    }
    f2 = dict(f1)
    f2.update({"age": 28, "reach_in": 70, "elo": 1480, "stance_southpaw": 0.0, "stance_orthodox": 1.0})
    diff = build_matchup_features(f1, f2, weight_class="Welterweight")
    frame = pd.DataFrame([diff])
    out = build_interaction_candidates(frame)
    assert "ix_age_x_reach" in out.columns
    assert out["ix_age_x_reach"].iloc[0] == pytest.approx(diff["age_diff"] * diff["reach_diff"])


def test_discover_interaction_features_selects_top():
    rows = []
    y = []
    for i in range(120):
        base = {
            "elo_diff": float(i - 60),
            "win_rate_diff": 0.1 * (i % 5),
            "striking_acc_diff": 0.05 * i,
            "td_defense_diff": 0.02 * (120 - i),
            "last5_winrate_diff": 0.1 if i > 60 else -0.1,
            "days_since_last_fight_diff": float(i),
            "age_diff": 2.0,
            "reach_diff": 1.0,
            "wc_age_advantage_diff": 1.5,
            "striker_score_diff": 0.2,
            "grappler_score_diff": -0.1,
            "southpaw_advantage": 0.08,
            "long_layoff_flag_diff": 1.0 if i % 3 == 0 else 0.0,
            "stance_matchup": 1.0 if i % 2 else 0.0,
            "style_clash": 0.5,
            "short_notice_flag_diff": 0.0,
            "short_notice_perf_diff": 0.0,
            "momentum_diff": 0.1,
            "similar_opp_win_rate_diff": 0.05,
            "long_layoff_perf_diff": 0.0,
            "takedown_acc_diff": 0.0,
            "striker_vs_grappler": 0.0,
            "experience_diff": 1.0,
            "control_time_diff": 0.0,
            "height_diff": 0.0,
            "ko_rate_diff": 0.0,
            "sub_avg_diff": 0.0,
            "sig_strikes_per_min_diff": 0.0,
            "sentiment_diff": 0.0,
            "is_title_fight": 0,
            "is_main_event": 0,
            "scheduled_rounds": 3,
        }
        frame = build_interaction_candidates(pd.DataFrame([base]))
        rows.append(frame.iloc[0].to_dict())
        y.append(1 if i > 60 else 0)
    train_df = pd.DataFrame(rows)
    train_df["f1_win"] = y
    base_cols = [c for c in train_df.columns if not str(c).startswith("ix_")]
    candidates = interaction_candidate_names()
    selected, insights = discover_interaction_features(
        train_df,
        base_cols,
        candidates,
        train_df["f1_win"],
        min_select=4,
        max_select=8,
    )
    assert 4 <= len(selected) <= 8
    assert insights
    assert all("message" in row for row in insights)


def test_apply_interaction_specs_inference():
    df = pd.DataFrame([{"age_diff": 2.0, "reach_diff": 3.0}])
    specs = [s for s in INTERACTION_SPECS if s.name == "ix_age_x_reach"]
    out = apply_interaction_specs(df, specs)
    assert out["ix_age_x_reach"].iloc[0] == pytest.approx(6.0)
