#!/usr/bin/env python3
"""Delegate to ufc_betting_bot debug diagnostic."""

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.append(str(_root))

from ufc_betting_bot.debug_2025 import diagnose_2025_predictions

if __name__ == "__main__":
    diagnose_2025_predictions()
