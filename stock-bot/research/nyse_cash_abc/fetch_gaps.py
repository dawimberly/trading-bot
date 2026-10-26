"""Fill research-only gaps: new member bars and unapplied corporate actions.

Does not open market_data.db, print credentials, or send orders.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import config  # noqa: E402
from alpaca.data.enums import CorporateActionsType  # noqa: E402
from alpaca.data.historical import StockHistoricalDataClient  # noqa: E402
from alpaca.data.historical.corporate_actions import CorporateActionsClient  # noqa: E402
from alpaca.data.requests import CorporateActionsRequest  # noqa: E402
from fetch_alpaca import CUTOFF, DB_PATH, END, START, alpaca_symbol, fetch_symbols, init_db, redact  # noqa: E402

TYPES = [
    CorporateActionsType.STOCK_DIVIDEND,
    CorporateActionsType.SPIN_OFF,
    CorporateActionsType.CASH_MERGER,
    CorporateActionsType.STOCK_MERGER,
    CorporateActionsType.STOCK_AND_CASH_MERGER,
    CorporateActionsType.RIGHTS_DISTRIBUTION,
    CorporateActionsType.NAME_CHANGE,
    CorporateActionsType.UNIT_SPLIT,
    CorporateActionsType.WORTHLESS_REMOVAL,
]
OUT = HERE / "data" / "unapplied_actions.json"
EXTRA_SYMBOLS = ["BE", "P", "TWLO", "VYLR", "HONA", "SOLS"]


def dump_row(kind: str, row) -> dict:
    raw = row.model_dump() if hasattr(row, "model_dump") else {}
    keep = {}
    for key, value in raw.items():
        if key in {"id", "cusip", "source_cusip", "new_cusip", "acquirer_cusip", "acquiree_cusip", "old_cusip"}:
            continue
        if hasattr(value, "isoformat"):
            keep[key] = value.isoformat()
        elif isinstance(value, (str, int, float, bool)) or value is None:
            keep[key] = value
        else:
            keep[key] = str(value)
    keep["kind"] = kind
    return keep


def main() -> int:
    key, secret = config.get_alpaca_credentials(paper=True)
    secrets = (key, secret)
    conn = init_db(DB_PATH)
    present = {
        sym
        for sym, n, mx in conn.execute("SELECT symbol, COUNT(1), MAX(session) FROM bars GROUP BY symbol")
        if n and mx and mx >= CUTOFF
    }
    missing = [sym for sym in EXTRA_SYMBOLS if sym not in present]
    print("fetch_bars", missing, flush=True)
    if missing:
        bars_client = StockHistoricalDataClient(key, secret)
        failures = fetch_symbols(bars_client, conn, missing, secrets, set())
        print("bar_failures", failures, flush=True)
    symbols = sorted(
        {
            r[0]
            for r in conn.execute("SELECT DISTINCT symbol FROM bars")
        }
        | set(EXTRA_SYMBOLS)
        | {"FI", "FISV"}
    )
    actions = CorporateActionsClient(key, secret)
    found: list[dict] = []
    for i in range(0, len(symbols), 40):
        batch = symbols[i : i + 40]
        try:
            got = actions.get_corporate_actions(
                CorporateActionsRequest(
                    symbols=[alpaca_symbol(s) for s in batch],
                    types=TYPES,
                    start=START.date(),
                    end=datetime(2026, 10, 14).date(),
                    limit=1000,
                )
            )
        except Exception as exc:
            print(f"actions error {i} {redact(exc, secrets)}", flush=True)
            continue
        data = got.data if hasattr(got, "data") else {}
        for kind, rows in data.items():
            for row in rows:
                found.append(dump_row(kind, row))
        print(f"actions {i + len(batch)}/{len(symbols)} rows={len(found)}", flush=True)
        time.sleep(0.25)
    OUT.write_text(json.dumps({"retrieved_at": datetime.now(timezone.utc).isoformat(), "rows": found}, indent=2), encoding="utf-8")
    print("wrote", OUT, "n", len(found), flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
