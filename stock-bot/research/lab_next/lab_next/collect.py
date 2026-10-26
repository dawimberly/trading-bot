"""Isolated forward collector. No broker orders and no production database writes."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from .__main__ import connect, ingest
from .calendar import ET, bar_starts, close_verified, is_session, previous_session, session_bounds
from .engine import BLOCKED_ACTIONS
from .provider import (
    ADJUSTMENT,
    FEED,
    UNIVERSE_PATH,
    DataError,
    digest,
    execution_class,
    fetch_actions,
    fetch_bars,
    headers,
    load_credentials,
    load_universe,
    merge_bars,
    normalize_actions,
    regular_bars,
    session_audit,
)

RUN = Path(__file__).resolve().parents[1] / "runtime" / "forward_v1"
POLL_SECONDS = 30


def _now() -> datetime:
    return datetime.now(ET).replace(microsecond=0)


def pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def _log(text: str) -> None:
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN / "collector.log").open("a", encoding="utf-8") as handle:
        handle.write(f"{_now().isoformat()} {text}\n")


def open_store(directory: Path | None = None) -> sqlite3.Connection:
    root = directory or RUN
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root / "observations.sqlite", timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS observations (
            symbol TEXT NOT NULL,
            bar_start TEXT NOT NULL,
            bar_end TEXT NOT NULL,
            session_date TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            feed TEXT NOT NULL,
            adjustment TEXT NOT NULL,
            provider TEXT NOT NULL,
            received_at TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL,
            PRIMARY KEY (symbol, bar_start)
        );
        CREATE TABLE IF NOT EXISTS polls (
            id INTEGER PRIMARY KEY,
            retrieved_at TEXT NOT NULL,
            session_date TEXT NOT NULL,
            observations INTEGER NOT NULL,
            inserted INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS revisions (
            id INTEGER PRIMARY KEY,
            symbol TEXT NOT NULL,
            bar_start TEXT NOT NULL,
            received_at TEXT NOT NULL,
            previous_sha TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL,
            close REAL NOT NULL
        );
        """
    )
    return connection


