"""Separate daily OHLC store. Does not open market_data.db or the trading loop."""

from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from modules.nyse_calendar import is_finalized_session

_DATE_LEN = 10
_NY = ZoneInfo("America/New_York")

_CREATE = """
CREATE TABLE IF NOT EXISTS daily_ohlc (
    symbol TEXT NOT NULL,
    session_date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (symbol, session_date)
)
"""


def default_ohlc_path(root: Path | None = None) -> Path:
    base = root or Path(__file__).resolve().parents[1]
    return base / "data" / "daily_ohlc.sqlite"


def record_daily_bars(
    db_path: Path | str,
    bars: list[dict],
    *,
    as_of: date | datetime | None = None,
) -> dict[str, int]:
    """Store finalized sessions. A newer ``updated_at`` may replace that session."""
    path = Path(db_path)
    _refuse_close_matrix(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    today = _as_of_clock(as_of)
    inserted = replaced = unchanged = rejected = 0
    conn = sqlite3.connect(path)
    try:
        _ensure_schema(conn)
        for bar in bars:
            clean = _validate(bar, today)
            if clean is None:
                rejected += 1
                continue
            symbol, day, op, hi, lo, cl, source, updated_at = clean
            existing = conn.execute(
                "SELECT updated_at FROM daily_ohlc WHERE symbol = ? AND session_date = ?",
                (symbol, day),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO daily_ohlc
                        (symbol, session_date, open, high, low, close, source, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (symbol, day, op, hi, lo, cl, source, updated_at),
                )
                inserted += 1
                continue
            stored = _parse_updated(existing[0])
            incoming = _parse_updated(updated_at)
            if incoming is None or (stored is not None and incoming <= stored):
                unchanged += 1
                continue
            conn.execute(
                """
                UPDATE daily_ohlc
                SET open = ?, high = ?, low = ?, close = ?, source = ?, updated_at = ?
                WHERE symbol = ? AND session_date = ?
                """,
                (op, hi, lo, cl, source, updated_at, symbol, day),
            )
            replaced += 1
        conn.commit()
    finally:
        conn.close()
    return {
        "inserted": inserted,
        "replaced": replaced,
        "unchanged": unchanged,
        "duplicates": unchanged,
        "rejected": rejected,
    }


def load_daily_bars(db_path: Path | str, symbol: str) -> list[dict]:
    path = Path(db_path)
    if not path.is_file():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT symbol, session_date, open, high, low, close, source, updated_at
            FROM daily_ohlc
            WHERE symbol = ?
            ORDER BY session_date
            """,
            (str(symbol or "").strip().upper(),),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "symbol": sym,
            "session_date": day,
            "open": op,
            "high": hi,
            "low": lo,
            "close": cl,
            "source": source,
            "updated_at": updated,
        }
        for sym, day, op, hi, lo, cl, source, updated in rows
    ]


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(daily_ohlc)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE daily_ohlc ADD COLUMN source TEXT NOT NULL DEFAULT ''")
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE daily_ohlc ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")


def _refuse_close_matrix(path: Path) -> None:
    if path.name.lower() == "market_data.db":
        raise ValueError("daily OHLC uses its own file, not market_data.db")
    if not path.is_file():
        return
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
    finally:
        conn.close()
    if any(str(name).endswith("_daily") for name in names):
        raise ValueError("refusing to write OHLC into a close-matrix database")


def _as_of_clock(as_of: date | datetime | None) -> datetime:
    if as_of is None:
        return datetime.now(_NY)
    if isinstance(as_of, datetime):
        if as_of.tzinfo is None:
            return as_of.replace(tzinfo=_NY)
        return as_of.astimezone(_NY)
    return datetime(as_of.year, as_of.month, as_of.day, tzinfo=_NY)


def _parse_updated(raw) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_NY)
    return parsed


def _validate(bar: dict, as_of: datetime) -> tuple | None:
    try:
        symbol = str(bar.get("symbol") or "").strip().upper()
        day_text = str(bar.get("session_date") or "")[:_DATE_LEN]
        session = date.fromisoformat(day_text)
        op = float(bar["open"])
        hi = float(bar["high"])
        lo = float(bar["low"])
        cl = float(bar["close"])
        source = str(bar.get("source") or "").strip()
        updated_at = str(bar.get("updated_at") or "").strip()
    except (KeyError, TypeError, ValueError):
        return None
    if not symbol or any(ch in symbol for ch in " /\\"):
        return None
    if not is_finalized_session(session, as_of):
        return None
    if not source or _parse_updated(updated_at) is None:
        return None
    prices = (op, hi, lo, cl)
    if not all(math.isfinite(px) and px > 0 for px in prices):
        return None
    if hi < max(op, cl, lo) or lo > min(op, cl, hi):
        return None
    return symbol, session.isoformat(), op, hi, lo, cl, source, updated_at
