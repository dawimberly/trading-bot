"""Edge and implied probability from closing odds."""

from __future__ import annotations

import numpy as np
import pandas as pd


def odds_to_decimal(price: float) -> float:
    if not np.isfinite(price):
        return np.nan
    if abs(price) > 100:
        if price >= 100:
            return 1.0 + price / 100.0
        return 1.0 + 100.0 / abs(price)
    return float(price)


def has_valid_odds(row: pd.Series) -> bool:
    if "f1_odds" not in row.index or "f2_odds" not in row.index:
        return False
    o1, o2 = row.get("f1_odds"), row.get("f2_odds")
    if pd.isna(o1) or pd.isna(o2):
        return False
    try:
        o1_f, o2_f = float(o1), float(o2)
    except (TypeError, ValueError):
        return False
    if abs(o1_f) > 100 or abs(o2_f) > 100:
        return abs(o1_f) >= 100 and abs(o2_f) >= 100
    return o1_f > 1 and o2_f > 1


def fight_decimal_odds(row: pd.Series) -> tuple[float, float] | None:
    if not has_valid_odds(row):
        return None
    f1 = odds_to_decimal(float(row["f1_odds"]))
    f2 = odds_to_decimal(float(row["f2_odds"]))
    if not np.isfinite(f1) or not np.isfinite(f2) or f1 <= 1 or f2 <= 1:
        return None
    return f1, f2


def decimal_implied_prob(f1_odds: float, f2_odds: float) -> tuple[float, float]:
    p1 = 1 / f1_odds if f1_odds > 0 else np.nan
    p2 = 1 / f2_odds if f2_odds > 0 else np.nan
    total = p1 + p2
    if not np.isfinite(total) or total <= 0:
        return np.nan, np.nan
    return p1 / total, p2 / total


def market_probs(row: pd.Series) -> tuple[float, float] | None:
    """De-vigged implied probabilities only when both closing odds exist."""
    decimal = fight_decimal_odds(row)
    if decimal is None:
        return None
    p1, p2 = decimal_implied_prob(*decimal)
    if np.isfinite(p1) and np.isfinite(p2):
        return p1, p2
    return None


def compute_edge(
    model_p1: float,
    model_p2: float,
    market_p1: float,
    market_p2: float,
) -> dict[str, float]:
    edge_f1 = model_p1 - market_p1
    edge_f2 = model_p2 - market_p2
    if edge_f1 >= edge_f2:
        return {
            "bet_side": "f1",
            "edge": edge_f1,
            "best_edge": max(edge_f1, edge_f2),
            "edge_f1": edge_f1,
            "edge_f2": edge_f2,
        }
    return {
        "bet_side": "f2",
        "edge": edge_f2,
        "best_edge": max(edge_f1, edge_f2),
        "edge_f1": edge_f1,
        "edge_f2": edge_f2,
    }


def raw_kelly_fraction(prob: float, decimal_odds: float) -> float:
    """Full Kelly fraction before scaling and caps."""
    if decimal_odds <= 1 or not np.isfinite(prob) or prob <= 0 or prob >= 1:
        return 0.0
    b = decimal_odds - 1.0
    q = 1.0 - prob
    return max(0.0, (b * prob - q) / b)
