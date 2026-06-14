"""Unit-style test for strategy_module MA signal logic.

Run: python scripts/dev/test_strategy.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from strategy_module.logic import check_signal

data = pd.Series([100] * 45 + [105, 110, 115, 120, 125])
signal, ma = check_signal(data, "stock", 45)
print(f"Signal: {signal}, MA: {ma}")
