"""Quiet 3-way Markov compare: RHYME | HMM soft | HMM primary.

Default window 100d; set HMM_COMPARE_DAYS=365 for the decision-grade run.
"""
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
os.environ.setdefault("PYTHONUNBUFFERED", "1")
os.environ.setdefault("HMM_RETRAIN_EVERY_BARS", "21")

logging.disable(logging.INFO)
_orig = logging.Logger.callHandlers


def _quiet_handlers(self, record):
    if record.levelno < logging.WARNING:
        return
    return _orig(self, record)


logging.Logger.callHandlers = _quiet_handlers

days = os.environ.get("HMM_COMPARE_DAYS", "100")
sys.argv = [
    "backtester.py",
    "--days",
    days,
    "--paper-aggressive",
    "--compare-markov-hmm",
    "--no-thinking",
]

import runpy

runpy.run_path(str(ROOT / "backtester.py"), run_name="__main__")
