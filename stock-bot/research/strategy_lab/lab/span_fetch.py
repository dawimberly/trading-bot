"""Build the 2021-2026 research snapshot. Does not write frozen files or market_data.db."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from lab.paths import BARS, SPAN_BUNDLE, STOCK_BOT
from lab.span_audit import REQUEST_END, REQUEST_START, adjusted_rate, redact, write_audit

if str(STOCK_BOT) not in sys.path:
    sys.path.insert(0, str(STOCK_BOT))

ACTIONS_END = date(2026, 12, 31)
BAR_END = datetime(2026, 10, 15, tzinfo=timezone.utc)
BAR_START = datetime(2021, 1, 1, tzinfo=timezone.utc)
# Alpaca applies a split to historical prices once its ex-date has arrived.
SPLIT_APPLIED_THROUGH = "2026-10-15"
PAGE_CAP = 1000


def bundle_dir() -> Path:
    if SPAN_BUNDLE.resolve() == BARS.parent.resolve():
        raise RuntimeError("snapshot directory collided with the frozen snapshot")
    SPAN_BUNDLE.mkdir(parents=True, exist_ok=True)
    return SPAN_BUNDLE


def init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bars (
            symbol TEXT NOT NULL, session TEXT NOT NULL,
            open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL,
            PRIMARY KEY (symbol, session))"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS cash_dividends (
            symbol TEXT NOT NULL, ex_date TEXT NOT NULL, raw_rate REAL NOT NULL,
            rate REAL NOT NULL, split_factor REAL NOT NULL, payable_date TEXT, special INTEGER,
            PRIMARY KEY (symbol, ex_date, raw_rate))"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS splits (
            symbol TEXT NOT NULL, ex_date TEXT NOT NULL, kind TEXT NOT NULL,
            new_rate REAL, old_rate REAL, PRIMARY KEY (symbol, ex_date, kind))"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS fetch_notes (
            symbol TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT)"""
    )
    conn.commit()
    return conn


def alpaca_symbol(ticker: str) -> str:
    return str(ticker).upper().replace("-", ".").replace("_", ".")


def session_of(value) -> str:
    import pandas as pd

    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC")
    return ts.strftime("%Y-%m-%d")


def requested_symbols() -> list[str]:
    """Every name that was a member at any point in the declared window, including names that left."""
    import pandas as pd
    from modules.pit_universe import sp500_tickers_in_window

    abc = STOCK_BOT / "research" / "nyse_cash_abc"
    if str(abc) not in sys.path:
        sys.path.insert(0, str(abc))
    from membership import research_membership

    frame = research_membership()
    names = sp500_tickers_in_window(frame, pd.Timestamp(REQUEST_START), pd.Timestamp(REQUEST_END))
    return [symbol for symbol in names if symbol != "SPY"]


def _client():
    import config
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.historical.corporate_actions import CorporateActionsClient

    key, secret = config.get_alpaca_credentials(paper=True)
    return StockHistoricalDataClient(key, secret), CorporateActionsClient(key, secret), (key, secret)


def _checkpoint(path: Path, key: str) -> set[str]:
    if not path.is_file():
        return set()
    return set(json.loads(path.read_text(encoding="utf-8")).get(key, []))


