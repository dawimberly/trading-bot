"""Tests for live prop odds providers."""

from __future__ import annotations

import pandas as pd
import pytest

import config
from src.odds_providers.draftkings_props import _totals_to_prop_rows
from src.odds_providers.prop_odds_common import lookup_prop_odds_row, parse_american_odds
from src.props import resolve_prop_quote


@pytest.fixture(autouse=True)
def enable_props(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_PROPS", True)


def test_parse_american_odds():
    assert parse_american_odds("+450") == 450.0
    assert parse_american_odds("-110") == -110.0


def test_totals_to_prop_rows_maps_round_markets():
    rows = _totals_to_prop_rows(
        fighter_1="Diego Lopes",
        fighter_2="Steve Garcia Jr.",
        outcomes=[
            {"name": "Over", "price": 1.65, "point": 1.5},
            {"name": "Under", "price": 2.2, "point": 1.5},
        ],
    )
    keys = {r["prop_key"] for r in rows}
    assert "over_1_5_rounds" in keys
    assert "round_1_finish" in keys


def test_resolve_prop_quote_prefers_live():
    prop_odds = pd.DataFrame(
        [
            {
                "fighter_1": "Diego Lopes",
                "fighter_2": "Steve Garcia Jr.",
                "prop_key": "over_1_5_rounds",
                "selection": "Over",
                "decimal_odds": 1.65,
                "implied_prob": 1 / 1.65,
                "american_odds": -154,
                "market_key": "totals",
                "point": 1.5,
                "rotation": "",
                "bookmaker": "DraftKings",
                "odds_source": "live",
            }
        ]
    )
    row = pd.Series(
        {
            "fighter_1": "Diego Lopes",
            "fighter_2": "Steve Garcia",
            "prob_f1_win": 0.58,
            "f1_ko_rate": 0.2,
            "f2_ko_rate": 0.15,
            "f1_sub_avg": 0.3,
            "f2_sub_avg": 0.2,
            "f1_finish_rate": 0.4,
            "f2_finish_rate": 0.35,
        }
    )
    quote = resolve_prop_quote(row, "over_1_5_rounds", book="DraftKings", prop_odds=prop_odds)
    assert quote["odds_source"] == "live"
    assert quote["decimal_odds"] == 1.65


def test_lookup_prop_odds_row_fuzzy_names():
    prop_odds = pd.DataFrame(
        [
            {
                "fighter_1": "Michael Chandler",
                "fighter_2": "Mauricio Ruffy",
                "prop_key": "goes_to_decision",
                "selection": "Yes",
                "decimal_odds": 2.5,
                "implied_prob": 0.4,
                "american_odds": 150,
                "market_key": "prop",
                "point": None,
                "rotation": "24013",
                "bookmaker": "BetNow.eu",
                "odds_source": "live",
            }
        ]
    )
    hit = lookup_prop_odds_row("Michael Chandler", "Mauricio Ruffy", "goes_to_decision", prop_odds)
    assert hit is not None
    assert float(hit["decimal_odds"]) == 2.5
