"""Build sim_2021_2026_v1 from the preserved span snapshot. The span files stay read-only."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from lab.accounting import classify_actions, dividend_resolutions, member_intervals
from lab.paths import SIM_BUNDLE, SPAN_BUNDLE
from lab.span_audit import redact

NVDA_SESSION = "2024-06-10"
NVDA_HIGH = 123.1
NVDA_EVIDENCE = "Alpaca SIP one-minute bars, Adjustment.SPLIT, 2024-06-10: 960 minutes, maximum high 123.1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_source(path: Path) -> tuple[dict, dict, dict, list, list]:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        sessions: dict[str, set[str]] = {}
        closes: dict[tuple[str, str], float] = {}
        for symbol, session, close in conn.execute("SELECT symbol, session, close FROM bars"):
            sessions.setdefault(symbol, set()).add(session)
            closes[(symbol, session)] = float(close)
    finally:
        conn.close()
    membership = []
    import csv

    with (path.parent / "membership.csv").open(encoding="utf-8", newline="") as handle:
        for rec in csv.DictReader(handle):
            membership.append((rec["ticker"], rec["start_date"], rec["end_date"] or None))
    actions = json.loads((path.parent / "unresolved_actions.json").read_text(encoding="utf-8"))["rows"]
    payments = json.loads((path.parent / "dividend_payments.json").read_text(encoding="utf-8"))["rows"]
    return sessions, closes, member_intervals(membership), actions, payments


def _classify(root: Path, sessions, closes, intervals):
    unusable = {(symbol, day) for symbol, days in sessions.items() if symbol == "PARA" for day in days}
    return classify_actions(
        json.loads((root / "unresolved_actions.json").read_text(encoding="utf-8"))["rows"],
        intervals=intervals,
        sessions=sessions,
        unusable=unusable,
        closes=closes,
    )


def _fetch_successors(symbols: list[str], db_path: Path) -> list[str]:
    from datetime import datetime as dt

    from lab.span_fetch import BAR_END, BAR_START, REQUEST_END, REQUEST_START, _client, alpaca_symbol, session_of

    if not symbols:
        return []
    try:
        client, _actions, secrets = _client()
    except Exception as exc:
        print(redact(exc, ()), flush=True)
        return []
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    inserted = []
    conn = sqlite3.connect(db_path)
    try:
        for symbol in symbols:
            try:
                frame = client.get_stock_bars(
                    StockBarsRequest(
                        symbol_or_symbols=alpaca_symbol(symbol),
                        timeframe=TimeFrame.Day,
                        start=BAR_START,
                        end=BAR_END,
                        adjustment=Adjustment.SPLIT,
                        feed=DataFeed.SIP,
                        limit=50000,
                    )
                ).df
            except Exception as exc:
                print(f"successor {symbol} failed", redact(exc, secrets), flush=True)
                continue
            if frame is None or frame.empty:
                print(f"successor {symbol} empty", flush=True)
                continue
            import pandas as pd

            rows = []
            view = frame
            if isinstance(frame.index, pd.MultiIndex):
                view = frame.xs(alpaca_symbol(symbol), level="symbol")
            for ts, row in view.iterrows():
                session = session_of(ts)
                if session < REQUEST_START or session > REQUEST_END:
                    continue
                rows.append((symbol, session, float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]), None if pd.isna(row["volume"]) else float(row["volume"])))
            conn.executemany(
                "INSERT OR REPLACE INTO bars (symbol, session, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.execute(
                "INSERT OR REPLACE INTO fetch_notes (symbol, status, detail) VALUES (?, 'successor', ?)",
                (symbol, "SIP split-adjusted bars for a rename successor. Not part of the preserved span file."),
            )
            conn.commit()
            if rows:
                inserted.append(symbol)
                print(f"successor {symbol} rows={len(rows)}", flush=True)
    finally:
        conn.close()
    return inserted


def _missing_tickers(decision: dict, sessions: dict[str, set[str]]) -> list[str]:
    import re

    ticker = re.compile(r"^[A-Z]{1,5}$")
    found = set()
    for row in decision["material_unresolved"]:
        if row.get("fetch_symbol"):
            found.add(row["fetch_symbol"])
        reason = row.get("reason") or ""
        if " has no usable bar" in reason:
            symbol = reason.split(" has no usable bar", 1)[0]
            if ticker.match(symbol) and not sessions.get(symbol):
                found.add(symbol)
    return sorted(found)


def _align_sessions(decision: dict, spy: set[str]) -> None:
    ordered = sorted(spy)

    def nxt(day: str) -> str | None:
        if day in spy:
            return day
        return next((item for item in ordered if item > day), None)

    for bucket in ("renames", "settlements", "spins"):
        for row in decision[bucket]:
            mapped = nxt(row["session"])
            if mapped is None:
                decision["material_unresolved"].append(
                    {"reason": "action date is after the last SPY session", "session": row["session"], "bucket": bucket}
                )
                decision["simulation_ready"] = False
            else:
                row["source_date"] = row["session"]
                row["session"] = mapped


def build() -> dict:
    source = SPAN_BUNDLE
    dest = SIM_BUNDLE
    if dest.resolve() == source.resolve():
        raise RuntimeError("simulation bundle collided with the preserved span")
    sessions, closes, intervals, _actions, payments = _load_source(source / "bars.sqlite")
    first = _classify(source, sessions, closes, intervals)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name in ("bars.sqlite", "membership.csv", "dividend_payments.json", "unresolved_actions.json", "requested_symbols.json", "manifest.json"):
        shutil.copy2(source / name, dest / name)
    wanted = _missing_tickers(first, sessions)
    fetched = _fetch_successors(wanted, dest / "bars.sqlite")
    sessions, closes, intervals, _actions, payments = _load_source(dest / "bars.sqlite")
    decision = _classify(dest, sessions, closes, intervals)
    _align_sessions(decision, sessions.get("SPY", set()))
    conn = sqlite3.connect(dest / "bars.sqlite")
    try:
        raw = conn.execute(
            "SELECT high FROM bars WHERE symbol = 'NVDA' AND session = ?",
            (NVDA_SESSION,),
        ).fetchone()
        conn.execute(
            """CREATE TABLE IF NOT EXISTS corrections (
                symbol TEXT, session TEXT, field TEXT, raw_value REAL, value REAL, evidence TEXT,
                PRIMARY KEY (symbol, session, field))"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS bar_policy (
                symbol TEXT, session TEXT, status TEXT, detail TEXT,
                PRIMARY KEY (symbol, session))"""
        )
        if raw is not None:
            conn.execute(
                "UPDATE bars SET high = ? WHERE symbol = 'NVDA' AND session = ?",
                (NVDA_HIGH, NVDA_SESSION),
            )
            conn.execute(
                "INSERT OR REPLACE INTO corrections VALUES ('NVDA', ?, 'high', ?, ?, ?)",
                (NVDA_SESSION, float(raw[0]), NVDA_HIGH, NVDA_EVIDENCE),
            )
        conn.executemany(
            "INSERT OR REPLACE INTO bar_policy VALUES (?, ?, 'unusable_wrong_issuer', ?)",
            [
                ("PARA", session, "PARA SIP history is not Paramount. Rows are kept and are not tradable.")
                for session in sorted(sessions.get("PARA", ()))
            ],
        )
        conn.commit()
    finally:
        conn.close()
    resolutions = dividend_resolutions(payments)
    (dest / "accounting.json").write_text(json.dumps(decision, indent=2), encoding="utf-8")
    (dest / "dividend_resolutions.json").write_text(
        json.dumps({"rows": resolutions}, indent=2),
        encoding="utf-8",
    )
    snapshot_id = _sha256(dest / "bars.sqlite")
    manifest = {
        "source": "sim bundle derived from span_2021_2026",
        "parent_snapshot": str(source),
        "parent_bars_sha256": _sha256(source / "bars.sqlite"),
        "snapshot_id": snapshot_id,
        "bundle": dest.name,
        "feed": "sip",
        "adjustment": "split",
        "request_start": "2021-01-01",
        "request_end": "2026-10-14",
        "built_at": datetime.now(timezone.utc).isoformat(),
        "successor_symbols_fetched": fetched,
        "simulation_ready": bool(decision["simulation_ready"]),
        "material_unresolved": len(decision["material_unresolved"]),
        "credentials_printed": False,
        "market_data_db_written": False,
        "frozen_snapshot_written": False,
        "span_snapshot_written": False,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("simulation_ready", manifest["simulation_ready"], "unresolved", manifest["material_unresolved"], flush=True)
    return manifest
