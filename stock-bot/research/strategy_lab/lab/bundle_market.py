"""Load one selected research bundle. Frozen reproduction keeps its own files."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

from lab.paths import ABC, SIM_V1, SIM_V2, WALK
from lab.snapshots import identify
from lab.specs import SpecError

for path in (str(ABC), str(WALK)):
    if path not in sys.path:
        sys.path.insert(0, path)


def resolve_snapshot(snapshot_id: str | None) -> tuple[str, str, Path | None]:
    """Return ('frozen' or 'sim', snapshot id, bundle directory). Unknown ids are rejected."""
    frozen = identify()["snapshot_id"]
    if snapshot_id in (None, "", "frozen", frozen):
        return "frozen", frozen, None
    matches: list[tuple[Path, str]] = []
    for root in (SIM_V1, SIM_V2):
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        sim_id = manifest.get("snapshot_id")
        if snapshot_id in {sim_id, root.name}:
            matches.append((root, sim_id))
    if len(matches) == 1:
        return "sim", matches[0][1], matches[0][0]
    if len(matches) > 1:
        raise SpecError(f"snapshot {snapshot_id} matches more than one simulation bundle")
    raise SpecError(f"snapshot {snapshot_id} is not the frozen tape, {SIM_V1.name}, or {SIM_V2.name}")


def load_frozen_market() -> dict:
    from cash_lots import dividend_lots, spy_lots
    from data_access import load_market

    market = load_market()
    lots, problems = dividend_lots(market["divs"], market["splits"], market["calendar"])
    market["lots"] = lots
    market["lot_problems"] = problems
    market["spy_lot_list"] = spy_lots(market["calendar"])[0]
    market["snapshot"] = identify()
    market["kind"] = "frozen"
    market["simulation_ready"] = True
    market["blockers"] = []
    return market


def load_sim_market(root=None) -> dict:
    from cash_lots import dividend_lots
    from engine import Bar, usable
    from run_abc import membership_index

    root = root or SIM_V1
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    accounting = json.loads((root / "accounting.json").read_text(encoding="utf-8"))
    resolutions = {
        (row["symbol"], row["ex_date"])
        for row in json.loads((root / "dividend_resolutions.json").read_text(encoding="utf-8"))["rows"]
        if row.get("resolution") == "not_entitled"
    }
    conn = sqlite3.connect(f"file:{(root / 'bars.sqlite').as_posix()}?mode=ro", uri=True)
    try:
        blocked = {(row[0], row[1]) for row in conn.execute("SELECT symbol, session FROM bar_policy WHERE status = 'unusable_wrong_issuer'")}
        lookup: dict[str, dict[str, Bar]] = {}
        for symbol, session, open_, high, low, close in conn.execute(
            "SELECT symbol, session, open, high, low, close FROM bars"
        ):
            if (symbol, session) in blocked:
                continue
            lookup.setdefault(symbol, {})[session] = Bar(float(open_), float(high), float(low), float(close))
        divs = pd.read_sql("SELECT symbol, ex_date, raw_rate AS rate FROM cash_dividends", conn)
        splits = pd.read_sql("SELECT symbol, ex_date, kind, new_rate, old_rate FROM splits", conn)
        calendar = [
            row[0]
            for row in conn.execute("SELECT session FROM bars WHERE symbol = 'SPY' ORDER BY session")
            if usable(lookup.get("SPY", {}).get(row[0]))
        ]
    finally:
        conn.close()
    for alias in accounting.get("aliases", []):
        donor = lookup.get(alias["source"], {})
        borrowed = {}
        for day, bar in donor.items():
            if day < alias["start"]:
                continue
            if alias.get("end") and day >= alias["end"]:
                continue
            borrowed[day] = bar
        lookup[alias["symbol"]] = borrowed
    membership = pd.read_csv(root / "membership.csv")
    membership = membership.rename(columns={"start_date": "start", "end_date": "end"})
    payments = json.loads((root / "dividend_payments.json").read_text(encoding="utf-8"))["rows"]
    raw_payments = []
    for row in payments:
        if (row["symbol"], row["ex_date"]) in resolutions:
            continue
        copied = dict(row)
        copied["rate"] = row.get("raw_rate", row["rate"])
        raw_payments.append(copied)
    divs = divs[~divs.apply(lambda rec: (rec["symbol"], rec["ex_date"]) in resolutions, axis=1)]
    lots, problems = dividend_lots(divs, splits, calendar, payments=raw_payments)
    spy_lots = [lot for lot in lots if lot["symbol"] == "SPY"]
    return {
        "kind": "sim",
        "snapshot": {
            "snapshot_id": manifest["snapshot_id"],
            "path": str(root / "bars.sqlite"),
            "provenance": {
                "source": manifest.get("source"),
                "feed": manifest.get("feed"),
                "adjustment": manifest.get("adjustment"),
                "parent_bars_sha256": manifest.get("parent_bars_sha256"),
                "simulation_ready": manifest.get("simulation_ready"),
            },
        },
        "calendar": calendar,
        "lookup": lookup,
        "membership": membership_index(membership),
        "divs": divs,
        "splits": splits,
        "lots": lots,
        "lot_problems": problems,
        "spy_lot_list": spy_lots,
        "accounting": accounting,
        "simulation_ready": bool(manifest.get("simulation_ready")),
        "search_blocked": bool(manifest.get("search_blocked", not manifest.get("simulation_ready"))),
        "search_block_reasons": list(manifest.get("search_block_reasons") or []),
        "representative_universe": bool(manifest.get("representative_universe", False)),
        "blockers": accounting.get("material_unresolved", []),
    }
