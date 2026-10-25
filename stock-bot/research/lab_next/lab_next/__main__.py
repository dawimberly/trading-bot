"""Offline transactional file-feed runner. No broker client and no API credentials."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from .engine import SPEC, advance, code_hash, fresh, spec_hash

ROOT = Path(__file__).resolve().parents[1]


def connect(name: str) -> tuple[sqlite3.Connection, Path]:
    if not name or not all(char.isalnum() or char in "_-" for char in name):
        raise ValueError("simple isolated run name required")
    path = ROOT / "runtime" / name
    path.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path / "paper.sqlite", timeout=30)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS frames(time TEXT PRIMARY KEY, hash TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS records(seq INTEGER PRIMARY KEY, kind TEXT, book TEXT, time TEXT, payload TEXT NOT NULL);
        """
    )
    return connection, path


def ingest(connection: sqlite3.Connection, frame: dict) -> bool:
    blob = json.dumps(frame, sort_keys=True, allow_nan=False)
    digest = hashlib.sha256(blob.encode()).hexdigest()
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        old = connection.execute("SELECT hash FROM frames WHERE time=?", (frame["timestamp"],)).fetchone()
        if old:
            if old[0] != digest:
                raise ValueError("revised historical frame refused")
            return False
        row = connection.execute("SELECT payload FROM state WHERE id=1").fetchone()
        state = json.loads(row[0]) if row else fresh()
        if state["spec_hash"] != spec_hash() or state.get("code_hash") != code_hash():
            raise ValueError("spec or code mismatch; create a new run")
        new = advance(state, frame)
        connection.execute("INSERT INTO frames VALUES(?,?,?)", (frame["timestamp"], digest, blob))
        for kind, items in (("event", new["events"]), ("mark", new["marks"])):
            for item in items:
                connection.execute(
                    "INSERT INTO records(kind,book,time,payload) VALUES(?,?,?,?)",
                    (kind, item.get("book"), item.get("time") or item.get("exit_time"), json.dumps(item, allow_nan=False)),
                )
        for book, account in new["books"].items():
            for plan in account["plans"]:
                connection.execute(
                    "INSERT INTO records(kind,book,time,payload) VALUES(?,?,?,?)",
                    ("plan", book, plan["decision_time"], json.dumps(plan, allow_nan=False)),
                )
            for skip in account["skips"]:
                connection.execute(
                    "INSERT INTO records(kind,book,time,payload) VALUES(?,?,?,?)",
                    ("skip", book, skip["time"], json.dumps(skip, allow_nan=False)),
                )
        for info in new["learning"][len(state["learning"]):]:
            connection.execute(
                "INSERT INTO records(kind,book,time,payload) VALUES(?,?,?,?)",
                ("learning", "learner", info["asof"], json.dumps(info, allow_nan=False)),
            )
        new["events"] = []
        new["marks"] = []
        for account in new["books"].values():
            account["plans"] = []
            account["skips"] = []
        connection.execute("INSERT OR REPLACE INTO state VALUES(1,?)", (json.dumps(new, allow_nan=False),))
    return True


def _book_report(connection: sqlite3.Connection, name: str, account: dict) -> dict:
    marks = [
        json.loads(row[0])
        for row in connection.execute(
            "SELECT payload FROM records WHERE kind='mark' AND book=? ORDER BY seq", (name,)
        )
    ]
    peak = 100000.0
    drawdown = 0.0
    exposure = []
    for mark in marks:
        equity = mark["equity"]
        peak = max(peak, equity)
        if peak:
            drawdown = max(drawdown, 1 - equity / peak)
        if equity:
            exposure.append(mark.get("gross", equity - mark["cash"]) / equity)
    realized = sum(trade["pnl"] for trade in account["closed"])
    basis = sum(position["cost"] for position in account["positions"].values())
    receivable = account.get("receivable", 0.0)
    market = account["equity"] - account["cash"] - receivable
    unrealized = market - basis
    dividends = account.get("dividends", 0.0)
    external = account.get("external_flows", 0.0)
    residual = account["cash"] + receivable + basis - 100000 - realized - dividends - external
    if abs(residual) > 0.001:
        raise AssertionError(f"{name} cash ledger does not reconcile: {residual}")
    equity_identity = account["equity"] - (100000 + realized + unrealized + dividends + external)
    if abs(equity_identity) > 0.001:
        raise AssertionError(f"{name} equity identity does not reconcile: {equity_identity}")
    bought = account.get("bought", 0.0)
    sold = account.get("sold", 0.0)
    return {
        "equity": account["equity"],
        "cash": account["cash"],
        "external_flows": external,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "dividends": dividends,
        "net_return": account["equity"] / 100000 - 1,
        "max_drawdown": drawdown,
        "exposure": sum(exposure) / len(exposure) if exposure else 0.0,
        "turnover": (bought + sold) / (2 * 100000),
        "bought": bought,
        "sold": sold,
        "closed_trades": len(account["closed"]),
        "costs": account["costs"],
        "positions": account["positions"],
        "pending": account["pending"],
        "ledger_residual": residual,
        "entry_halt": account["entry_halt"],
    }


