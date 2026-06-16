"""Tests for incremental fighter stats cache."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import config
from src.data_loader import _add_pipeline_aliases
from src.feature_engineering import build_feature_matrix
from src.fighter_cache import (
    build_features_with_cache,
    build_full_cache,
    ensure_fighter_cache,
    is_cache_valid,
    labeled_fights,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_fights.csv"


def _cache_paths():
    return (
        config.CACHE_DIR / "fighter_history_long.parquet",
        config.CACHE_DIR / "fighter_elo.parquet",
        config.CACHE_DIR / "fighter_cache_meta.json",
    )


@pytest.fixture
def sample_fights() -> pd.DataFrame:
    df = pd.read_csv(FIXTURE, parse_dates=["date"])
    return _add_pipeline_aliases(df)


@pytest.fixture(autouse=True)
def _isolate_fighter_cache(tmp_path, monkeypatch):
    """Point fighter cache files at a temp directory."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(config, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(
        "src.fighter_cache.FIGHTER_HISTORY_PATH",
        cache_dir / "fighter_history_long.parquet",
    )
    monkeypatch.setattr(
        "src.fighter_cache.FIGHTER_ELO_PATH",
        cache_dir / "fighter_elo.parquet",
    )
    monkeypatch.setattr(
        "src.fighter_cache.FIGHTER_CACHE_META_PATH",
        cache_dir / "fighter_cache_meta.json",
    )
    monkeypatch.setattr(
        "src.fighter_cache.fights_source_fingerprint",
        lambda: "test_fp",
    )


def test_labeled_fights_excludes_upcoming(sample_fights: pd.DataFrame):
    upcoming = pd.DataFrame(
        {
            config.FIGHT_ID_COLUMN: ["upcoming_1"],
            config.DATE_COLUMN: ["2099-01-01"],
            "fighter_1": ["A"],
            "fighter_2": ["B"],
            "winner": [""],
            "weight_class": ["Lightweight"],
        }
    )
    combined = pd.concat([sample_fights, upcoming], ignore_index=True)
    labeled = labeled_fights(combined)
    assert "upcoming_1" not in labeled[config.FIGHT_ID_COLUMN].astype(str).tolist()


def test_build_and_use_fighter_cache(sample_fights: pd.DataFrame):
    build_full_cache(sample_fights)
    hist_path, elo_path, meta_path = _cache_paths()
    assert is_cache_valid()
    assert hist_path.is_file()
    assert elo_path.is_file()
    assert meta_path.is_file()

    last = sample_fights.sort_values(config.DATE_COLUMN).iloc[-1]
    upcoming = pd.DataFrame(
        {
            config.FIGHT_ID_COLUMN: ["card_test_1"],
            config.DATE_COLUMN: [pd.Timestamp("2099-06-01")],
            "fighter_1": [last["fighter_1"]],
            "fighter_2": [last["fighter_2"]],
            "winner": [""],
            "weight_class": [last.get("weight_class", "Lightweight")],
            "event_name": ["UFC Test Card"],
        }
    )
    combined = pd.concat([sample_fights, upcoming], ignore_index=True)
    target_ids = {"card_test_1"}

    cached = build_features_with_cache(
        combined,
        target_fight_ids=target_ids,
        keep_unlabeled=True,
    )
    assert not cached.empty
    assert cached[config.FIGHT_ID_COLUMN].astype(str).iloc[0] == "card_test_1"
    for col in ("f1_win_rate", "f2_win_rate", "elo_diff", "win_rate_diff"):
        assert col in cached.columns
        assert pd.to_numeric(cached[col], errors="coerce").notna().any()

    # Historical rows should match between cache and full rebuild.
    hist_id = str(sample_fights.iloc[5][config.FIGHT_ID_COLUMN])
    cached_hist = build_features_with_cache(
        sample_fights,
        target_fight_ids={hist_id},
        keep_unlabeled=True,
    )
    full_hist = build_feature_matrix(
        sample_fights,
        keep_unlabeled=True,
        target_fight_ids={hist_id},
    )
    assert len(cached_hist) == len(full_hist)
    for col in ("f1_win_rate", "f2_win_rate", "win_rate_diff", "momentum_diff"):
        pd.testing.assert_series_equal(
            pd.to_numeric(cached_hist[col], errors="coerce").reset_index(drop=True),
            pd.to_numeric(full_hist[col], errors="coerce").reset_index(drop=True),
            check_names=False,
            rtol=1e-4,
            atol=1e-4,
        )


def test_ensure_fighter_cache_idempotent(sample_fights: pd.DataFrame):
    ensure_fighter_cache(sample_fights)
    hist_path, _, _ = _cache_paths()
    mtime = hist_path.stat().st_mtime
    ensure_fighter_cache(sample_fights)
    assert hist_path.stat().st_mtime == mtime
