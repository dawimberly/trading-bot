"""Mock ROI grouping demo for trade post-analysis.

Run: python scripts/dev/test_logic.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

data = {
    "Signal": ["TEST-BOT-1", "TEST-BOT-1", "LOSING-BOT"],
    "Ticker": ["VTI", "VTI", "BTC-USD"],
    "Timestamp": ["2026-04-01", "2026-04-15", "2026-04-01"],
    "Entry_Price": [100.0, 105.0, 50000.0],
    "Exit_Price": [110.0, 102.0, 45000.0],
}
df = pd.DataFrame(data)
df["ROI"] = ((df["Exit_Price"] - df["Entry_Price"]) / df["Entry_Price"]) * 100
print("--- MOCK PERFORMANCE REPORT ---")
print(df.groupby("Signal")["ROI"].mean())