def _save_checkpoint(path: Path, key: str, done: set[str]) -> None:
    payload = {}
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
    payload[key] = sorted(done)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def fetch_bars(client, conn: sqlite3.Connection, symbols: list[str], secrets: tuple[str, ...], checkpoint: Path) -> None:
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    done = _checkpoint(checkpoint, "bars_done")
    wanted = {alpaca_symbol(s): s for s in symbols}

    def store(frame) -> set[str]:
        if frame is None or frame.empty:
            return set()
        import pandas as pd

        if not isinstance(frame.index, pd.MultiIndex):
            return set()
        saved: set[str] = set()
        rows = []
        for sym, sub in frame.groupby(level="symbol"):
            local = wanted.get(str(sym).upper())
            if local is None:
                continue
            saved.add(local)
            sub = sub.droplevel("symbol") if "symbol" in getattr(sub.index, "names", []) else sub
            for ts, row in sub.iterrows():
                session = session_of(ts)
                if session < REQUEST_START or session > REQUEST_END:
                    continue
                rows.append(
                    (
                        local,
                        session,
                        float(row["open"]),
                        float(row["high"]),
                        float(row["low"]),
                        float(row["close"]),
                        None if pd.isna(row["volume"]) else float(row["volume"]),
                    )
                )
        conn.executemany(
            "INSERT OR REPLACE INTO bars (symbol, session, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        return saved

    def one(batch: list[str]) -> None:
        try:
            frame = client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=[alpaca_symbol(s) for s in batch],
                    timeframe=TimeFrame.Day,
                    start=BAR_START,
                    end=BAR_END,
                    adjustment=Adjustment.SPLIT,
                    feed=DataFeed.SIP,
                    limit=50000,
                )
            ).df
            found = store(frame)
        except Exception as exc:
            if len(batch) == 1:
                conn.execute(
                    "INSERT OR REPLACE INTO fetch_notes (symbol, status, detail) VALUES (?, 'error', ?)",
                    (batch[0], redact(exc, secrets)),
                )
                conn.commit()
                done.add(batch[0])
                _save_checkpoint(checkpoint, "bars_done", done)
                print(f"bar error {batch[0]}", flush=True)
                return
            mid = len(batch) // 2
            one(batch[:mid])
            one(batch[mid:])
            return
        missing = [s for s in batch if s not in found]
        for sym in missing:
            conn.execute(
                "INSERT OR REPLACE INTO fetch_notes (symbol, status, detail) VALUES (?, 'no_bars', ?)",
                (sym, "SIP split-adjusted request returned no rows"),
            )
        for sym in found:
            conn.execute(
                "INSERT OR REPLACE INTO fetch_notes (symbol, status, detail) VALUES (?, 'ok', NULL)",
                (sym,),
            )
        conn.commit()
        done.update(batch)
        _save_checkpoint(checkpoint, "bars_done", done)
        print(f"bars {len(done)}/{len(symbols)} empty={len(missing)}", flush=True)

    pending = [s for s in symbols if s not in done]
    for i in range(0, len(pending), 5):
        one(pending[i : i + 5])
        time.sleep(0.2)


def _rows_of(payload, key: str) -> list:
    data = payload.data if hasattr(payload, "data") else {}
    return list(data.get(key, []))


