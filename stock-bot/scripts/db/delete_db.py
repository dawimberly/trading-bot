"""Delete market_data.db (repopulate with fetch_data.py after).

Run: python scripts/db/delete_db.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os

import config

if os.path.exists(config.DB_PATH):
    os.remove(config.DB_PATH)
    print("Database deleted. Run: python fetch_data.py")
else:
    print("Database not found.")
