"""Backfill macro series into a SEPARATE research DB (never touches prod).

Sources:
  - FRED CSV (no API key): VIXCLS, DCOILWTICO
  - yfinance period=max: SPY, VTI, XLE, GLD, ^VIX, CL=F, GC=F

Usage (from stock-bot/):
  python scripts/research/geopolitical_event_study/backfill_research_macro.py
  python scripts/research/geopolitical_event_study/backfill_research_macro.py --force

Writes under data/research/geopolitical/:
  research_macro.db
  series_manifest.json
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

RESEARCH_DIR = ROOT / "data" / "research" / "geopolitical"
DB_PATH = RESEARCH_DIR / "research_macro.db"
MANIFEST_PATH = RESEARCH_DIR / "series_manifest.json"

# Logical name -> fetch plan
SERIES_PLAN: list[dict[str, str]] = [
    {
        "name": "VIX",
        "source": "FRED",
        "fred_id": "VIXCLS",
        "notes": "CBOE VIX daily close via FRED",
    },
    {
        "name": "WTI",
        "source": "FRED",
        "fred_id": "DCOILWTICO",
        "notes": "WTI Cushing spot USD/bbl via FRED/EIA",
    },
    {"name": "SPY", "source": "yfinance", "yf_ticker": "SPY", "notes": "SPDR S&P 500 ETF"},
    {"name": "VTI", "source": "yfinance", "yf_ticker": "VTI", "notes": "Vanguard Total Stock Market ETF"},
    {"name": "XLE", "source": "yfinance", "yf_ticker": "XLE", "notes": "Energy Select Sector SPDR"},
    {"name": "GLD", "source": "yfinance", "yf_ticker": "GLD", "notes": "SPDR Gold Shares"},
    {"name": "VIX_YF", "source": "yfinance", "yf_ticker": "^VIX", "notes": "yfinance VIX cross-check"},
    {"name": "CL_F", "source": "yfinance", "yf_ticker": "CL=F", "notes": "WTI futures proxy"},
    {"name": "GC_F", "source": "yfinance", "yf_ticker": "GC=F", "notes": "Gold futures proxy"},
]


def _fetch_fred(series_id: str) -> pd.Series:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    df = pd.read_csv(io.StringIO(raw))
    if df.empty or len(df.columns) < 2:
        return pd.Series(dtype=float)
    date_col, val_col = df.columns[0], df.columns[-1]
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df[val_col] = pd.to_numeric(df[val_col].replace(".", None), errors="coerce")
    s = df.dropna(subset=[date_col, val_col]).set_index(date_col)[val_col].sort_index()
    s.index = pd.DatetimeIndex(s.index).tz_localize(None).normalize()
    return s.astype(float)


def _fetch_yf(ticker: str) -> pd.Series:
    import yfinance as yf

    df = yf.download(ticker, period="max", interval="1d", progress=False, auto_adjust=True)
    if df is None or df.empty:
        return pd.Series(dtype=float)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    close_col = next((c for c in df.columns if str(c).lower() == "close"), None)
    if close_col is None:
        return pd.Series(dtype=float)
    s = pd.to_numeric(df[close_col], errors="coerce").dropna()
    s.index = pd.DatetimeIndex(s.index).tz_localize(None).normalize()
    return s.sort_index().astype(float)


def _checksum(series: pd.Series) -> str:
    if series.empty:
        return ""
    blob = (",".join(f"{i.date()}:{float(v):.8g}" for i, v in series.items())).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS series_meta (
            name TEXT PRIMARY KEY,
            source TEXT,
            source_id TEXT,
            first_date TEXT,
            last_date TEXT,
            rows INTEGER,
            checksum TEXT,
            retrieved_at TEXT,
            notes TEXT
        )
        """
    )
    conn.commit()


def _store_series(
    conn: sqlite3.Connection,
    *,
    name: str,
    series: pd.Series,
    source: str,
    source_id: str,
    notes: str,
) -> dict[str, Any]:
    table = f"{name}_daily"
    conn.execute(f'DROP TABLE IF EXISTS "{table}"')
    if series.empty:
        meta = {
            "name": name,
            "source": source,
            "source_id": source_id,
            "first_date": None,
            "last_date": None,
            "rows": 0,
            "checksum": "",
            "retrieved_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "notes": notes + " | EMPTY",
            "ok": False,
        }
    else:
        df = series.rename("Close").reset_index()
        df.columns = ["Date", "Close"]
        df.to_sql(table, conn, if_exists="replace", index=False)
        meta = {
            "name": name,
            "source": source,
            "source_id": source_id,
            "first_date": str(series.index[0].date()),
            "last_date": str(series.index[-1].date()),
            "rows": int(len(series)),
            "checksum": _checksum(series),
            "retrieved_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "notes": notes,
            "ok": True,
        }
    conn.execute(
        """
        INSERT OR REPLACE INTO series_meta
        (name, source, source_id, first_date, last_date, rows, checksum, retrieved_at, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            meta["name"],
            meta["source"],
            meta["source_id"],
            meta["first_date"],
            meta["last_date"],
            meta["rows"],
            meta["checksum"],
            meta["retrieved_at"],
            meta["notes"],
        ),
    )
    conn.commit()
    return meta


def backfill(*, force: bool = False) -> dict[str, Any]:
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    if DB_PATH.is_file() and not force:
        # Still allow additive refresh of missing series
        pass

    # Safety: never point at prod
    prod = ROOT / "market_data.db"
    if DB_PATH.resolve() == prod.resolve():
        raise RuntimeError("Refusing to write research store onto prod market_data.db")

    conn = sqlite3.connect(str(DB_PATH))
    results: list[dict[str, Any]] = []
    try:
        _init_db(conn)
        for plan in SERIES_PLAN:
            name = plan["name"]
            print(f">>> {name} via {plan['source']} ...")
            try:
                if plan["source"] == "FRED":
                    series = _fetch_fred(plan["fred_id"])
                    source_id = plan["fred_id"]
                else:
                    series = _fetch_yf(plan["yf_ticker"])
                    source_id = plan["yf_ticker"]
                meta = _store_series(
                    conn,
                    name=name,
                    series=series,
                    source=plan["source"],
                    source_id=source_id,
                    notes=plan["notes"],
                )
                print(
                    f"    {name}: {meta.get('first_date')} -> {meta.get('last_date')} "
                    f"rows={meta.get('rows')} checksum={meta.get('checksum')}"
                )
                results.append(meta)
            except Exception as exc:
                err = {
                    "name": name,
                    "ok": False,
                    "error": str(exc),
                    "source": plan["source"],
                    "notes": plan["notes"],
                }
                print(f"    FAILED {name}: {exc}")
                results.append(err)
    finally:
        conn.close()

    manifest = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "research_db": str(DB_PATH.relative_to(ROOT)),
        "prod_db_untouched": True,
        "disclaimer": "Research store only — freeze stays on; no strategy/live changes.",
        "series": results,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill research macro DB (not prod)")
    ap.add_argument("--force", action="store_true", help="Rebuild tables even if present")
    args = ap.parse_args()
    print(f"Research DB: {DB_PATH}")
    print("Prod market_data.db will NOT be written.")
    manifest = backfill(force=bool(args.force))
    ok = sum(1 for s in manifest["series"] if s.get("ok"))
    print(f"\nDone: {ok}/{len(manifest['series'])} series OK")
    print(f"Manifest: {MANIFEST_PATH}")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
