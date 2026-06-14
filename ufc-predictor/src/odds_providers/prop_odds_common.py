"""Shared helpers for live UFC prop odds ingestion."""

from __future__ import annotations

import re
from typing import Any

import numpy as np
import pandas as pd

from src.predictor import _implied_probs, _names_match, _to_decimal_odds

PROP_ODDS_COLUMNS = [
    "fighter_1",
    "fighter_2",
    "prop_key",
    "selection",
    "decimal_odds",
    "implied_prob",
    "american_odds",
    "market_key",
    "point",
    "rotation",
    "bookmaker",
    "odds_source",
]


def parse_american_odds(text: str) -> float | None:
    """Parse +450 / -110 from text."""
    if not text:
        return None
    clean = str(text).strip().replace("\u2212", "-")
    m = re.search(r"(?<![0-9T])([+-]\d{2,4})(?![0-9])", clean)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def american_to_decimal(american: float) -> float:
    if american >= 0:
        return 1.0 + american / 100.0
    return 1.0 + 100.0 / abs(american)


def implied_from_decimal(decimal_odds: float) -> float:
    if decimal_odds <= 1:
        return np.nan
    return float(1.0 / decimal_odds)


def normalize_totals_point(point: float | None) -> float | None:
    if point is None or not np.isfinite(point):
        return None
    return float(point)


def prop_row(
    *,
    fighter_1: str,
    fighter_2: str,
    prop_key: str,
    selection: str,
    decimal_odds: float,
    bookmaker: str,
    odds_source: str = "live",
    market_key: str = "",
    point: float | None = None,
    rotation: str = "",
    american_odds: float | None = None,
) -> dict[str, Any]:
    am = american_odds
    if am is None and decimal_odds > 1:
        if decimal_odds >= 2.0:
            am = float(round((decimal_odds - 1.0) * 100))
        else:
            am = float(round(-100.0 / (decimal_odds - 1.0)))
    return {
        "fighter_1": fighter_1.strip(),
        "fighter_2": fighter_2.strip(),
        "prop_key": prop_key,
        "selection": selection,
        "decimal_odds": round(float(decimal_odds), 3),
        "implied_prob": implied_from_decimal(decimal_odds),
        "american_odds": am,
        "market_key": market_key,
        "point": point,
        "rotation": rotation,
        "bookmaker": bookmaker,
        "odds_source": odds_source,
    }


def empty_prop_odds_df() -> pd.DataFrame:
    return pd.DataFrame(columns=PROP_ODDS_COLUMNS)


def lookup_prop_odds_row(
    fighter_1: str,
    fighter_2: str,
    prop_key: str,
    odds: pd.DataFrame,
    *,
    selection: str | None = None,
) -> pd.Series | None:
    """Find a prop line for a fight, optionally matching selection (Over/Under/Yes)."""
    if odds is None or odds.empty:
        return None
    for _, row in odds.iterrows():
        f1 = str(row.get("fighter_1", ""))
        f2 = str(row.get("fighter_2", ""))
        aligned = _names_match(fighter_1, f1) and _names_match(fighter_2, f2)
        swapped = _names_match(fighter_1, f2) and _names_match(fighter_2, f1)
        if not aligned and not swapped:
            continue
        if str(row.get("prop_key", "")) != prop_key:
            continue
        if selection is not None and str(row.get("selection", "")).lower() != selection.lower():
            continue
        return row
    return None


def attach_prop_odds_to_predictions(
    predictions: pd.DataFrame,
    prop_odds: pd.DataFrame,
) -> pd.DataFrame:
    """Add per-prop live quote columns to prediction rows."""
    if predictions.empty:
        return predictions.copy()
    out = predictions.copy()
    for key in set(prop_odds.get("prop_key", pd.Series(dtype=str)).tolist()) if not prop_odds.empty else []:
        out[f"prop_live_odds_{key}"] = np.nan
        out[f"prop_live_implied_{key}"] = np.nan
        out[f"prop_odds_source_{key}"] = ""
    if prop_odds.empty:
        return out

    for idx, row in out.iterrows():
        f1 = str(row.get("fighter_1", row.get("fighter1", "")))
        f2 = str(row.get("fighter_2", row.get("fighter2", "")))
        for prop_key in prop_odds["prop_key"].unique():
            match = lookup_prop_odds_row(f1, f2, str(prop_key), prop_odds)
            if match is None:
                continue
            out.at[idx, f"prop_live_odds_{prop_key}"] = float(match["decimal_odds"])
            out.at[idx, f"prop_live_implied_{prop_key}"] = float(match["implied_prob"])
            out.at[idx, f"prop_odds_source_{prop_key}"] = str(match.get("odds_source", "live"))
    return out


def prop_odds_summary(prop_odds: pd.DataFrame) -> dict[str, int]:
    if prop_odds is None or prop_odds.empty:
        return {"live": 0, "synthetic": 0}
    live = int((prop_odds.get("odds_source", "") == "live").sum())
    return {"live": live, "synthetic": 0}
