"""Monorepo launcher — runs stock-bot/run_all.py with correct cwd and PYTHONTRADING_ROOT."""

from __future__ import annotations

import os
import sys
from pathlib import Path

STOCK_BOT = Path(__file__).resolve().parent / "stock-bot"
if not (STOCK_BOT / "run_all.py").is_file():
    raise SystemExit(f"stock-bot/run_all.py not found under {STOCK_BOT.parent}")

os.chdir(STOCK_BOT)
sys.path.insert(0, str(STOCK_BOT))
os.environ["PYTHONTRADING_ROOT"] = str(STOCK_BOT)

import runpy

runpy.run_path(str(STOCK_BOT / "run_all.py"), run_name="__main__")
