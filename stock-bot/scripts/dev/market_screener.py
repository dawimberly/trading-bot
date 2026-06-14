"""Generate a static buy plan JSON for target allocations.

Run: python scripts/dev/market_screener.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modules.market_screener import run_screener

if __name__ == "__main__":
    run_screener()
