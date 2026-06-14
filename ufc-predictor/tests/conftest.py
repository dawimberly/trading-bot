"""Pytest path setup for monorepo (ufc-predictor + ufc_betting_bot)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PREDICTOR = Path(__file__).resolve().parents[1]
if str(PREDICTOR) not in sys.path:
    sys.path.insert(0, str(PREDICTOR))
