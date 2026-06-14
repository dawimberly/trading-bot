"""Inspect VTI table schema sample rows.

Run: python scripts/db/check_db.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import sqlite3

import pandas as pd

import config

try:
    conn = sqlite3.connect(config.DB_PATH)
    df = pd.read_sql("SELECT * FROM 'VTI' LIMIT 5", conn)
    print("The columns are:")
    print(df.columns.tolist())
    conn.close()
except Exception as e:
    print(f"Error: {e}")
