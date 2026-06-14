"""Load ticker universe from all_tickers.csv or fall back to config.UNIVERSE.

Run: python scripts/maintenance/update_universe.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

import config


def get_full_market_universe():
    try:
        df = pd.read_csv("all_tickers.csv")
        return df["Symbol"].tolist()
    except FileNotFoundError:
        return list(config.UNIVERSE)


if __name__ == "__main__":
    tickers = get_full_market_universe()
    print(f"Universe size: {len(tickers)}")
    print(tickers[:20], "..." if len(tickers) > 20 else "")
