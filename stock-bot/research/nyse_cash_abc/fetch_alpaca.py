"""Download split-adjusted SIP daily OHLC into this research folder only.

Uses the existing paper Alpaca data credentials. Does not print them.
Does not open market_data.db for writing and does not send orders.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from alpaca.data.enums import Adjustment, CorporateActionsType, DataFeed  # noqa: E402
from alpaca.data.historical import StockHistoricalDataClient  # noqa: E402
from alpaca.data.historical.corporate_actions import CorporateActionsClient  # noqa: E402
from alpaca.data.requests import CorporateActionsRequest, StockBarsRequest  # noqa: E402
from alpaca.data.timeframe import TimeFrame  # noqa: E402
from modules.pit_universe import load_sp500_membership, sp500_tickers_in_window  # noqa: E402

from engine import EVAL_SESSIONS, WARMUP  # noqa: E402

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DB_PATH = DATA / "bars.sqlite"
MANIFEST_PATH = DATA / "manifest.json"
CHECKPOINT_PATH = DATA / "checkpoint.json"
CUTOFF = "2026-10-14"
START = datetime(2023, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 10, 15, tzinfo=timezone.utc)


def redact(exc: BaseException, secrets: tuple[str, ...]) -> str:
    text = f"{type(exc).__name__}: {exc}"
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text[:300]


def session_of(value) -> str:
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC")
    return ts.strftime("%Y-%m-%d")


def alpaca_symbol(ticker: str) -> str:
    return str(ticker).upper().replace("-", ".").replace("_", ".")


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bars (
            symbol TEXT NOT NULL,
            session TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL,
            PRIMARY KEY (symbol, session)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cash_dividends (
            symbol TEXT NOT NULL,
            ex_date TEXT NOT NULL,
            rate REAL NOT NULL,
            PRIMARY KEY (symbol, ex_date, rate)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS splits (
            symbol TEXT NOT NULL,
            ex_date TEXT NOT NULL,
            kind TEXT NOT NULL,
            new_rate REAL,
            old_rate REAL,
            PRIMARY KEY (symbol, ex_date, kind)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fetch_notes (
            symbol TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            detail TEXT
        )
        """
    )
    conn.commit()
    return conn


def load_checkpoint() -> set[str]:
    if not CHECKPOINT_PATH.is_file():
        return set()
    return set(json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8")).get("bars_done", []))


def save_checkpoint(done: set[str]) -> None:
    CHECKPOINT_PATH.write_text(
        json.dumps({"bars_done": sorted(done)}, indent=2),
        encoding="utf-8",
    )


def bars_frame(client, symbols: list[str]):
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=START,
        end=END,
        adjustment=Adjustment.SPLIT,
        feed=DataFeed.SIP,
        limit=50000,
    )
    return client.get_stock_bars(request).df


