"""DraftKings UFC prop odds via The Odds API event markets."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests

import config
from src.data_loader import ensure_data_dirs
from src.odds_providers.prop_odds_common import empty_prop_odds_df, prop_row
from src.predictor import OddsAPIError, _names_match, _to_decimal_odds

logger = logging.getLogger(__name__)

DK_PROP_CACHE_PATH = config.CACHE_DIR / "draftkings_prop_odds.csv"
BOOKMAKER_KEY = "draftkings"


def _cache_fresh(path: Path) -> bool:
    if not path.is_file() or config.ODDS_CACHE_TTL_HOURS <= 0:
        return False
    age_h = (time.time() - path.stat().st_mtime) / 3600
    return age_h < config.ODDS_CACHE_TTL_HOURS


def _totals_to_prop_rows(
    *,
    fighter_1: str,
    fighter_2: str,
    outcomes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Map DK totals (round O/U 1.5) to internal prop keys."""
    rows: list[dict[str, Any]] = []
    for outcome in outcomes:
        name = str(outcome.get("name", "")).strip()
        price = outcome.get("price")
        point = outcome.get("point")
        if price is None or point is None:
            continue
        decimal = _to_decimal_odds(float(price), config.ODDS_API_ODDS_FORMAT)
        if decimal <= 1:
            continue
        point_f = float(point)
        selection = name
        if name.lower() == "over" and abs(point_f - 1.5) < 0.01:
            prop_key = "over_1_5_rounds"
        elif name.lower() == "under" and abs(point_f - 1.5) < 0.01:
            prop_key = "round_1_finish"
            selection = "Under 1.5"
        else:
            prop_key = f"totals_{name.lower()}_{point_f:g}"
        rows.append(
            prop_row(
                fighter_1=fighter_1,
                fighter_2=fighter_2,
                prop_key=prop_key,
                selection=selection,
                decimal_odds=decimal,
                bookmaker="DraftKings",
                odds_source="live",
                market_key="totals",
                point=point_f,
            )
        )
    return rows


def fetch_draftkings_prop_odds(*, force_refresh: bool = False) -> pd.DataFrame:
    """
    Fetch DraftKings UFC prop markets from The Odds API.

    Currently supported live markets:
    - totals (Over/Under 1.5 rounds) -> over_1_5_rounds, round_1_finish
    """
    ensure_data_dirs()
    if not config.ENABLE_PROPS:
        return empty_prop_odds_df()
    if not config.ODDS_API_KEY:
        raise OddsAPIError("Missing THE_ODDS_API_KEY for DraftKings prop odds.")

    if not force_refresh and _cache_fresh(DK_PROP_CACHE_PATH):
        cached = pd.read_csv(DK_PROP_CACHE_PATH)
        if not cached.empty:
            logger.info("Using cached DraftKings prop odds (%s rows)", len(cached))
            return cached

    events_url = f"{config.ODDS_API_BASE_URL}/sports/{config.ODDS_API_SPORT}/events"
    try:
        events_resp = requests.get(
            events_url,
            params={"apiKey": config.ODDS_API_KEY},
            timeout=config.REQUEST_TIMEOUT_SEC,
        )
        events_resp.raise_for_status()
        events = events_resp.json()
    except requests.RequestException as exc:
        raise OddsAPIError(f"DraftKings prop events request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise OddsAPIError("DraftKings prop events returned invalid JSON") from exc

    if not isinstance(events, list):
        raise OddsAPIError(f"Unexpected DraftKings events response: {type(events)}")

    markets = config.ODDS_API_PROP_MARKETS or "totals"
    rows: list[dict[str, Any]] = []

    for event in events:
        event_id = event.get("id")
        home = str(event.get("home_team", "")).strip()
        away = str(event.get("away_team", "")).strip()
        if not event_id or not home or not away:
            continue

        odds_url = f"{config.ODDS_API_BASE_URL}/sports/{config.ODDS_API_SPORT}/events/{event_id}/odds"
        params = {
            "apiKey": config.ODDS_API_KEY,
            "regions": config.ODDS_API_REGIONS,
            "markets": markets,
            "oddsFormat": config.ODDS_API_ODDS_FORMAT,
            "bookmakers": BOOKMAKER_KEY,
        }
        try:
            resp = requests.get(odds_url, params=params, timeout=config.REQUEST_TIMEOUT_SEC)
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as exc:
            logger.debug("DK prop odds skipped for %s vs %s: %s", home, away, exc)
            continue

        for book in payload.get("bookmakers", []):
            if str(book.get("key", "")).lower() != BOOKMAKER_KEY:
                continue
            for market in book.get("markets", []):
                key = str(market.get("key", ""))
                if key == "totals":
                    rows.extend(
                        _totals_to_prop_rows(
                            fighter_1=home,
                            fighter_2=away,
                            outcomes=market.get("outcomes", []),
                        )
                    )

    df = pd.DataFrame(rows)
    if df.empty:
        logger.warning("No DraftKings prop odds returned from The Odds API.")
        return empty_prop_odds_df()

    df.to_csv(DK_PROP_CACHE_PATH, index=False)
    logger.info("Fetched %s DraftKings prop odds lines", len(df))
    return df
