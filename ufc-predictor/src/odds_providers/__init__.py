"""Book-specific odds providers for the UFC dashboard."""

from src.odds_providers.betnow_scraper import fetch_betnow_odds, fetch_betnow_prop_odds
from src.odds_providers.draftkings import fetch_draftkings_odds
from src.odds_providers.draftkings_props import fetch_draftkings_prop_odds
from src.odds_providers.mybookie_scraper import fetch_mybookie_odds, fetch_mybookie_prop_odds

__all__ = [
    "fetch_betnow_odds",
    "fetch_betnow_prop_odds",
    "fetch_draftkings_odds",
    "fetch_draftkings_prop_odds",
    "fetch_mybookie_odds",
    "fetch_mybookie_prop_odds",
]