def fetch_actions(client, conn: sqlite3.Connection, symbols: list[str], secrets: tuple[str, ...], unresolved_path: Path) -> None:
    from alpaca.data.enums import CorporateActionsType
    from alpaca.data.requests import CorporateActionsRequest

    wanted = {alpaca_symbol(s): s for s in symbols}
    cash_types = [CorporateActionsType.CASH_DIVIDEND, CorporateActionsType.FORWARD_SPLIT, CorporateActionsType.REVERSE_SPLIT]
    other_types = [
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
    unresolved: list[dict] = []

    def pull(batch: list[str], types) -> dict:
        got = client.get_corporate_actions(
            CorporateActionsRequest(
                symbols=[alpaca_symbol(s) for s in batch],
                types=types,
                start=date.fromisoformat(REQUEST_START),
                end=ACTIONS_END,
                limit=1000,
            )
        )
        data = got.data if hasattr(got, "data") else {}
        if not isinstance(data, dict):
            data = {}
        lists = {key: list(rows) for key, rows in data.items() if isinstance(rows, list)}
        total = sum(len(rows) for rows in lists.values())
        if total >= PAGE_CAP and len(batch) > 1:
            mid = len(batch) // 2
            left = pull(batch[:mid], types)
            right = pull(batch[mid:], types)
            merged: dict = {}
            for key in set(left) | set(right):
                merged[key] = list(left.get(key, [])) + list(right.get(key, []))
            return merged
        if total >= PAGE_CAP and len(batch) == 1:
            unresolved.append({"symbol": batch[0], "problem": "corporate_action_page_truncated"})
        return lists

    def dump(kind: str, row) -> dict:
        raw = row.model_dump() if hasattr(row, "model_dump") else {}
        keep = {"kind": kind}
        for key, value in raw.items():
            if hasattr(value, "isoformat"):
                keep[key] = value.isoformat()
            elif isinstance(value, (str, int, float, bool)) or value is None:
                keep[key] = value
        return keep

    for i in range(0, len(symbols), 15):
        batch = symbols[i : i + 15]
        try:
            cash = pull(batch, cash_types)
            extra = pull(batch, other_types)
        except Exception as exc:
            print(f"actions error batch {i}", flush=True)
            unresolved.append(
                {"batch_start": batch[0], "symbols": batch, "problem": "actions_request_failed", "detail": redact(exc, secrets)}
            )
            continue
        div_rows = []
        for row in cash.get("cash_dividends", []):
            local = wanted.get(str(row.symbol).upper())
            if local is None:
                continue
            ex_date = row.ex_date.isoformat()
            if ex_date < REQUEST_START or ex_date > REQUEST_END:
                continue
            div_rows.append(
                (
                    local,
                    ex_date,
                    float(row.rate),
                    None if row.payable_date is None else row.payable_date.isoformat(),
                    1 if row.special else 0,
                )
            )
        split_rows = []
        for kind in ("forward_splits", "reverse_splits"):
            for row in cash.get(kind, []):
                local = wanted.get(str(row.symbol).upper())
                if local is None:
                    continue
                split_rows.append((local, row.ex_date.isoformat(), kind, float(row.new_rate), float(row.old_rate)))
        conn.executemany(
            "INSERT OR REPLACE INTO splits (symbol, ex_date, kind, new_rate, old_rate) VALUES (?, ?, ?, ?, ?)",
            split_rows,
        )
        conn.executemany(
            """INSERT OR REPLACE INTO cash_dividends
               (symbol, ex_date, raw_rate, rate, split_factor, payable_date, special)
               VALUES (?, ?, ?, ?, 1, ?, ?)""",
            [(a, b, c, c, d, e) for a, b, c, d, e in div_rows],
        )
        conn.commit()
        for kind, rows in extra.items():
            for row in rows:
                unresolved.append(dump(kind, row))
        print(f"actions {min(i + 15, len(symbols))}/{len(symbols)} divs={len(div_rows)} unresolved={len(unresolved)}", flush=True)
        time.sleep(0.25)
    _reconcile_dividends(conn)
    unresolved.extend(_known_blockers())
    unresolved_path.write_text(
        json.dumps(
            {
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "note": "These actions are recorded and not applied to prices or share counts.",
                "rows": unresolved,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _reconcile_dividends(conn: sqlite3.Connection) -> None:
    splits: dict[str, list[tuple[str, float, float]]] = {}
    for symbol, ex_date, new_rate, old_rate in conn.execute(
        "SELECT symbol, ex_date, new_rate, old_rate FROM splits"
    ):
        splits.setdefault(symbol, []).append((ex_date, float(new_rate), float(old_rate)))
    updates = []
    for symbol, ex_date, raw in conn.execute("SELECT symbol, ex_date, raw_rate FROM cash_dividends"):
        later = [row for row in splits.get(symbol, []) if row[0] <= SPLIT_APPLIED_THROUGH]
        rate, factor = adjusted_rate(float(raw), ex_date, later)
        updates.append((rate, factor, symbol, ex_date, raw))
    conn.executemany(
        "UPDATE cash_dividends SET rate = ?, split_factor = ? WHERE symbol = ? AND ex_date = ? AND raw_rate = ?",
        updates,
    )
    conn.commit()


def write_membership(path: Path, symbols: list[str]) -> None:
    import csv

    abc = STOCK_BOT / "research" / "nyse_cash_abc"
    if str(abc) not in sys.path:
        sys.path.insert(0, str(abc))
    from membership import research_membership

    frame = research_membership()
    keep = set(symbols)
    rows = frame[frame["ticker"].isin(keep)]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ticker", "start_date", "end_date"])
        for rec in rows.itertuples(index=False):
            end = "" if rec.end is None or str(rec.end) == "NaT" else pd_date(rec.end)
            writer.writerow([rec.ticker, pd_date(rec.start), end])


def pd_date(value) -> str:
    import pandas as pd

    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return ""
    return ts.strftime("%Y-%m-%d")


def write_payments(conn: sqlite3.Connection, path: Path) -> None:
    rows = [
        {
            "symbol": symbol,
            "ex_date": ex_date,
            "payable_date": payable,
            "rate": rate,
            "raw_rate": raw,
            "split_factor": factor,
            "special": bool(special),
        }
        for symbol, ex_date, payable, rate, raw, factor, special in conn.execute(
            "SELECT symbol, ex_date, payable_date, rate, raw_rate, split_factor, special FROM cash_dividends"
        )
    ]
    path.write_text(
        json.dumps(
            {
                "source": "Alpaca corporate actions cash_dividend",
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "rate_policy": (
                    "rate is raw_rate divided by split factors whose ex-date is after the dividend "
                    f"and on or before {SPLIT_APPLIED_THROUGH}"
                ),
                "rows": rows,
                "credentials_printed": False,
            }
        ),
        encoding="utf-8",
    )


def _known_blockers() -> list[dict]:
    """Recorded from the frozen snapshot. This download does not copy or replace those rows."""
    return [
        {
            "problem": "known_tape_mismatch",
            "symbol": "PARA",
            "sessions": "2024-04-24 through 2025-08-06",
            "detail": (
                "The frozen snapshot found Alpaca SIP split-adjusted PARA did not match Paramount traded OHLC. "
                "Alpaca later mapped PARA to a different issuer. This snapshot keeps the Alpaca rows and does not "
                "substitute another vendor."
            ),
        },
        {
            "problem": "known_tape_mismatch",
            "symbol": "NVDA",
            "session": "2024-06-10",
            "field": "high",
            "detail": (
                "The frozen snapshot replaced one NVDA high after it disagreed with that day's SIP minute bars. "
                "This snapshot does not apply that replacement."
            ),
        },
    ]


def write_manifest(path: Path, symbols: list[str], bar_errors: int) -> None:
    path.write_text(
        json.dumps(
            {
                "source": "Alpaca stock bars and corporate actions",
                "feed": "sip",
                "adjustment": "split",
                "dividend_policy": (
                    "Cash dividend raw rate divided by later split factors. "
                    "Prices are not dividend-adjusted. Payable date is required."
                ),
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "request_start": REQUEST_START,
                "request_end": REQUEST_END,
                "symbols_requested": len(symbols),
                "bar_errors": bar_errors,
                "credentials_printed": False,
                "market_data_db_written": False,
                "frozen_snapshot_written": False,
                "validated": False,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> int:
    secrets: tuple[str, ...] = ()
    try:
        root = bundle_dir()
        db_path = root / "bars.sqlite"
        symbols = requested_symbols()
        (root / "requested_symbols.json").write_text(json.dumps(["SPY", *symbols]), encoding="utf-8")
        write_membership(root / "membership.csv", symbols)
        bars_client, actions_client, secrets = _client()
        conn = init_db(db_path)
        checkpoint = root / "checkpoint.json"
        fetch_bars(bars_client, conn, ["SPY", *symbols], secrets, checkpoint)
        fetch_actions(actions_client, conn, ["SPY", *symbols], secrets, root / "unresolved_actions.json")
        write_payments(conn, root / "dividend_payments.json")
        errors = conn.execute("SELECT COUNT(*) FROM fetch_notes WHERE status = 'error'").fetchone()[0]
        conn.close()
        write_manifest(root / "manifest.json", ["SPY", *symbols], int(errors))
        report = write_audit(root, BARS)
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        manifest["validated"] = bool(report["validated"])
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(
            "validated",
            report["validated"],
            "coverage",
            report["checks"]["pit_coverage"]["coverage"],
            flush=True,
        )
        if not report["validated"]:
            print("not registered", flush=True)
            return 2
        from lab.expansion import register_snapshot

        record = register_snapshot(db_path)
        print("registered", record["snapshot_id"][:12], flush=True)
        return 0
    except Exception as exc:
        print(redact(exc, secrets), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
