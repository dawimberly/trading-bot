import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sqlite3
import config
from modules.data_loader import load_close_matrix

p = config.resolve_db_path()
print("db:", p)
c = sqlite3.connect(p)
tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print("tables:", len(tables), "daily:", sum(1 for x in tables if x.endswith("_daily")))
d5 = load_close_matrix(interval="5m")
d1 = load_close_matrix(interval="1d")
print("5m rows:", 0 if d5 is None else len(d5))
print("1d rows:", 0 if d1 is None else len(d1))
if d5 is not None and not d5.empty:
    print("5m cols:", len(d5.columns), "last:", d5.index[-1])
if d1 is not None and not d1.empty:
    print("1d cols:", len(d1.columns), "last:", d1.index[-1])
