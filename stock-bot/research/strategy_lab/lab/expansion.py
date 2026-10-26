"""What a longer validated snapshot would require. Existing snapshots stay put."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from lab.paths import (
    BARS,
    MEMBERSHIP_FILE,
    PAYMENTS,
    SNAPSHOT_DIR,
    SNAPSHOT_REGISTRY,
    UNAPPLIED_ACTIONS,
)
from lab.snapshots import connect_readonly, identify


class ExpansionError(ValueError):
    """A new snapshot failed a data-quality gate. Nothing was substituted."""


# Market conditions outside the current snapshot. Dates are the condition, not a request to clip history.
TARGET_REGIMES = (
    {"name": "2007-2009 crisis", "start": "2007-10-01", "end": "2009-03-09"},
    {"name": "2020 crash", "start": "2020-02-19", "end": "2020-03-23"},
    {"name": "2022 bear market", "start": "2022-01-03", "end": "2022-10-14"},
)


def bar_span() -> dict:
    conn = connect_readonly()
    try:
        row = conn.execute(
            "SELECT MIN(session), MAX(session), COUNT(DISTINCT session), COUNT(DISTINCT symbol) FROM bars"
        ).fetchone()
        spy = conn.execute("SELECT COUNT(DISTINCT session) FROM bars WHERE symbol = 'SPY'").fetchone()[0]
    finally:
        conn.close()
    return {
        "first_session": row[0],
        "last_session": row[1],
        "sessions": int(row[2]),
        "symbols": int(row[3]),
        "spy_sessions": int(spy),
    }


def _membership_span() -> dict:
    earliest = None
    latest_end = None
    open_ended = 0
    rows = 0
    with MEMBERSHIP_FILE.open(newline="", encoding="utf-8") as handle:
        for rec in csv.DictReader(handle):
            rows += 1
            start = rec.get("start_date") or ""
            end = rec.get("end_date") or ""
            if start and (earliest is None or start < earliest):
                earliest = start
            if end:
                if latest_end is None or end > latest_end:
                    latest_end = end
            else:
                open_ended += 1
    return {"rows": rows, "earliest_start": earliest, "latest_end": latest_end, "open_ended": open_ended}


def _unapplied() -> dict:
    payload = json.loads(UNAPPLIED_ACTIONS.read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for row in payload.get("rows", []):
        kind = str(row.get("kind") or row.get("corporate_action_type") or "unknown")
        counts[kind] = counts.get(kind, 0) + 1
    return {"retrieved_at": payload.get("retrieved_at"), "counts": counts, "rows": sum(counts.values())}


def inventory() -> dict:
    """Local evidence only. No data API is called and no file in the frozen snapshot is written."""
    span = bar_span()
    identity = identify()
    membership = _membership_span()
    actions = _unapplied()
    regimes = []
    for regime in TARGET_REGIMES:
        if regime["end"] < span["first_session"] or regime["start"] > span["last_session"]:
            status = "absent"
            reason = (
                f"The snapshot runs {span['first_session']} through {span['last_session']}. "
                f"{regime['start']} through {regime['end']} is not in it, and no earlier prices were filled in."
            )
        else:
            status = "partial"
            reason = "Part of this span meets the snapshot. Missing sessions were not interpolated."
        regimes.append({**regime, "status": status, "reason": reason})
    requirements = [
        {
            "item": "Split-adjusted SIP open, high, low, and close",
            "status": "required",
            "detail": "Same Adjustment.SPLIT convention as the frozen snapshot. Prices stay undividend-adjusted. A close-only file is not a substitute.",
        },
        {
            "item": "Point-in-time membership for every new session",
            "status": "required",
            "detail": (
                f"The local membership file has {membership['rows']} rows from {membership['earliest_start']}, "
                f"with {membership['open_ended']} open-ended names. Those dates do not supply prices. "
                "The research overlay only records the 2026 constituent changes already used."
            ),
        },
        {
            "item": "Payable date for every cash dividend",
            "status": "required",
            "detail": "bars.sqlite stores ex-date and rate. Payable dates live in a separate payment file. A missing payable date blocks the experiment.",
        },
        {
            "item": "SPY bar on every session",
            "status": "required",
            "detail": f"The current snapshot has {span['spy_sessions']} SPY sessions and {span['sessions']} distinct bar sessions.",
        },
        {
            "item": "Coverage gate",
            "status": "required",
            "detail": "At least 95 percent of point-in-time member-sessions need a usable bar. A shortfall stops the study. The window is not shortened to clear the gate.",
        },
    ]
    sources = [
        {
            "name": "Frozen bars.sqlite",
            "covers": f"{span['first_session']} to {span['last_session']}, {span['symbols']} symbols",
            "extends_history": "no",
            "blocker": "This is the current snapshot. New history is stored as a separate snapshot.",
        },
        {
            "name": "dividend_payments.json",
            "covers": "Payable dates for the current research window",
            "extends_history": "no",
            "blocker": "It does not record dividends before the snapshot. A longer book needs its own payment file.",
        },
        {
            "name": "sp500_ticker_start_end.csv",
            "covers": f"Membership dates from {membership['earliest_start']}",
            "extends_history": "no",
            "blocker": "Membership without SIP bars would invent prices. The file is not used that way.",
        },
        {
            "name": "market_data.db",
            "covers": "Production daily closes. Liquid names in that file start later than this snapshot.",
            "extends_history": "no",
            "blocker": "Not opened. Close-only rows and mixed dollar conventions are rejected.",
        },
        {
            "name": "Alpaca SIP historical bars and corporate actions",
            "covers": "The feed that built the frozen snapshot",
            "extends_history": "not called",
            "blocker": "A longer pull needs that data subscription. This lab did not call it and does not state a price.",
        },
    ]
    return {
        "snapshot_id": identity["snapshot_id"],
        "span": span,
        "requirements": requirements,
        "sources": sources,
        "regimes": regimes,
        "corporate_actions": {
            "unapplied": actions,
            "para_tape": "PARA traded OHLC replaces Alpaca only from 2024-04-24 through 2025-08-06. Earlier and later PARA rows were removed, not estimated.",
            "payable_exception": "ALL ex-date 2023-03-30 has a source payable date of 2023-03-17. Cash is credited on the ex-date. The exception is not applied to other symbols.",
        },
        "registered": list_registered(),
        "payments_file": str(PAYMENTS),
    }


def _require_payable_and_spy(bars_path: Path, payments_path: Path) -> None:
    import sqlite3

    payload = json.loads(payments_path.read_text(encoding="utf-8"))
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ExpansionError("dividend_payments.json has no rows list. Payable dates were not assumed.")
    paid = {
        (str(row.get("symbol")), str(row.get("ex_date")))
        for row in rows
        if row.get("payable_date")
    }
    conn = sqlite3.connect(f"file:{bars_path.as_posix()}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "bars" not in tables or "cash_dividends" not in tables:
            raise ExpansionError("The snapshot needs bars and cash_dividends tables. They were not created to fill the gap.")
        sessions = {row[0] for row in conn.execute("SELECT DISTINCT session FROM bars")}
        spy = {row[0] for row in conn.execute("SELECT DISTINCT session FROM bars WHERE symbol = 'SPY'")}
        missing_spy = sorted(sessions - spy)
        if missing_spy:
            raise ExpansionError(f"SPY is missing on {missing_spy[0]}. That session was not dropped.")
        missing_pay = [
            f"{symbol} {ex_date}"
            for symbol, ex_date in conn.execute("SELECT symbol, ex_date FROM cash_dividends")
            if (symbol, ex_date) not in paid
        ]
    finally:
        conn.close()
    if missing_pay:
        raise ExpansionError(
            f"No payable date for {missing_pay[0]}. The dividend was not credited on the ex-date instead."
        )


def list_registered() -> list[dict]:
    if not SNAPSHOT_REGISTRY.is_file():
        return []
    return list(json.loads(SNAPSHOT_REGISTRY.read_text(encoding="utf-8")).get("snapshots", []))


def register_snapshot(source: Path, *, root: Path | None = None) -> dict:
    """Copy a new bundle into the lab snapshot directory after the quality gates pass.

    The frozen bars file is refused. A failed gate writes nothing.
    """
    source = Path(source)
    if source.resolve() == BARS.resolve():
        raise ExpansionError("The frozen bars snapshot is already registered by path. It was not copied or replaced.")
    bundle = source.parent
    manifest_path = bundle / "manifest.json"
    payments_path = bundle / "dividend_payments.json"
    membership_path = bundle / "membership.csv"
    if not manifest_path.is_file():
        raise ExpansionError("manifest.json is missing. Provenance was not assumed.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("adjustment") != "split" or manifest.get("feed") != "sip":
        raise ExpansionError("The snapshot must say feed sip and adjustment split. A different convention was not accepted.")
    if not payments_path.is_file():
        raise ExpansionError("dividend_payments.json is missing. Payable dates were not skipped.")
    if not membership_path.is_file() or membership_path.stat().st_size == 0:
        raise ExpansionError("membership.csv is missing. Point-in-time membership was not inferred.")
    _require_payable_and_spy(source, payments_path)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    destination_root = root or SNAPSHOT_DIR
    destination = destination_root / digest
    if destination.exists():
        raise ExpansionError(f"Snapshot {digest[:12]} is already stored. The existing copy was kept.")
    destination.mkdir(parents=True)
    try:
        for name in ("bars.sqlite", "manifest.json", "dividend_payments.json", "membership.csv"):
            src = source if name == "bars.sqlite" else bundle / name
            shutil.copy2(src, destination / name)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    record = {
        "snapshot_id": digest,
        "stored_at": str(destination),
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "source": manifest.get("source"),
        "feed": manifest.get("feed"),
        "adjustment": manifest.get("adjustment"),
    }
    registry_path = destination_root / "registry.json"
    current = {"snapshots": []}
    if registry_path.is_file():
        current = json.loads(registry_path.read_text(encoding="utf-8"))
    current.setdefault("snapshots", []).append(record)
    registry_path.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return record
