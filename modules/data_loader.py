"""Load close-price matrices from the SQLite market database."""

import sqlite3

import pandas as pd

import config


def load_close_matrix(db_path=None):
    """
    Read all ticker tables into a wide DataFrame of close prices.
    Skips legacy *_5m and *_daily table suffixes.
    """
    path = db_path or config.DB_PATH
    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [t[0] for t in cursor.fetchall()]
    clean_tables = [t for t in tables if "_5m" not in t and "_daily" not in t]
    data = pd.DataFrame()
    for table in clean_tables:
        df = pd.read_sql(f"SELECT * FROM '{table}'", conn)
        target_col = next((c for c in df.columns if "close" in c.lower()), None)
        if not target_col:
            continue
        data[table] = df.set_index("Date")[target_col]
    conn.close()
    return data.ffill().dropna(how="all")
