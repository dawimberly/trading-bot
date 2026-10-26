"""Quality gates for the 2021-2026 snapshot. A failed gate is not registered."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

REQUEST_START = "2021-01-01"
REQUEST_END = "2026-10-14"
FIRST_SESSION = "2021-01-04"
COVERAGE_GATE = 0.95

DOWNLOADER_AUDIT = [
    {
        "file": "research/nyse_cash_abc/fetch_alpaca.py",
        "reused_as_writer": False,
        "problem": (
            "START is 2023-01-01, DB_PATH and the checkpoint are the frozen bars.sqlite, "
            "and the membership calendar is clipped to the last warm-up plus two 365-session windows."
        ),
    },
    {
        "file": "research/walk_forward_select/fetch_payments.py",
        "reused_as_writer": False,
        "problem": "Start date and dividend_payments.json path are hard-coded to the frozen window.",
    },
    {
        "file": "research/nyse_cash_abc/fetch_gaps.py",
        "reused_as_writer": False,
        "problem": "It writes the frozen sqlite and unapplied_actions.json, and it uses the same 2023 start.",
    },
]


def redact(exc: BaseException, secrets: tuple[str, ...]) -> str:
    text = f"{type(exc).__name__}: {exc}"
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text[:300]


def adjusted_rate(raw_rate: float, ex_date: str, splits: list[tuple[str, float, float]]) -> tuple[float, float]:
    """Divide a historical cash rate by later split factors. Prices stay split-adjusted."""
    factor = 1.0
    for split_ex, new_rate, old_rate in splits:
        if split_ex > ex_date and old_rate > 0 and new_rate > 0:
            factor *= new_rate / old_rate
    if factor == 0:
        return 0.0, factor
    return float(raw_rate) / factor, factor


def weekday_holes(sessions: list[str], *, limit: int = 4) -> list[dict]:
    """Runs of missing weekdays longer than a normal holiday."""
    if not sessions:
        return [{"start": REQUEST_START, "end": REQUEST_END, "missing_weekdays": None}]
    present = set(sessions)
    holes = []
    run: list[str] = []
    day = datetime.fromisoformat(sessions[0]).date()
    last = datetime.fromisoformat(sessions[-1]).date()
    while day <= last:
        iso = day.isoformat()
        if day.weekday() >= 5:
            day += timedelta(days=1)
            continue
        if iso not in present:
            run.append(iso)
        elif run:
            if len(run) > limit:
                holes.append({"start": run[0], "end": run[-1], "missing_weekdays": len(run)})
            run = []
        day += timedelta(days=1)
    if len(run) > limit:
        holes.append({"start": run[0], "end": run[-1], "missing_weekdays": len(run)})
    return holes


def usable_ohlc(open_: float, high: float, low: float, close: float) -> bool:
    vals = (open_, high, low, close)
    if not all(v is not None and math.isfinite(v) and v > 0 for v in vals):
        return False
    if high + 1e-8 < max(open_, close, low):
        return False
    if low - 1e-8 > min(open_, close, high):
        return False
    return True


def pit_coverage(calendar: list[str], intervals: list[tuple[str, str, str | None]], usable: dict[str, set[str]]) -> dict:
    """Member-session coverage. Names are not removed when their bars are missing."""
    by_year: dict[str, list[int]] = {}
    by_symbol: dict[str, list[int]] = {}
    for symbol, start, end in intervals:
        days = [day for day in calendar if day >= start and (end is None or day < end)]
        good = usable.get(symbol, set())
        hit = sum(1 for day in days if day in good)
        by_symbol.setdefault(symbol, [0, 0])
        by_symbol[symbol][0] += hit
        by_symbol[symbol][1] += len(days)
        for day in days:
            year = day[:4]
            bucket = by_year.setdefault(year, [0, 0])
            bucket[1] += 1
            if day in good:
                bucket[0] += 1
    member_days = sum(item[1] for item in by_symbol.values())
    covered = sum(item[0] for item in by_symbol.values())
    ratio = covered / member_days if member_days else 0.0

    def pack(hit: int, total: int) -> dict:
        return {
            "usable": hit,
            "member_sessions": total,
            "missing": total - hit,
            "coverage": (hit / total) if total else None,
        }

    symbols = [
        {"symbol": symbol, **pack(hit, total)}
        for symbol, (hit, total) in sorted(by_symbol.items())
        if total
    ]
    return {
        "coverage": ratio,
        "usable": covered,
        "member_sessions": member_days,
        "gate": COVERAGE_GATE,
        "passed": ratio >= COVERAGE_GATE and member_days > 0,
        "by_year": {year: pack(hit, total) for year, (hit, total) in sorted(by_year.items())},
        "by_symbol": symbols,
    }


def rate_against_close(rate: float, close: float, *, limit: float = 0.25) -> bool:
    """A split-adjusted cash rate should be a small fraction of that session's close."""
    return math.isfinite(rate) and math.isfinite(close) and close > 0 and rate >= 0 and rate <= limit * close


def connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def load_spy(path: Path) -> list[str]:
    conn = connect(path)
    try:
        return [row[0] for row in conn.execute("SELECT session FROM bars WHERE symbol = 'SPY' ORDER BY session")]
    finally:
        conn.close()


def audit_bundle(bundle: Path, *, frozen_spy: list[str] | None = None) -> dict:
    db = bundle / "bars.sqlite"
    conn = connect(db)
    try:
        spy = [row[0] for row in conn.execute("SELECT session FROM bars WHERE symbol = 'SPY' ORDER BY session")]
        invalid = []
        invalid_count = 0
        for symbol, session, open_, high, low, close in conn.execute(
            "SELECT symbol, session, open, high, low, close FROM bars"
        ):
            if usable_ohlc(open_, high, low, close):
                continue
            invalid_count += 1
            if len(invalid) < 50:
                invalid.append({"symbol": symbol, "session": session})
        notes = {row[0]: row[1] for row in conn.execute("SELECT symbol, status FROM fetch_notes")}
        dividends = list(
            conn.execute(
                "SELECT symbol, ex_date, payable_date, raw_rate, rate, split_factor FROM cash_dividends"
            )
        )
    finally:
        conn.close()
    holes = weekday_holes(spy)
    frozen_missing = []
    if frozen_spy:
        have = set(spy)
        frozen_missing = [day for day in frozen_spy if REQUEST_START <= day <= REQUEST_END and day not in have]
    spy_ok = (
        bool(spy)
        and spy[0] <= FIRST_SESSION
        and spy[-1] >= REQUEST_END
        and not holes
        and not frozen_missing
    )
    in_window = [row for row in dividends if REQUEST_START <= row[1] <= REQUEST_END]
    missing_pay = [
        {"symbol": row[0], "ex_date": row[1]}
        for row in in_window
        if not row[2]
    ]
    adjusted = sum(1 for row in in_window if row[5] and row[5] != 1)
    membership = _intervals(bundle / "membership.csv")
    usable = _usable_bars(db)
    coverage = pit_coverage(spy, membership, usable)
    requested = json.loads((bundle / "requested_symbols.json").read_text(encoding="utf-8"))
    omitted = sorted(set(requested) - set(notes))
    unresolved_rows = _unresolved_rows(bundle / "unresolved_actions.json")
    truncated = [row for row in unresolved_rows if row.get("problem") == "corporate_action_page_truncated"]
    action_errors = [row for row in unresolved_rows if row.get("problem") == "actions_request_failed"]
    rates = _rate_reconciliation(db, in_window)
    checks = {
        "spy_coverage": {
            "passed": spy_ok,
            "first": spy[0] if spy else None,
            "last": spy[-1] if spy else None,
            "sessions": len(spy),
            "weekday_holes": holes,
            "missing_versus_frozen_snapshot": frozen_missing[:30],
            "missing_versus_frozen_count": len(frozen_missing),
        },
        "ohlc": {"passed": invalid_count == 0, "invalid_rows": invalid_count, "examples": invalid[:50]},
        "dividend_payments": {
            "passed": len(in_window) > 0 and not missing_pay,
            "in_window": len(in_window),
            "missing_payable": len(missing_pay),
            "examples": missing_pay[:30],
            "split_adjusted_rates": adjusted,
        },
        "pit_coverage": {
            "passed": coverage["passed"],
            "coverage": coverage["coverage"],
            "usable": coverage["usable"],
            "member_sessions": coverage["member_sessions"],
            "by_year": coverage["by_year"],
        },
        "names_retained": {"passed": not omitted, "omitted": omitted},
        "actions_complete": {
            "passed": not truncated and not action_errors,
            "truncated_pages": len(truncated),
            "request_errors": len(action_errors),
            "examples": (truncated + action_errors)[:20],
        },
    }
    validated = all(item["passed"] for item in checks.values())
    return {
        "validated": validated,
        "request_start": REQUEST_START,
        "request_end": REQUEST_END,
        "checks": checks,
        "recorded": {
            "rate_reconciliation": rates,
            "known_blockers": [
                row for row in unresolved_rows if row.get("problem") == "known_tape_mismatch"
            ],
            "unresolved_action_rows": len(unresolved_rows),
        },
        "downloader_audit": DOWNLOADER_AUDIT,
        "missingness": {"by_year": coverage["by_year"], "by_symbol": coverage["by_symbol"]},
    }


