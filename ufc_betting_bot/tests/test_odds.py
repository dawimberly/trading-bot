from __future__ import annotations

import pandas as pd
import pytest

from ufc_betting_bot.modules.odds import (
    lookup_odds_for_fight,
    merge_historical_odds,
    normalize_odds_frame,
)


def test_normalize_ultimate_schema():
    raw = pd.DataFrame(
        {
            "date": ["2025-01-11"],
            "R_fighter": ["Brandon Royval"],
            "B_fighter": ["Manel Kape"],
            "R_odds": [200.0],
            "B_odds": [-245.0],
        }
    )
    odds = normalize_odds_frame(raw, source="test")
    assert len(odds) == 1
    assert odds.loc[0, "f1_odds"] == 200.0


def test_lookup_by_date_and_fighters():
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
    matched = lookup_odds_for_fight(
        odds,
        event="UFC FN",
        fight_date=pd.Timestamp("2025-01-11"),
        fighter1="Brandon Royval",
        fighter2="Manel Kape",
    )
    assert matched == (200.0, -245.0)


def test_merge_keeps_unmatched(monkeypatch):
    fights = pd.DataFrame(
        {
            "fight_id": ["a", "b"],
            "event": ["E1", "E2"],
            "date": pd.to_datetime(["2025-01-11", "2025-01-11"]),
            "fighter1": ["Brandon Royval", "Nobody"],
            "fighter2": ["Manel Kape", "Else"],
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
    assert pd.isna(merged.loc[1, "f1_odds"])
    assert len(merged) == 2
