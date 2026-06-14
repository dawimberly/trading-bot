"""Drop legacy *_5m and *_daily tables from market_data.db.

Run: python scripts/db/cleanup_db.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import sqlite3

import config

conn = sqlite3.connect(config.DB_PATH)
cursor = conn.cursor()
cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
all_tables = [t[0] for t in cursor.fetchall()]
junk = [t for t in all_tables if "_5m" in t or "_daily" in t]
for t in junk:
    cursor.execute(f"DROP TABLE IF EXISTS '{t}'")
    print("Dropped: " + t)
conn.commit()
conn.close()
print("Database cleaned.")
