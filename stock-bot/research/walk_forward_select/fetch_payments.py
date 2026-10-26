"""Store cash-dividend payable dates beside this experiment. Does not write the research sqlite."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ABC = HERE.parent / "nyse_cash_abc"
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from alpaca.data.enums import CorporateActionsType  # noqa: E402
from alpaca.data.historical.corporate_actions import CorporateActionsClient  # noqa: E402
from alpaca.data.requests import CorporateActionsRequest  # noqa: E402

DB = ABC / "data" / "bars.sqlite"
OUT = HERE / "data" / "dividend_payments.json"


def redact(exc: BaseException, secrets: tuple[str, ...]) -> str:
    text = f"{type(exc).__name__}: {exc}"
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text[:300]


def main() -> int:
    key, secret = config.get_alpaca_credentials(paper=True)
    secrets = (key, secret)
    client = CorporateActionsClient(key, secret)
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    symbols = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM bars ORDER BY symbol")]
    conn.close()
    rows = []
    failures = []
    start = date(2023, 1, 1)
    end = date(2026, 12, 31)
    wanted = {s.upper().replace("-", "."): s for s in symbols}

    def one(batch: list[str]) -> None:
        try:
            got = client.get_corporate_actions(
                CorporateActionsRequest(
                    symbols=[s.upper().replace("-", ".") for s in batch],
                    types=[CorporateActionsType.CASH_DIVIDEND],
                    start=start,
                    end=end,
                    limit=1000,
                )
            )
        except Exception as exc:
            if len(batch) == 1:
                failures.append({"symbol": batch[0], "error": redact(exc, secrets)})
                print(f"error {batch[0]}", flush=True)
                return
            mid = len(batch) // 2
            one(batch[:mid])
            one(batch[mid:])
            return
        data = got.data if hasattr(got, "data") else {}
        found = list(data.get("cash_dividends", []))
        if len(found) >= 1000 and len(batch) > 1:
            mid = len(batch) // 2
            one(batch[:mid])
            one(batch[mid:])
            return
        for row in found:
            local = wanted.get(str(row.symbol).upper())
            if local is None:
                continue
            rows.append(
                {
                    "symbol": local,
                    "alpaca_symbol": str(row.symbol),
                    "ex_date": row.ex_date.isoformat(),
                    "payable_date": None if row.payable_date is None else row.payable_date.isoformat(),
                    "rate": float(row.rate),
                    "special": bool(row.special),
                }
            )

    for i in range(0, len(symbols), 40):
        batch = symbols[i : i + 40]
        one(batch)
        print(f"payments {min(i + 40, len(symbols))}/{len(symbols)} rows={len(rows)}", flush=True)
    payload = {
        "source": "Alpaca corporate actions cash_dividend payable_date",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "symbols": len(symbols),
        "rows": rows,
        "failures": failures,
        "credentials_printed": False,
        "market_data_db_written": False,
        "research_sqlite_written": False,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload), encoding="utf-8")
    missing = sum(1 for row in rows if not row["payable_date"])
    print(f"wrote {OUT} rows={len(rows)} missing_payable={missing} failures={len(failures)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