def report(connection: sqlite3.Connection, path: Path) -> dict:
    row = connection.execute("SELECT payload FROM state WHERE id=1").fetchone()
    if not row:
        raise ValueError("no frames processed")
    state = json.loads(row[0])
    books = {name: _book_report(connection, name, account) for name, account in state["books"].items()}
    baseline = books["baseline"]["net_return"]
    learner = books["learner"]["net_return"]
    out = {
        "mode": "LOCAL VIRTUAL PAPER ONLY",
        "evidence": state.get("evidence", "see feed_mode on each stored frame"),
        "last": state["last"],
        "complete_sessions": len(state["completed_sessions"]),
        "spec": SPEC,
        "model_id": state["model_id"],
        "learning": state["learning"],
        "benchmarks": {
            "SPY": {"status": "unavailable", "reason": "no verified total-return series in this run"},
            "VTI": {"status": "unavailable", "reason": "no verified total-return series in this run"},
        },
        "learner_minus_baseline": learner - baseline,
        "books": books,
    }
    from .review import build

    out["post_sale_review"] = build(connection, state)
    (path / "report.json").write_text(json.dumps(out, indent=2, allow_nan=False), encoding="utf-8")
    lines = [
        "# Lab Next — virtual paper report",
        "",
        "Separate local simulation. Not an Alpaca account. Not a profitability claim.",
        "The rejected 100/30 parameter selector is not this model and is not revived.",
        "Prediction MAE is not a portfolio-profitability promotion gate.",
        "",
        f"Last processed: {state['last']}. Complete sessions: {len(state['completed_sessions'])}.",
        f"Model: {state['model_id'] or 'fixed ranking fallback'}.",
        "",
        "Exits watch completed five-minute CLOSES, not ticks or intrabar highs.",
        "A 2.00% retreat from the highest observed close latches a full exit. 1.99% does not.",
        "The exit stays latched if price bounces. Same-symbol reentry waits 30 minutes after the virtual fill.",
        "",
        "| Book | Equity | Cash | Realized | Unrealized | Return | MaxDD | Exposure | Turnover | Closed | Costs |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, account in books.items():
        lines.append(
            f"| {name} | {account['equity']:.2f} | {account['cash']:.2f} | {account['realized_pnl']:.2f} | "
            f"{account['unrealized_pnl']:.2f} | {account['net_return']:.2%} | {account['max_drawdown']:.2%} | "
            f"{account['exposure']:.2%} | {account['turnover']:.4f} | {account['closed_trades']} | {account['costs']:.2f} |"
        )
    lines += [
        "",
        f"Learner minus baseline: {learner - baseline:.2%} of starting equity.",
        "External cash flows are zero. SPY and VTI total returns are unavailable unless a verified series is attached.",
        "Post-sale horizons are price follow-through on the exchange calendar, not a sale verdict.",
        "Synthetic frames are plumbing. They are not market evidence.",
    ]
    (path / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def status(connection: sqlite3.Connection) -> dict:
    row = connection.execute("SELECT payload FROM state WHERE id=1").fetchone()
    frames = connection.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    return {
        "running": False,
        "pid": None,
        "frames": frames,
        "last": json.loads(row[0])["last"] if row else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--feed", type=Path)
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if not args.feed and not args.report_only and not args.status:
        parser.error("--feed, --report-only, or --status required")
    connection, path = connect(args.run)
    count = 0
    try:
        if args.status and not args.feed:
            print(json.dumps(status(connection)))
            return
        if args.feed:
            with args.feed.open(encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        count += ingest(connection, json.loads(line))
        payload = report(connection, path)
        print(json.dumps({"processed_frames": count, "last": payload["last"], "report": str(path / "REPORT.md"), "mode": payload["mode"], "running": False}))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
