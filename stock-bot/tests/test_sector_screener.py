"""Tests for dynamic sector screener."""

from __future__ import annotations

import numpy as np
import pandas as pd

import config
from modules.sector_screener import (
    _max_active_sectors,
    compute_sector_strengths,
    get_active_sectors,
    get_expanded_universe,
)


def _trending_prices(n: int, drift: float) -> pd.Series:
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    vals = 100.0 * np.exp(np.linspace(0, drift, n))
    return pd.Series(vals, index=idx)


def test_compute_sector_strength_ranks_strong_etf_first():
    n = 260
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    spy = _trending_prices(n, 0.05)
    xlk = _trending_prices(n, 0.20)
    xle = _trending_prices(n, -0.05)
    data = pd.DataFrame({"SPY": spy, "XLK": xlk, "XLE": xle}, index=idx)

    rows = compute_sector_strengths(data)
    assert rows
    assert rows[0]["etf"] == "XLK"
    assert rows[0]["above_ma200"] is True
    assert rows[0]["rs_vs_spy"] > 0


def test_get_active_sectors_caps_at_max():
    n = 260
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    data = pd.DataFrame(
        {
            "SPY": _trending_prices(n, 0.05),
            "XLK": _trending_prices(n, 0.15),
            "XLE": _trending_prices(n, 0.12),
            "XLF": _trending_prices(n, 0.10),
            "XLV": _trending_prices(n, 0.08),
        },
        index=idx,
    )
    active = get_active_sectors(data)
    assert len(active) <= _max_active_sectors(data)


def test_max_active_sectors_expands_when_strength_high(monkeypatch):
    monkeypatch.setattr(config, "MAX_ACTIVE_SECTORS", 3)
    monkeypatch.setattr(config, "MAX_ACTIVE_SECTORS_STRONG", 4)
    monkeypatch.setattr(config, "SECTOR_STRONG_SCORE_MIN", 0.01)
    n = 260
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    data = pd.DataFrame(
        {
            "SPY": _trending_prices(n, 0.05),
            "XLK": _trending_prices(n, 0.20),
            "XLE": _trending_prices(n, 0.18),
            "XLF": _trending_prices(n, 0.16),
            "XLV": _trending_prices(n, 0.14),
            "XLI": _trending_prices(n, 0.12),
        },
        index=idx,
    )
    active = get_active_sectors(data)
    assert len(active) <= 4
    assert _max_active_sectors(data) == 4


def test_get_expanded_universe_respects_cap(monkeypatch):
    was_ctx = config.paper_aggressive_context()
    was_bt = config.backtest_paper_sleeves_context()
    config.set_paper_aggressive_context(True)
    config.set_backtest_paper_sleeves_context(True)
    monkeypatch.setattr(config, "DYNAMIC_SECTOR_SCREENER_ENABLED", True)
    monkeypatch.setattr(config, "BASE_UNIVERSE_SIZE", 10)
    monkeypatch.setattr(config, "SECTOR_EXPANSION_SIZE", 25)
    monkeypatch.setattr(config, "SECTOR_MAX_TOTAL_TICKERS", 120)

    n = 260
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    base_syms = [f"S{i}" for i in range(15)]
    cols = {"SPY": _trending_prices(n, 0.05), "XLK": _trending_prices(n, 0.20)}
    for sym in base_syms:
        cols[sym] = _trending_prices(n, 0.06)
    for sym in ("AAPL", "MSFT", "NVDA", "ORCL", "ADBE"):
        cols[sym] = _trending_prices(n, 0.18)
    data = pd.DataFrame(cols, index=idx)

    def fake_universe(data_columns):
        return [c for c in data_columns if c not in ("SPY", "XLK")]

    monkeypatch.setattr(config, "nyse_momentum_universe", fake_universe)

    expanded = get_expanded_universe(data.columns, data)
    assert len(expanded) <= 120
    assert len(expanded) >= 10

    config.set_paper_aggressive_context(was_ctx)
    config.set_backtest_paper_sleeves_context(was_bt)


def test_momentum_fallback_when_no_sectors_qualify(monkeypatch):
    was_ctx = config.paper_aggressive_context()
    was_bt = config.backtest_paper_sleeves_context()
    config.set_paper_aggressive_context(True)
    config.set_backtest_paper_sleeves_context(True)
    monkeypatch.setattr(config, "DYNAMIC_SECTOR_SCREENER_ENABLED", True)
    monkeypatch.setattr(config, "BASE_UNIVERSE_SIZE", 5)
    monkeypatch.setattr(config, "SECTOR_FALLBACK_MOMENTUM_COUNT", 8)

    n = 80
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    data = pd.DataFrame(
        {
            "SPY": _trending_prices(n, 0.02),
            "AAPL": _trending_prices(n, 0.12),
            "MSFT": _trending_prices(n, 0.10),
            "XOM": _trending_prices(n, 0.08),
        },
        index=idx,
    )

    def fake_universe(data_columns):
        return ["AAPL"]

    monkeypatch.setattr(config, "nyse_momentum_universe", fake_universe)

    expanded = get_expanded_universe(data.columns, data)
    assert len(expanded) > 1
    assert "MSFT" in expanded or "XOM" in expanded

    config.set_paper_aggressive_context(was_ctx)
    config.set_backtest_paper_sleeves_context(was_bt)


def test_sector_screener_off_when_not_paper_context(monkeypatch):
    was_ctx = config.paper_aggressive_context()
    was_bt = config.backtest_paper_sleeves_context()
    was_paper = config.PAPER_TRADING
    was_agg = config.PAPER_AGGRESSIVE_ENABLED
    config.set_paper_aggressive_context(False)
    config.set_backtest_paper_sleeves_context(False)
    monkeypatch.setattr(config, "DYNAMIC_SECTOR_SCREENER_ENABLED", True)
    monkeypatch.setattr(config, "PAPER_TRADING", False)
    monkeypatch.setattr(config, "PAPER_AGGRESSIVE_ENABLED", False)

    n = 60
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    data = pd.DataFrame({"SPY": _trending_prices(n, 0.05), "AAPL": _trending_prices(n, 0.1)}, index=idx)

    called = {"n": 0}

    def fake_universe(data_columns):
        called["n"] += 1
        return list(data_columns)

    monkeypatch.setattr(config, "nyse_momentum_universe", fake_universe)
    out = get_expanded_universe(data.columns, data)
    assert out == ["SPY", "AAPL"]
    assert called["n"] == 1
    config.set_paper_aggressive_context(was_ctx)
    config.set_backtest_paper_sleeves_context(was_bt)
    monkeypatch.setattr(config, "PAPER_TRADING", was_paper)
    monkeypatch.setattr(config, "PAPER_AGGRESSIVE_ENABLED", was_agg)
