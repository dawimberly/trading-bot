"""NerdMiner monitor settings and data paths."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

SERIAL_PORT = os.getenv("NERDMINER_SERIAL_PORT", "COM4")
BAUD = int(os.getenv("NERDMINER_BAUD", "115200"))
STATE_FILE = ROOT / "state.json"
HISTORY_FILE = ROOT / "history.jsonl"
POLL_SECONDS = float(os.getenv("NERDMINER_POLL_SECONDS", "30"))
STALE_SECONDS = float(os.getenv("NERDMINER_STALE_SECONDS", "45"))
ALERTS_ENABLED = os.getenv("NERDMINER_ALERTS_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