def _unresolved_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return [{"problem": "actions_request_failed", "detail": "unresolved_actions.json is missing"}]
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("rows") if isinstance(payload, dict) else None
    return list(rows) if isinstance(rows, list) else []


def _rate_reconciliation(db: Path, dividends: list[tuple]) -> dict:
    conn = connect(db)
    closes: dict[tuple[str, str], float] = {}
    try:
        for symbol, session, close in conn.execute("SELECT symbol, session, close FROM bars"):
            closes[(symbol, session)] = close
    finally:
        conn.close()
    outliers = []
    above = 0
    missing_bar = 0
    compared = 0
    for symbol, ex_date, _payable, raw_rate, rate, factor in dividends:
        close = closes.get((symbol, ex_date))
        if close is None:
            missing_bar += 1
            continue
        compared += 1
        if rate_against_close(float(rate), float(close)):
            continue
        above += 1
        if len(outliers) < 50:
            outliers.append(
                {
                    "symbol": symbol,
                    "ex_date": ex_date,
                    "raw_rate": raw_rate,
                    "rate": rate,
                    "split_factor": factor,
                    "close": close,
                }
            )
    return {
        "threshold": 0.25,
        "gate": False,
        "note": "Recorded against the split-adjusted close. Rates were not rewritten to pass.",
        "compared": compared,
        "missing_ex_date_bar": missing_bar,
        "above_threshold": above,
        "examples": outliers,
    }


def _intervals(path: Path) -> list[tuple[str, str, str | None]]:
    import csv

    rows = []
    with path.open(newline="", encoding="utf-8") as handle:
        for rec in csv.DictReader(handle):
            end = rec.get("end_date") or None
            rows.append((rec["ticker"], rec["start_date"], end))
    return rows


def _usable_bars(path: Path) -> dict[str, set[str]]:
    conn = connect(path)
    try:
        out: dict[str, set[str]] = {}
        for symbol, session, open_, high, low, close in conn.execute(
            "SELECT symbol, session, open, high, low, close FROM bars"
        ):
            if usable_ohlc(open_, high, low, close):
                out.setdefault(symbol, set()).add(session)
        return out
    finally:
        conn.close()


def write_audit(bundle: Path, frozen_bars: Path) -> dict:
    frozen = load_spy(frozen_bars)
    report = audit_bundle(bundle, frozen_spy=frozen)
    (bundle / "validation.json").write_text(
        json.dumps({key: value for key, value in report.items() if key != "missingness"}, indent=2),
        encoding="utf-8",
    )
    (bundle / "missingness.json").write_text(json.dumps(report["missingness"], indent=2), encoding="utf-8")
    return report
