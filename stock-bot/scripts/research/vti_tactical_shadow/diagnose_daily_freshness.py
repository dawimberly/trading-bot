"""Diagnose daily-bar freshness vs live 5m path (ops check)."""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402


def _last_rows(conn: sqlite3.Connection, table: str, n: int = 8) -> list:
    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
    if not cols:
        return []
    date_col = next(
        (c for c in cols if "date" in c.lower() or "time" in c.lower()), cols[0]
    )
    close_col = next((c for c in cols if "close" in c.lower()), None)
    if close_col is None:
        return []
    q = (
        f'SELECT "{date_col}", "{close_col}" FROM "{table}" '
        f'ORDER BY "{date_col}" DESC LIMIT {n}'
    )
    return conn.execute(q).fetchall()


def main() -> int:
    path = config.resolve_db_path()
    print(f"db={path} mtime={datetime.fromtimestamp(Path(path).stat().st_mtime)}")
    conn = sqlite3.connect(str(path), timeout=15)
    for table in ("VTI_daily", "VTI", "SPY_daily", "SPY"):
        try:
            rows = _last_rows(conn, table)
        except Exception as exc:
            print(f"{table}: ERR {exc}")
            continue
        print(f"\n{table}:")
        for r in rows:
            print(f"  {r}")
    for col in ("Close", "close"):
        try:
            n = conn.execute(
                f"SELECT COUNT(*) FROM VTI_daily WHERE date >= '2026-07-01' "
                f"AND ({col} IS NULL OR {col} = 0)"
            ).fetchone()[0]
            print(f"\nVTI_daily null/zero since 2026-07-01 ({col}): {n}")
            break
        except Exception:
            continue
    print(f"\nnow_utc={datetime.now(timezone.utc).isoformat()}")
    print(
        "NOTE: live RefreshScheduler updates 5m tables only (fetch_and_store); "
        "*_daily needs fetch_data.py --daily or backtest refresh."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
