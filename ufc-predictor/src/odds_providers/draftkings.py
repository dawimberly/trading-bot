"""DraftKings UFC odds via The Odds API (bookmakers=draftkings)."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

import config
from src.data_loader import ensure_data_dirs
from src.predictor import OddsAPIError, _implied_probs, _names_match, _to_decimal_odds

logger = logging.getLogger(__name__)

DK_CACHE_PATH = config.CACHE_DIR / "draftkings_odds.csv"
BOOKMAKER_KEY = "draftkings"


def _cache_fresh(path: Path) -> bool:
    if not path.is_file() or config.ODDS_CACHE_TTL_HOURS <= 0:
        return False
    age_h = (time.time() - path.stat().st_mtime) / 3600
    return age_h < config.ODDS_CACHE_TTL_HOURS


def fetch_draftkings_odds(*, force_refresh: bool = False) -> pd.DataFrame:
    """
    Fetch DraftKings h2h lines for UFC/MMA.

    Returns columns aligned with merge_predictions_with_odds:
    fighter_1, fighter_2, f1_odds, f2_odds, implied_prob_f1, implied_prob_f2, bookmaker.
    """
    ensure_data_dirs()
    if not config.ODDS_API_KEY:
        raise OddsAPIError("Missing THE_ODDS_API_KEY for DraftKings odds.")

    if not force_refresh and _cache_fresh(DK_CACHE_PATH):
        cached = pd.read_csv(DK_CACHE_PATH, parse_dates=["commence_time"])
        if not cached.empty:
            logger.info("Using cached DraftKings odds (%s rows)", len(cached))
            return cached

    url = f"{config.ODDS_API_BASE_URL}/sports/{config.ODDS_API_SPORT}/odds"
    params = {
        "apiKey": config.ODDS_API_KEY,
        "regions": config.ODDS_API_REGIONS,
        "markets": config.ODDS_API_MARKETS,
        "oddsFormat": config.ODDS_API_ODDS_FORMAT,
        "bookmakers": BOOKMAKER_KEY,
    }

    try:
        response = requests.get(url, params=params, timeout=config.REQUEST_TIMEOUT_SEC)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise OddsAPIError(f"DraftKings odds request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise OddsAPIError("DraftKings odds returned invalid JSON") from exc

    if not isinstance(payload, list):
        raise OddsAPIError(f"Unexpected DraftKings response: {type(payload)}")

    rows: list[dict[str, Any]] = []
    for event in payload:
        home = str(event.get("home_team", "")).strip()
        away = str(event.get("away_team", "")).strip()
        if not home or not away:
            continue

        f1_px: float | None = None
        f2_px: float | None = None

        for book in event.get("bookmakers", []):
            if str(book.get("key", "")).lower() != BOOKMAKER_KEY:
                continue
            for market in book.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                prices: dict[str, float] = {}
                for outcome in market.get("outcomes", []):
                    name = str(outcome.get("name", "")).strip()
                    price = outcome.get("price")
                    if name and price is not None:
                        prices[name] = _to_decimal_odds(
                            float(price), config.ODDS_API_ODDS_FORMAT
                        )
                f1_px = next((px for nm, px in prices.items() if _names_match(nm, home)), None)
                f2_px = next((px for nm, px in prices.items() if _names_match(nm, away)), None)
                break

        if not f1_px or not f2_px or f1_px <= 1 or f2_px <= 1:
            continue

        imp1, imp2 = _implied_probs(f1_px, f2_px)
        rows.append(
            {
                "event_id": event.get("id", ""),
                "commence_time": event.get("commence_time"),
                "fighter_1": home,
                "fighter_2": away,
                "f1_odds": round(f1_px, 3),
                "f2_odds": round(f2_px, 3),
                "implied_prob_f1": imp1,
                "implied_prob_f2": imp2,
                "bookmaker": "DraftKings",
                "bookmaker_count": 1,
            }
        )

    odds_df = pd.DataFrame(rows)
    if odds_df.empty:
        raise OddsAPIError("No DraftKings UFC h2h odds returned.")

    if "commence_time" in odds_df.columns:
        odds_df["commence_time"] = pd.to_datetime(odds_df["commence_time"], errors="coerce")

    odds_df.to_csv(DK_CACHE_PATH, index=False)
    logger.info("Fetched %s DraftKings odds lines", len(odds_df))
    return odds_df