def store_frame(conn: sqlite3.Connection, frame, wanted: dict[str, str]) -> set[str]:
    if frame is None or frame.empty:
        return set()
    saved: set[str] = set()
    grouped = frame.groupby(level="symbol") if isinstance(frame.index, pd.MultiIndex) else None
    if grouped is None:
        return saved
    rows = []
    for sym, sub in grouped:
        local = wanted.get(str(sym).upper())
        if local is None:
            continue
        saved.add(local)
        sub = sub.droplevel("symbol") if "symbol" in getattr(sub.index, "names", []) else sub
        for ts, row in sub.iterrows():
            session = session_of(ts)
            if session > CUTOFF:
                continue
            rows.append(
                (
                    local,
                    session,
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    float(row["volume"]) if pd.notna(row["volume"]) else None,
                )
            )
    conn.executemany(
        "INSERT OR REPLACE INTO bars (symbol, session, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return saved


def fetch_symbols(client, conn, symbols: list[str], secrets: tuple[str, ...], done: set[str]) -> list[str]:
    wanted = {alpaca_symbol(s): s for s in symbols}
    pending = [s for s in symbols if s not in done]
    failures: list[str] = []

    def one(batch: list[str]) -> None:
        if not batch:
            return
        try:
            frame = bars_frame(client, [alpaca_symbol(s) for s in batch])
            found = store_frame(conn, frame, wanted)
        except Exception as exc:
            if len(batch) == 1:
                detail = redact(exc, secrets)
                failures.append(batch[0])
                conn.execute(
                    "INSERT OR REPLACE INTO fetch_notes (symbol, status, detail) VALUES (?, ?, ?)",
                    (batch[0], "error", detail),
                )
                conn.commit()
                done.add(batch[0])
                save_checkpoint(done)
                print(f"bar error {batch[0]} {detail}", flush=True)
                return
            mid = len(batch) // 2
            one(batch[:mid])
            one(batch[mid:])
            return
        missing = [s for s in batch if s not in found]
        for sym in missing:
            conn.execute(
                "INSERT OR REPLACE INTO fetch_notes (symbol, status, detail) VALUES (?, ?, ?)",
                (sym, "no_bars", "SIP split-adjusted request returned no rows"),
            )
        for sym in found:
            conn.execute(
                "INSERT OR REPLACE INTO fetch_notes (symbol, status, detail) VALUES (?, ?, ?)",
                (sym, "ok", None),
            )
        conn.commit()
        done.update(batch)
        save_checkpoint(done)
        print(f"bars {len(done)}/{len(symbols)} last_batch={len(batch)} empty={len(missing)}", flush=True)

    for i in range(0, len(pending), 5):
        one(pending[i : i + 5])
        time.sleep(0.2)
    return failures


def fetch_actions(client, conn, symbols: list[str], secrets: tuple[str, ...]) -> None:
    wanted = {alpaca_symbol(s): s for s in symbols}
    types = [
        CorporateActionsType.CASH_DIVIDEND,
        CorporateActionsType.FORWARD_SPLIT,
        CorporateActionsType.REVERSE_SPLIT,
        CorporateActionsType.STOCK_DIVIDEND,
    ]
    for i in range(0, len(symbols), 40):
        batch = symbols[i : i + 40]
        try:
            got = client.get_corporate_actions(
                CorporateActionsRequest(
                    symbols=[alpaca_symbol(s) for s in batch],
                    types=types,
                    start=START.date(),
                    end=datetime(2026, 10, 14).date(),
                    limit=1000,
                )
            )
        except Exception as exc:
            print(f"actions error batch {i} {redact(exc, secrets)}", flush=True)
            continue
        data = got.data if hasattr(got, "data") else {}
        div_rows = []
        split_rows = []
        for row in data.get("cash_dividends", []):
            local = wanted.get(str(row.symbol).upper())
            if local is None:
                continue
            div_rows.append((local, row.ex_date.isoformat(), float(row.rate)))
        for kind in ("forward_splits", "reverse_splits"):
            for row in data.get(kind, []):
                local = wanted.get(str(row.symbol).upper())
                if local is None:
                    continue
                split_rows.append(
                    (local, row.ex_date.isoformat(), kind, float(row.new_rate), float(row.old_rate))
                )
        conn.executemany(
            "INSERT OR REPLACE INTO cash_dividends (symbol, ex_date, rate) VALUES (?, ?, ?)",
            div_rows,
        )
        conn.executemany(
            "INSERT OR REPLACE INTO splits (symbol, ex_date, kind, new_rate, old_rate) VALUES (?, ?, ?, ?, ?)",
            split_rows,
        )
        conn.commit()
        print(f"actions {i + len(batch)}/{len(symbols)} divs={len(div_rows)} splits={len(split_rows)}", flush=True)
        time.sleep(0.25)


def spy_sessions(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT session FROM bars WHERE symbol = 'SPY' AND session <= ? ORDER BY session",
        (CUTOFF,),
    ).fetchall()
    return [r[0] for r in rows]


def main() -> int:
    key, secret = config.get_alpaca_credentials(paper=True)
    secrets = (key, secret)
    client = StockHistoricalDataClient(key, secret)
    actions = CorporateActionsClient(key, secret)
    conn = init_db(DB_PATH)
    # A 40-name request with limit 10000 kept only the first slice and marked the
    # rest as empty. Treat a name as done only when its tape reaches the cutoff.
    done = {
        sym
        for sym, n, mx in conn.execute("SELECT symbol, COUNT(1), MAX(session) FROM bars GROUP BY symbol")
        if mx is not None and mx >= CUTOFF and n >= 800
    }
    save_checkpoint(done)
    print("complete_tapes", len(done), flush=True)
    fetch_symbols(client, conn, ["SPY"], secrets, done)
    sessions = [d for d in spy_sessions(conn) if pd.Timestamp(d).weekday() < 5 and d <= CUTOFF]
    need = WARMUP + 2 * EVAL_SESSIONS
    if len(sessions) < need:
        print(f"SPY sessions {len(sessions)} below {need}", flush=True)
        return 1
    calendar = sessions[-need:]
    membership = load_sp500_membership()
    symbols = sp500_tickers_in_window(membership, pd.Timestamp(calendar[0]), pd.Timestamp(calendar[-1]))
    symbols = [s for s in symbols if s != "SPY"]
    print(f"calendar {calendar[0]} -> {calendar[-1]} sessions {len(calendar)} members {len(symbols)}", flush=True)
    failures = fetch_symbols(client, conn, symbols, secrets, done)
    fetch_actions(actions, conn, symbols, secrets)
    manifest = {
        "source": "Alpaca stock bars and corporate actions",
        "feed": "sip",
        "adjustment": "split",
        "dividend_policy": (
            "Cash dividend rate divided by later split factors (new/old) and credited once. "
            "Prices are not dividend-adjusted. Share count is not changed on split dates."
        ),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "request_start": "2023-01-01",
        "request_end_exclusive": "2026-10-15",
        "session_cutoff": CUTOFF,
        "calendar_sessions": len(calendar),
        "calendar_start": calendar[0],
        "calendar_end": calendar[-1],
        "prior_warmup_start": calendar[0],
        "symbols_requested": len(symbols) + 1,
        "bar_errors": failures,
        "credentials_printed": False,
        "market_data_db_written": False,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (DATA / "calendar.json").write_text(json.dumps(calendar), encoding="utf-8")
    conn.close()
    print("manifest", MANIFEST_PATH, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
