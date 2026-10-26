"""Read-only coverage audit of local daily bars for the NYSE PIT replay.

Does not write to market_data.db. Prints a JSON summary on stdout.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.pit_universe import (  # noqa: E402
    load_sp500_membership,
    membership_matrix,
    resolve_column,
    sp500_tickers_in_window,
)
from modules.data_loader import daily_table_name, safe_sql_table  # noqa: E402

DB_PATH = ROOT / "market_data.db"
WINDOW_START = pd.Timestamp("2024-12-24")
WINDOW_END = pd.Timestamp("2026-10-14")
SIM_START = pd.Timestamp("2025-10-14")


def _load_daily(conn: sqlite3.Connection, table: str) -> pd.DataFrame:
    info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    cols = {str(r[1]).lower(): str(r[1]) for r in info}
    date_col = next(
        (
            cols[n]
            for n in ("date", "timestamp", "datetime", "time", "dt")
            if n in cols
        ),
        None,
    )
    if date_col is None:
        for key, original in cols.items():
            if "date" in key or "time" in key:
                date_col = original
                break
    close_col = next((original for key, original in cols.items() if "close" in key), None)
    if date_col is None or close_col is None:
        return pd.DataFrame(columns=["date", "close"])
    df = pd.read_sql(
        f'SELECT "{date_col}" AS date, "{close_col}" AS close FROM "{table}"',
        conn,
    )
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
    if getattr(df["date"].dt, "tz", None) is not None:
        df["date"] = df["date"].dt.tz_convert(None)
    df["date"] = df["date"].dt.normalize()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["date"])
    return df


def main() -> None:
    membership = load_sp500_membership()
    members = sp500_tickers_in_window(membership, WINDOW_START, WINDOW_END)
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    spy_table = "SPY_daily" if "SPY_daily" in tables else None
    if spy_table is None:
        raise SystemExit("SPY_daily is not in market_data.db")
    spy = _load_daily(conn, spy_table)
    sessions = spy.loc[
        (spy["date"] >= WINDOW_START) & (spy["date"] <= WINDOW_END), "date"
    ]
    session_index = pd.DatetimeIndex(sorted(sessions.dropna().unique()))
    matrix = membership_matrix(session_index, membership, tickers=members)

    rows = []
    for ticker in members:
        table = daily_table_name(ticker)
        safe = safe_sql_table(table)
        present = safe in tables
        expected = set(session_index[matrix[ticker].to_numpy()])
        if not present:
            rows.append(
                {
                    "ticker": ticker,
                    "table": safe,
                    "has_table": False,
                    "rows_in_window": 0,
                    "duplicate_dates": 0,
                    "expected_sessions": len(expected),
                    "missing_sessions": len(expected),
                    "extra_sessions": 0,
                    "first": None,
                    "last": None,
                }
            )
            continue
        frame = _load_daily(conn, safe)
        in_window = frame.loc[
            (frame["date"] >= WINDOW_START) & (frame["date"] <= WINDOW_END)
        ]
        counts = in_window["date"].value_counts()
        dupes = int((counts > 1).sum())
        got = set(in_window["date"])
        missing = expected - got
        extra = got - set(session_index)
        rows.append(
            {
                "ticker": ticker,
                "table": safe,
                "has_table": True,
                "rows_in_window": int(len(in_window)),
                "duplicate_dates": dupes,
                "expected_sessions": len(expected),
                "missing_sessions": len(missing),
                "extra_sessions": len(extra),
                "first": None if in_window.empty else str(in_window["date"].min().date()),
                "last": None if in_window.empty else str(in_window["date"].max().date()),
            }
        )
    conn.close()
    detail = pd.DataFrame(rows)
    have = detail[detail["has_table"]]
    summary = {
        "db": str(DB_PATH),
        "window_start": str(WINDOW_START.date()),
        "window_end": str(WINDOW_END.date()),
        "sim_start": str(SIM_START.date()),
        "spy_sessions": int(len(session_index)),
        "members": int(len(members)),
        "members_with_table": int(have.shape[0]),
        "members_missing_table": int((~detail["has_table"]).sum()),
        "members_with_any_gap": int((have["missing_sessions"] > 0).sum()) if len(have) else 0,
        "members_complete": int((have["missing_sessions"] == 0).sum()) if len(have) else 0,
        "duplicate_date_symbols": int((detail["duplicate_dates"] > 0).sum()),
        "duplicate_date_count": int(detail["duplicate_dates"].sum()),
        "missing_tickers": detail.loc[~detail["has_table"], "ticker"].tolist(),
        "gapped_sample": have.loc[have["missing_sessions"] > 0]
        .sort_values("missing_sessions", ascending=False)
        .head(25)[["ticker", "missing_sessions", "expected_sessions", "first", "last"]]
        .to_dict(orient="records"),
    }
    out = ROOT / "data" / "massive_coverage_audit"
    out.mkdir(parents=True, exist_ok=True)
    detail.to_csv(out / "local_member_coverage.csv", index=False)
    (out / "local_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    printable = dict(summary)
    printable["missing_tickers"] = summary["missing_tickers"][:30]
    printable["missing_ticker_count"] = len(summary["missing_tickers"])
    print(json.dumps(printable, indent=2))


if __name__ == "__main__":
    main()
