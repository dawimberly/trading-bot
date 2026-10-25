"""Map stored cash dividends onto ex-dates and payable sessions.

The rate is adjusted for later splits once. A payable date before the ex-date
is not paid early; cash is credited on the ex-date and the row is flagged.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ABC = Path(__file__).resolve().parent.parent / "nyse_cash_abc"
sys.path.insert(0, str(ABC))

from engine import adjusted_dividend_rate  # noqa: E402

HERE = Path(__file__).resolve().parent
PAYMENTS = HERE / "data" / "dividend_payments.json"
SPY_FILE = HERE.parent / "nyse_cash_abc" / "data" / "spy_distributions.json"


def load_payments() -> list[dict]:
    return json.loads(PAYMENTS.read_text(encoding="utf-8"))["rows"]


def map_session(day: str, calendar: list[str]) -> str | None:
    if not calendar or day < calendar[0]:
        return None
    if day > calendar[-1]:
        return day
    if day in set(calendar):
        return day
    return next(d for d in calendar if d >= day)


def cash_session(ex_session: str, payable: str, calendar: list[str]) -> tuple[str, str | None]:
    """Return the session when cash becomes spendable, and a flag if the source date is inverted."""
    if payable < ex_session:
        return ex_session, "payable_before_ex"
    mapped = map_session(payable, calendar)
    if mapped is None:
        return ex_session, "payable_before_calendar"
    return mapped, None


def dividend_lots(divs: pd.DataFrame, splits: pd.DataFrame, calendar: list[str], payments: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    payments = payments if payments is not None else load_payments()
    by_key: dict[tuple, list[dict]] = {}
    for row in payments:
        by_key.setdefault((row["symbol"], row["ex_date"], round(float(row["rate"]), 6)), []).append(row)
    split_map: dict[str, list[tuple[str, float]]] = {}
    for rec in splits.itertuples(index=False):
        if not rec.old_rate:
            continue
        split_map.setdefault(rec.symbol, []).append((rec.ex_date, float(rec.new_rate) / float(rec.old_rate)))
    lots = []
    problems = []
    for rec in divs.itertuples(index=False):
        ex = map_session(rec.ex_date, calendar)
        if ex is None or ex > calendar[-1]:
            continue
        hits = by_key.get((rec.symbol, rec.ex_date, round(float(rec.rate), 6)), [])
        if not hits or not hits[0].get("payable_date"):
            problems.append({"symbol": rec.symbol, "ex_date": rec.ex_date, "rate": float(rec.rate), "problem": "missing_payable"})
            continue
        pay, flag = cash_session(ex, hits[0]["payable_date"], calendar)
        if flag:
            problems.append({"symbol": rec.symbol, "ex_date": rec.ex_date, "payable_date": hits[0]["payable_date"], "problem": flag})
        rate = adjusted_dividend_rate(float(rec.rate), rec.ex_date, split_map.get(rec.symbol, []))
        if rate <= 0:
            continue
        lots.append(
            {
                "symbol": rec.symbol,
                "ex_date": rec.ex_date,
                "ex_session": ex,
                "pay_session": pay,
                "rate": rate,
            }
        )
    return lots, problems


def lots_on(dates: list[str], lots: list[dict]) -> list[dict]:
    allowed = set(dates)
    return [lot for lot in lots if lot["ex_session"] in allowed]


def spy_lots(calendar: list[str]) -> tuple[list[dict], list[str]]:
    stored = json.loads(SPY_FILE.read_text(encoding="utf-8"))
    if stored.get("splits") or stored.get("stock_dividends"):
        raise RuntimeError("SPY split or stock dividend is not handled")
    alpaca = {(row["ex_date"], round(float(row["rate"]), 6)): row for row in load_payments() if row["symbol"] == "SPY"}
    notes = []
    lots = []
    for row in stored["cash_dividends"]:
        key = (row["ex_date"], round(float(row["rate"]), 6))
        hit = alpaca.get(key)
        if hit is None or not hit.get("payable_date"):
            raise RuntimeError(f"SPY payable date missing for {row['ex_date']}")
        if abs(float(hit["rate"]) - float(row["rate"])) > 1e-6:
            raise RuntimeError(f"SPY rate mismatch {row['ex_date']}")
        ex = map_session(row["ex_date"], calendar)
        if ex is None or ex > calendar[-1]:
            continue
        pay, flag = cash_session(ex, hit["payable_date"], calendar)
        if flag:
            notes.append(f"{row['ex_date']} {flag}")
        lots.append({"symbol": "SPY", "ex_date": row["ex_date"], "ex_session": ex, "pay_session": pay, "rate": float(row["rate"])})
    return lots, notes
