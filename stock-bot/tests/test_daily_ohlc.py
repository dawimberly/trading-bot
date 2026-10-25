"""Offline fixtures for the separate daily OHLC store."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from modules.daily_ohlc import load_daily_bars, record_daily_bars

_AS_OF = date(2026, 10, 13)


def _bar(**overrides):
    row = {
        "symbol": "AAPL",
        "session_date": "2026-10-09",
        "open": 100.0,
        "high": 105.0,
        "low": 99.0,
        "close": 102.0,
        "source": "fixture",
        "updated_at": "2026-10-09T21:00:00+00:00",
    }
    row.update(overrides)
    return row


def test_insert_leaves_same_timestamp_unchanged(tmp_path):
    path = tmp_path / "daily_ohlc.sqlite"
    first = record_daily_bars(
        path,
        [_bar(), _bar(symbol="MSFT", close=50, high=55, low=48, open=49)],
        as_of=_AS_OF,
    )
    assert first["inserted"] == 2
    assert first["unchanged"] == 0
    assert first["rejected"] == 0
    second = record_daily_bars(path, [_bar(close=103.0, high=106.0)], as_of=_AS_OF)
    assert second["inserted"] == 0
    assert second["unchanged"] == 1
    assert second["duplicates"] == 1
    assert second["replaced"] == 0
    rows = load_daily_bars(path, "AAPL")
    assert len(rows) == 1
    assert rows[0]["close"] == 102.0
    assert rows[0]["source"] == "fixture"
    assert rows[0]["updated_at"] == "2026-10-09T21:00:00+00:00"


def test_newer_authoritative_bar_replaces_corrected_ohlc(tmp_path):
    path = tmp_path / "daily_ohlc.sqlite"
    record_daily_bars(path, [_bar()], as_of=_AS_OF)
    result = record_daily_bars(
        path,
        [
            _bar(
                close=104.0,
                high=107.0,
                source="vendor",
                updated_at="2026-10-10T20:00:00+00:00",
            )
        ],
        as_of=_AS_OF,
    )
    assert result["replaced"] == 1
    row = load_daily_bars(path, "AAPL")[0]
    assert row["close"] == 104.0
    assert row["source"] == "vendor"
    older = record_daily_bars(
        path,
        [_bar(close=90.0, low=89.0, updated_at="2026-10-09T12:00:00+00:00")],
        as_of=_AS_OF,
    )
    assert older["unchanged"] == 1
    assert load_daily_bars(path, "AAPL")[0]["close"] == 104.0


def test_rejects_open_session_weekend_and_bad_ohlc(tmp_path):
    path = tmp_path / "daily_ohlc.sqlite"
    result = record_daily_bars(
        path,
        [
            _bar(),
            _bar(session_date="2026-10-13"),
            _bar(session_date="2026-10-10"),
            _bar(session_date="2026-10-16"),
            _bar(symbol="BAD", high=1.0, low=5.0, open=3.0, close=3.0),
            _bar(symbol="NOSRC", source=""),
        ],
        as_of=_AS_OF,
    )
    assert result["inserted"] == 1
    assert result["rejected"] == 5
    assert load_daily_bars(path, "BAD") == []
    assert load_daily_bars(path, "AAPL")[0]["close"] == 102.0


def test_rejects_inverted_range_and_keeps_good_row(tmp_path):
    path = tmp_path / "daily_ohlc.sqlite"
    result = record_daily_bars(
        path,
        [_bar(), _bar(symbol="BAD", high=1.0, low=5.0, open=3.0, close=3.0)],
        as_of=_AS_OF,
    )
    assert result["inserted"] == 1
    assert result["rejected"] == 1
    assert load_daily_bars(path, "BAD") == []


def test_refuses_market_data_filename(tmp_path):
    path = tmp_path / "market_data.db"
    path.write_bytes(b"")
    with pytest.raises(ValueError, match="market_data.db"):
        record_daily_bars(path, [_bar()], as_of=_AS_OF)


def test_refuses_database_that_already_has_daily_close_tables(tmp_path):
    path = tmp_path / "prices.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE AAPL_daily (Date TEXT, Close REAL)")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="close-matrix"):
        record_daily_bars(path, [_bar()], as_of=_AS_OF)


def test_legacy_table_accepts_a_corrected_bar(tmp_path):
    path = tmp_path / "daily_ohlc.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE daily_ohlc (
            symbol TEXT NOT NULL,
            session_date TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            PRIMARY KEY (symbol, session_date)
        )
        """
    )
    conn.execute(
        "INSERT INTO daily_ohlc VALUES ('AAPL', '2026-10-09', 100, 105, 99, 102)"
    )
    conn.commit()
    conn.close()
    result = record_daily_bars(
        path,
        [_bar(close=104.0, high=107.0, updated_at="2026-10-10T20:00:00+00:00")],
        as_of=_AS_OF,
    )
    assert result["replaced"] == 1
    row = load_daily_bars(path, "AAPL")[0]
    assert row["close"] == 104.0
    assert row["source"] == "fixture"


def test_holiday_is_rejected_and_early_close_session_is_kept(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    path = tmp_path / "daily_ohlc.sqlite"
    result = record_daily_bars(
        path,
        [
            _bar(session_date="2026-11-26"),
            _bar(session_date="2026-07-03"),
            _bar(session_date="2026-11-27", updated_at="2026-11-27T21:00:00+00:00"),
            _bar(session_date="2025-07-03", updated_at="2025-07-03T21:00:00+00:00"),
        ],
        as_of=date(2026, 12, 1),
    )
    assert result["rejected"] == 2
    assert result["inserted"] == 2
    assert {row["session_date"] for row in load_daily_bars(path, "AAPL")} == {
        "2025-07-03",
        "2026-11-27",
    }
    et = ZoneInfo("America/New_York")
    early = tmp_path / "early.sqlite"
    before = record_daily_bars(
        early,
        [_bar(session_date="2026-11-27", updated_at="2026-11-27T18:00:00+00:00")],
        as_of=datetime(2026, 11, 27, 12, 30, tzinfo=et),
    )
    assert before["rejected"] == 1
    after = record_daily_bars(
        early,
        [_bar(session_date="2026-11-27", updated_at="2026-11-27T18:00:00+00:00")],
        as_of=datetime(2026, 11, 27, 13, 1, tzinfo=et),
    )
    assert after["inserted"] == 1
    regular = tmp_path / "regular.sqlite"
    open_session = record_daily_bars(
        regular,
        [_bar(session_date="2026-10-09")],
        as_of=datetime(2026, 10, 9, 15, 0, tzinfo=et),
    )
    assert open_session["rejected"] == 1
    closed_session = record_daily_bars(
        regular,
        [_bar(session_date="2026-10-09")],
        as_of=datetime(2026, 10, 9, 16, 1, tzinfo=et),
    )
    assert closed_session["inserted"] == 1
