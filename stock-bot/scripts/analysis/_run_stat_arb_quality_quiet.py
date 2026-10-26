"""Quiet --compare-stat-arb-quality wrapper."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("PAPER_DEPLOY_DEBUG", "false")
os.environ.setdefault("MARKOV_HMM_ENABLED", "false")
os.environ.setdefault("PYTHONUNBUFFERED", "1")

logging.disable(logging.INFO)
_orig = logging.Logger.callHandlers


def _quiet_handlers(self, record):
    if record.levelno < logging.WARNING:
        return
    return _orig(self, record)


logging.Logger.callHandlers = _quiet_handlers

days = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("V154_TUNE_DAYS", "365")
sys.argv = [
    "backtester.py",
    "--days",
    str(days),
    "--paper-aggressive",
    "--compare-stat-arb-quality",
    "--no-thinking",
]

import runpy

runpy.run_path(str(ROOT / "backtester.py"), run_name="__main__")
