"""Historical UFC odds loading — delegates to ufc-predictor ``src.data_loader``."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_PREDICTOR_DIR = Path(__file__).resolve().parents[2] / "ufc-predictor"
if str(_PREDICTOR_DIR) not in sys.path:
    sys.path.insert(0, str(_PREDICTOR_DIR))

from src.data_loader import (  # noqa: E402
    OddsLoadError,
    _lookup_odds_for_fight as lookup_odds_for_fight,
    _normalize_odds_frame as normalize_odds_frame,
    build_unified_odds_table,
    merge_historical_odds,
)

__all__ = [
    "OddsLoadError",
    "build_unified_odds_table",
    "lookup_odds_for_fight",
    "merge_historical_odds",
    "normalize_odds_frame",
]
