"""Regenerate fetch_data and DB utility scripts from templates.

WARNING: Does NOT overwrite run_all.py — that file is maintained manually with consolidated modules.
Run: python write_bot.py
"""

import config

FETCH_DATA_TEMPLATE = '''"""Download 5-minute OHLCV from yfinance and store in SQLite.

Run: python fetch_data.py
"""

import sqlite3

import pandas as pd
import yfinance as yf

import config


def fetch_and_store():
    conn = sqlite3.connect(config.DB_PATH)
    print("Fetching 5-minute data for " + str(len(config.UNIVERSE)) + " tickers...")
    for ticker in config.UNIVERSE:
        try:
            df = yf.download(ticker, period="5d", interval="5m", progress=False)
            if df.empty:
                print("No data for " + ticker)
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df[["Close"]].copy()
            df.index.name = "Date"
            df.reset_index(inplace=True)
            df.to_sql(ticker, conn, if_exists="replace", index=False)
            print("Stored: " + ticker)
        except Exception as e:
            print("Failed: " + ticker + " - " + str(e))
    conn.close()
    print("Done. Database updated.")


if __name__ == "__main__":
    fetch_and_store()
'''

CHECK_TABLES_TEMPLATE = '''"""List all tables in market_data.db.

Run: python scripts/db/check_tables.py
"""

import sqlite3

import config

conn = sqlite3.connect(config.DB_PATH)
cursor = conn.cursor()
cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
tables = [t[0] for t in cursor.fetchall()]
print(tables)
conn.close()
'''

CLEANUP_DB_TEMPLATE = '''"""Drop legacy *_5m and *_daily tables from market_data.db.

Run: python scripts/db/cleanup_db.py
"""

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
'''


def main():
    print("WARNING: run_all.py is NOT regenerated — edit it directly.")
    with open("fetch_data.py", "w", encoding="utf-8") as f:
        f.write(FETCH_DATA_TEMPLATE)
    print("fetch_data.py written successfully")
    # check_tables and cleanup_db written under scripts/ when folder exists
    print("Done. Use scripts/db/ utilities after scripts/ folder is set up.")


if __name__ == "__main__":
    main()
