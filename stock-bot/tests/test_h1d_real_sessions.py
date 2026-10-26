"""Regression: the SPY 200-SMA must use real NYSE sessions only."""
from datetime import date, timedelta

import pytest

import modules.h1d_trend_mix as m


def _sessions(start, count):
    out, d = [], start
    while len(out) < count:
        if m.is_nyse_session(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def test_is_nyse_session_rejects_weekends_and_holidays():
    assert m.is_nyse_session(date(2026, 10, 23))
    assert not m.is_nyse_session(date(2026, 10, 24))  # Saturday
    assert not m.is_nyse_session(date(2026, 10, 25))  # Sunday
    assert not m.is_nyse_session(date(2025, 12, 25))  # Christmas


def test_weekend_padding_is_dropped_and_sma_is_last_200_sessions():
    days = _sessions(date(2025, 6, 2), 260)
    real = [(d, 100.0 + i) for i, d in enumerate(days)]
    padded = list(real)
    for d, c in real:  # forward-filled weekend rows with junk values
        if d.weekday() == 4:
            padded.append((d + timedelta(days=1), 1.0))
            padded.append((d + timedelta(days=2), 1.0))
    padded.append((date(2025, 12, 25), 1.0))
    session = days[-1] + timedelta(days=1)
    while not m.is_nyse_session(session):
        session += timedelta(days=1)
    closes = m.completed_closes(m.real_session_bars(padded), session_date=session)
    assert closes == [c for _, c in real]
    window = closes[-200:]
    assert sum(window) / 200 == pytest.approx(sum(c for _, c in real[-200:]) / 200)


def test_load_spy_bars_returns_only_sessions():
    bars = m._load_spy_bars()
    if not bars:
        pytest.skip("no local SPY daily history")
    assert all(m.is_nyse_session(d) for d, _ in bars)
    assert len(bars) <= m.SPY_BAR_LOOKBACK


def test_regression_current_spy_data_sma_and_signal():
    bars = m._load_spy_bars()
    if not bars or max(d for d, _ in bars) < date(2026, 10, 23):
        pytest.skip("local SPY history does not cover 2026-10-23")
    bars = [(d, c) for d, c in bars if d <= date(2026, 10, 23)]
    closes = m.completed_closes(bars, session_date=date(2026, 10, 26))
    sma = sum(closes[-200:]) / 200
    # Adjusted closes: Yahoo adjusted 200-SMA is 724.00 (raw 727.01).
    assert 718 < sma < 733
    assert m.prior_close_signal(closes) is True
