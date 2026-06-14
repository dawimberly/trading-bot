"""Unit tests for data loading and cleaning helpers."""

from __future__ import annotations

import pandas as pd

from src.data_loader import (
    _fighters_same_person,
    _is_cleaned_fights_df,
    _lookup_odds_for_fight,
    _normalize_odds_frame,
    _read_fights_csv,
    _resolve_winner,
    clean_fighter_name,
    merge_historical_odds,
)


def test_fighters_same_person_last_name_match():
    assert _fighters_same_person("Jon Jones", "Jonathan Jones")
    assert not _fighters_same_person("Jon Jones", "Tom Silva")


def test_fighters_same_person_no_short_substring_false_positive():
    """Short tokens must not match via bare substring."""
    assert not _fighters_same_person("Al", "Alice Fighter")
    assert not _fighters_same_person("Bo", "Bob Fighter")


def test_resolve_winner_canonical_names():
    row = pd.Series(
        {"winner": "Alice Fighter", "fighter1": "Alice Fighter", "fighter2": "Bob Fighter"}
    )
    assert _resolve_winner(row) == "Alice Fighter"


def test_resolve_winner_f1_alias():
    row = pd.Series({"winner": "red", "fighter1": "Alice Fighter", "fighter2": "Bob Fighter"})
    assert _resolve_winner(row) == "Alice Fighter"


def test_is_cleaned_fights_df():
    raw = pd.DataFrame({"fighter_1": ["A"], "fighter_2": ["B"]})
    clean = pd.DataFrame(
        {
            "fight_id": ["x"],
            "fighter1": ["A"],
            "fighter2": ["B"],
            "date": ["2020-01-01"],
        }
    )
    assert not _is_cleaned_fights_df(raw)
    assert _is_cleaned_fights_df(clean)


def test_read_fights_csv_skips_reclean(tmp_path):
    path = tmp_path / "fights.csv"
    pd.DataFrame(
        {
            "fight_id": ["id1", "id2"],
            "fighter1": ["Alice Fighter", "Bob Fighter"],
            "fighter2": ["Bob Fighter", "Carol Fighter"],
            "date": ["2020-01-01", "2020-06-01"],
            "event": ["UFC 1", "UFC 2"],
            "winner": ["Alice Fighter", "Bob Fighter"],
            "weight_class": ["LW", "LW"],
        }
    ).to_csv(path, index=False)

    loaded = _read_fights_csv(path)
    assert len(loaded) == 2
    assert "fighter_1" in loaded.columns
    assert clean_fighter_name(loaded.loc[0, "fighter_1"]) == "Alice Fighter"


def test_normalize_odds_frame_kaggle_jerzyszocik_schema():
    """jerzyszocik daily dataset uses event_date + R_/B_ fighter columns."""
    raw = pd.DataFrame(
        {
            "event_date": ["2025-03-08"],
            "event_name": ["UFC 313"],
            "R_fighter": ["Alex Pereira"],
            "B_fighter": ["Jamahal Hill"],
            "R_odds": [-180.0],
            "B_odds": [150.0],
        }
    )
    odds = _normalize_odds_frame(raw, source="kaggle:jerzyszocik")
    assert len(odds) == 1
    assert odds.loc[0, "f1_odds"] == -180.0
    assert odds.loc[0, "f2_odds"] == 150.0
    assert odds.loc[0, "event"] == "UFC 313"


def test_lookup_odds_for_fight_fuzzy_name_on_date():
    odds = pd.DataFrame(
        {
            "event": [""],
            "date": pd.to_datetime(["2025-01-11"]),
            "fighter1": ["Brandon Royval"],
            "fighter2": ["Manel Kape"],
            "f1_odds": [200.0],
            "f2_odds": [-245.0],
            "source": ["test"],
        }
    )
    matched = _lookup_odds_for_fight(
        odds,
        event="",
        fight_date=pd.Timestamp("2025-01-11"),
        fighter1="B Royval",
        fighter2="Manel Kape",
    )
    assert matched == (200.0, -245.0)


def test_normalize_odds_frame_ultimate_schema():
    raw = pd.DataFrame(
        {
            "date": ["2025-01-11"],
            "R_fighter": ["Brandon Royval"],
            "B_fighter": ["Manel Kape"],
            "R_odds": [200.0],
            "B_odds": [-245.0],
        }
    )
    odds = _normalize_odds_frame(raw, source="test")
    assert len(odds) == 1
    assert odds.loc[0, "f1_odds"] == 200.0
    assert odds.loc[0, "f2_odds"] == -245.0


def test_lookup_odds_for_fight_by_date_and_fighters():
    odds = pd.DataFrame(
        {
            "event": [""],
            "date": pd.to_datetime(["2025-01-11"]),
            "fighter1": ["Brandon Royval"],
            "fighter2": ["Manel Kape"],
            "f1_odds": [200.0],
            "f2_odds": [-245.0],
            "source": ["test"],
        }
    )
    matched = _lookup_odds_for_fight(
        odds,
        event="UFC Fight Night: Royval vs. Kape",
        fight_date=pd.Timestamp("2025-01-11"),
        fighter1="Brandon Royval",
        fighter2="Manel Kape",
    )
    assert matched == (200.0, -245.0)


def test_merge_historical_odds_fills_matching_fight(monkeypatch):
    fights = pd.DataFrame(
        {
            "fight_id": ["x1", "x2"],
            "event": ["UFC Fight Night", "UFC 999"],
            "date": pd.to_datetime(["2025-01-11", "2025-01-11"]),
            "fighter1": ["Brandon Royval", "No Match A"],
            "fighter2": ["Manel Kape", "No Match B"],
            "winner": ["", ""],
            "weight_class": ["Flyweight", "LW"],
        }
    )
    odds = pd.DataFrame(
        {
            "event": [""],
            "date": pd.to_datetime(["2025-01-11"]),
            "fighter1": ["Brandon Royval"],
            "fighter2": ["Manel Kape"],
            "f1_odds": [200.0],
            "f2_odds": [-245.0],
            "source": ["test"],
        }
    )
    monkeypatch.setattr(
        "src.data_loader.build_unified_odds_table",
        lambda: odds,
    )
    merged = merge_historical_odds(fights)
    assert merged.loc[0, "f1_odds"] == 200.0
    assert merged.loc[0, "f2_odds"] == -245.0
    assert len(merged) == 2
    assert pd.isna(merged.loc[1, "f1_odds"])