def save_observation(connection: sqlite3.Connection, row: dict) -> str:
    existing = connection.execute(
        "SELECT open, high, low, close, volume, raw_sha256 FROM observations WHERE symbol=? AND bar_start=?",
        (row["symbol"], row["bar_start"]),
    ).fetchone()
    if existing is None:
        connection.execute(
            """
            INSERT INTO observations(
                symbol, bar_start, bar_end, session_date, open, high, low, close, volume,
                feed, adjustment, provider, received_at, raw_sha256
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["symbol"],
                row["bar_start"],
                row["bar_end"],
                row["session_date"],
                row["open"],
                row["high"],
                row["low"],
                row["close"],
                row["volume"],
                row["feed"],
                row["adjustment"],
                row["provider"],
                row["received_at"],
                row["raw_sha256"],
            ),
        )
        return "inserted"
    same = (
        existing[0] == row["open"]
        and existing[1] == row["high"]
        and existing[2] == row["low"]
        and existing[3] == row["close"]
        and existing[4] == row["volume"]
    )
    if same:
        return "duplicate"
    connection.execute(
        "INSERT INTO revisions(symbol, bar_start, received_at, previous_sha, raw_sha256, close) VALUES (?,?,?,?,?,?)",
        (row["symbol"], row["bar_start"], row["received_at"], existing[5], row["raw_sha256"], row["close"]),
    )
    return "revised"


def _target(now: datetime) -> tuple[str, datetime, datetime]:
    day = now.astimezone(ET).date().isoformat()
    if not is_session(day):
        day = previous_session(day) if day > "1900-01-01" else day
        start, end = session_bounds(day)
        return day, start, end + timedelta(minutes=5)
    start, end = session_bounds(day)
    now = now.astimezone(ET)
    if now < start + timedelta(minutes=5):
        day = previous_session(day)
        start, end = session_bounds(day)
        return day, start, end + timedelta(minutes=5)
    if now >= end:
        return day, start, end + timedelta(minutes=5)
    last = start
    while last + timedelta(minutes=5) <= now and last + timedelta(minutes=5) < end:
        last += timedelta(minutes=5)
    return day, start, last + timedelta(minutes=5)


def _write_raw(kind: str, retrieved: str, payload) -> str:
    folder = RUN / "raw" / kind
    folder.mkdir(parents=True, exist_ok=True)
    stamp = retrieved.replace(":", "").replace("-", "")
    path = folder / f"{stamp}.json"
    path.write_text(json.dumps(payload, default=str), encoding="utf-8")
    return digest(payload)


def _frames(day: str, found: dict, gaps: set[str], audit: dict, received_at: str, revised: set[str]) -> list[dict]:
    blocked = {event["symbol"] for event in audit["events"] if event["type"] in BLOCKED_ACTIONS}
    blocked |= gaps | revised
    clocks = bar_starts(day)
    newest = None
    for clock in clocks:
        for rows in found.values():
            if clock in rows:
                newest = datetime.fromisoformat(rows[clock]["bar_start"])
    frames = []
    if newest is None:
        return frames
    received = datetime.fromisoformat(received_at)
    tradable = list(found)
    for clock in clocks:
        bars = {}
        bar_start = None
        for symbol in tradable:
            row = found[symbol].get(clock)
            if row is None:
                continue
            bar_start = datetime.fromisoformat(row["bar_start"])
            bars[symbol] = {
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "eligible": symbol not in blocked,
            }
        if bar_start is None:
            continue
        frames.append(
            {
                "timestamp": bar_start.isoformat(),
                "as_of": (bar_start + timedelta(minutes=5)).isoformat(),
                "received_at": received_at,
                "complete": True,
                "price_basis": ADJUSTMENT,
                "feed_mode": "forward_observation",
                "execution": execution_class(bar_start, received, newest),
                "provider": "alpaca_data",
                "feed": FEED,
                "bars": bars,
                "action_audit": {
                    "source": audit["source"],
                    "independent": True,
                    "coverage_from": day,
                    "coverage_through": day,
                    "symbols": sorted(bars),
                    "events": [event for event in audit["events"] if clock == "09:30" and event["symbol"] in bars],
                    "pages_complete": audit["pages_complete"],
                },
            }
        )
    return frames


def _benchmarks(day: str, found: dict, events: list[dict], pages_complete: bool) -> dict:
    out = {}
    for symbol, rows in found.items():
        ordered = [rows[clock] for clock in bar_starts(day) if clock in rows]
        expected = len(bar_starts(day))
        if len(ordered) != expected or not pages_complete:
            out[symbol] = {"status": "price_only" if ordered else "unavailable", "total_return": None}
            if ordered:
                out[symbol]["price_return"] = ordered[-1]["close"] / ordered[0]["open"] - 1
            continue
        price_return = ordered[-1]["close"] / ordered[0]["open"] - 1
        dividends = [
            event for event in events
            if event["symbol"] == symbol and event["type"] == "cash_dividend" and event["ex_date"] == day
        ]
        if any(event["type"].endswith("unparsed") or event["type"] in BLOCKED_ACTIONS for event in events if event["symbol"] == symbol and event["ex_date"] == day):
            out[symbol] = {"status": "unavailable", "price_return": price_return, "total_return": None}
            continue
        cash = sum(event.get("amount") or 0 for event in dividends)
        out[symbol] = {
            "status": "total_return" if cash else "session_price_return_equals_total_return",
            "price_return": price_return,
            "total_return": (ordered[-1]["close"] + cash) / ordered[0]["open"] - 1,
        }
    return out


def cycle() -> dict:
    universe = load_universe()
    now = _now()
    day, query_start, query_end = _target(now)
    symbols = list(universe["tradable"]) + list(universe["benchmarks"])
    key, secret = load_credentials()
    header = headers(key, secret)
    bar_pages, bars_complete = fetch_bars(header, symbols, query_start, query_end)
    retrieved = bar_pages[-1]["retrieved_at"]
    raw_sha = _write_raw("bars", retrieved, {"feed": FEED, "adjustment": ADJUSTMENT, "pages": bar_pages})
    merged = merge_bars(bar_pages)
    found, gaps, extras = regular_bars(merged, day, symbols)
    action_pages, actions_complete = fetch_actions(
        header,
        symbols,
        universe["frozen_at"][:10],
        (now.date() + timedelta(days=10)).isoformat(),
    )
    _write_raw("actions", action_pages[-1]["retrieved_at"], {"pages": action_pages})
    events, unscoped = normalize_actions(action_pages)
    audit = session_audit(events, symbols, day, actions_complete and bars_complete, unscoped)
    counts = {"inserted": 0, "duplicate": 0, "revised": 0}
    revised = set()
    connection = open_store()
    try:
        with connection:
            for symbol, rows in found.items():
                for row in rows.values():
                    stored = dict(row)
                    stored.update(
                        {
                            "symbol": symbol,
                            "session_date": day,
                            "feed": FEED,
                            "adjustment": ADJUSTMENT,
                            "provider": "alpaca_data",
                            "received_at": retrieved,
                            "raw_sha256": raw_sha,
                        }
                    )
                    result = save_observation(connection, stored)
                    counts[result] += 1
                    if result == "revised":
                        revised.add(symbol)
        connection.execute(
            "INSERT INTO polls(retrieved_at, session_date, observations, inserted) VALUES (?,?,?,?)",
            (retrieved, day, 0, counts["inserted"]),
        )
        connection.commit()
        observation_count = connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        connection.execute("UPDATE polls SET observations=? WHERE id=last_insert_rowid()", (observation_count,))
        connection.commit()
        latest = connection.execute(
            "SELECT MAX(bar_end), MAX(received_at) FROM observations"
        ).fetchone()
    finally:
        connection.close()
    universe_hash = digest(json.loads(UNIVERSE_PATH.read_text(encoding="utf-8")))
    hash_path = RUN / "universe.sha256"
    if not hash_path.exists():
        hash_path.write_text(universe_hash, encoding="utf-8")
    universe_unchanged = hash_path.read_text(encoding="utf-8").strip() == universe_hash
    virtual_ok = bool(audit["pages_complete"] and universe_unchanged and close_verified(day))
    ingested = 0
    if audit["pages_complete"] and universe_unchanged:
        tradable_found = {symbol: found[symbol] for symbol in universe["tradable"]}
        frames = _frames(day, tradable_found, set(gaps), audit, retrieved, revised)
        paper, _path = connect("forward_v1")
        try:
            stored = {row[0] for row in paper.execute("SELECT time FROM frames")}
            for frame in frames:
                if frame["timestamp"] in stored:
                    continue
                if ingest(paper, frame):
                    ingested += 1
                    stored.add(frame["timestamp"])
        finally:
            paper.close()
    else:
        virtual_ok = False
    benchmarks = _benchmarks(day, {symbol: found[symbol] for symbol in universe["benchmarks"]}, events, audit["pages_complete"])
    summary = {
        "session": day,
        "retrieved_at": retrieved,
        "feed": FEED,
        "adjustment": ADJUSTMENT,
        "bars_complete": bars_complete,
        "actions_complete": actions_complete,
        "unscoped_actions": unscoped,
        "calendar_verified": close_verified(day),
        "gaps": {symbol: len(missing) for symbol, missing in gaps.items()},
        "extras_ignored": len(extras),
        "counts": counts,
        "observations": observation_count,
        "latest_bar_end": latest[0],
        "latest_received_at": latest[1],
        "ingested_frames": ingested,
        "virtual_execution": bool(virtual_ok),
        "symbol_gaps": sorted(gaps),
        "revised_symbols": sorted(revised),
        "benchmarks": benchmarks,
        "historical_bootstrap": False,
        "checked_at": now.isoformat(),
    }
    (RUN / "last_cycle.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    _log(
        f"session={day} inserted={counts['inserted']} duplicate={counts['duplicate']} "
        f"revised={counts['revised']} ingested={ingested} gaps={len(gaps)} virtual={summary['virtual_execution']}"
    )
    return summary


def _lock_path() -> Path:
    return RUN / "collector.lock"


def _stop_path() -> Path:
    return RUN / "STOP"


def read_lock() -> dict | None:
    path = _lock_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def status() -> dict:
    lock = read_lock() or {}
    pid = lock.get("pid")
    alive = pid_alive(pid) if isinstance(pid, int) else False
    heartbeat = {}
    path = RUN / "heartbeat.json"
    if path.is_file():
        heartbeat = json.loads(path.read_text(encoding="utf-8"))
    observations = None
    store = RUN / "observations.sqlite"
    if store.is_file():
        connection = sqlite3.connect(store)
        try:
            observations = connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        finally:
            connection.close()
    return {
        "running": alive,
        "pid": pid if alive else None,
        "started_at": lock.get("started_at") if alive else None,
        "last_progress_at": heartbeat.get("at"),
        "observations": observations,
        "latest_bar_end": heartbeat.get("latest_bar_end"),
        "latest_received_at": heartbeat.get("latest_received_at"),
    }


def _write_heartbeat(summary: dict) -> None:
    payload = {
        "pid": os.getpid(),
        "at": _now().isoformat(),
        "observations": summary.get("observations"),
        "latest_bar_end": summary.get("latest_bar_end"),
        "latest_received_at": summary.get("latest_received_at"),
        "inserted": summary.get("counts", {}).get("inserted"),
    }
    (RUN / "heartbeat.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def serve() -> None:
    current = read_lock()
    if current and pid_alive(current.get("pid")):
        print(json.dumps(status()))
        return
    if _stop_path().exists():
        _stop_path().unlink()
    RUN.mkdir(parents=True, exist_ok=True)
    _lock_path().write_text(
        json.dumps({"pid": os.getpid(), "started_at": _now().isoformat()}),
        encoding="utf-8",
    )
    _log(f"start pid={os.getpid()}")
    try:
        while not _stop_path().exists():
            try:
                summary = cycle()
            except Exception as exc:
                _log(f"cycle_error {type(exc).__name__}: {exc}")
                summary = {"observations": None, "latest_bar_end": None, "latest_received_at": None, "counts": {}}
            _write_heartbeat(summary)
            for _ in range(POLL_SECONDS):
                if _stop_path().exists():
                    break
                time.sleep(1)
    finally:
        lock = read_lock()
        if lock and lock.get("pid") == os.getpid() and _lock_path().exists():
            _lock_path().unlink()
        _log(f"stop pid={os.getpid()}")


def stop() -> None:
    RUN.mkdir(parents=True, exist_ok=True)
    _stop_path().write_text("stop\n", encoding="utf-8")
    lock = read_lock()
    pid = lock.get("pid") if lock else None
    if isinstance(pid, int) and pid != os.getpid():
        for _ in range(5):
            if not pid_alive(pid):
                break
            time.sleep(1)
        if pid_alive(pid):
            import subprocess

            command = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                text=True,
            )
            if "lab_next.collect" not in command:
                raise SystemExit("lock pid is not the lab next collector")
            subprocess.check_call(["taskkill", "/PID", str(pid)])
    print(json.dumps(status()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    if args.once:
        print(json.dumps(cycle(), default=str))
        return
    if args.start or args.resume:
        serve()
        return
    if args.stop:
        stop()
        return
    if args.report:
        path = RUN / "last_cycle.json"
        print(path.read_text(encoding="utf-8") if path.is_file() else "{}")
        return
    print(json.dumps(status()))


if __name__ == "__main__":
    main()
