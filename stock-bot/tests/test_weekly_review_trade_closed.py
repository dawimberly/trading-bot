"""Saturday review prefers trade_closed dollars over paired percent-only exits."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "analysis"))

import weekly_review as weekly_review  # noqa: E402


def _load():
    return weekly_review


def _rows(*events: dict) -> pd.DataFrame:
    base = {
        "timestamp": datetime(2026, 10, 9, 11, 49, 59),
        "event": "exit",
        "symbol": "CRGY",
        "sleeve": "NYSE",
        "notes": "smart_atr_stop -1.84% (2.0x)",
        "realized_pnl": "",
        "trade_id": "t-crgy",
    }
    out = []
    for ev in events:
        row = dict(base)
        row.update(ev)
        out.append(row)
    return pd.DataFrame(out)


def test_paired_exit_yields_to_trade_closed_dollars():
    mod = _load()
    df = _rows(
        {"event": "exit", "realized_pnl": ""},
        {"event": "trade_closed", "realized_pnl": -122.39, "notes": "smart_atr_stop"},
    )
    cutoff = datetime(2026, 10, 1)
    pnls, sleeves, _inf, directional = mod._closed_trade_stats(df, cutoff)
    assert directional == 0
    assert pnls == [-122.39]
    assert sleeves["nyse"].realized_trades == 1
    assert sleeves["nyse"].realized_pnl == -122.39
    assert sleeves["nyse"].realized_wins == 0


def test_unpaired_percent_exit_still_counts_direction():
    mod = _load()
    df = _rows({"event": "exit", "realized_pnl": "", "trade_id": ""})
    cutoff = datetime(2026, 10, 1)
    pnls, sleeves, _inf, directional = mod._closed_trade_stats(df, cutoff)
    assert pnls == []
    assert directional == 1
    assert sleeves["nyse"].realized_trades == 1
    assert sleeves["nyse"].realized_pnl == 0.0
    assert sleeves["nyse"].realized_wins == 0


def test_blank_exit_trade_id_still_pairs_on_timestamp():
    """Real Medium journal: exit has no trade_id, trade_closed does."""
    mod = _load()
    ts = datetime(2026, 10, 9, 11, 49, 59)
    df = _rows(
        {"event": "exit", "trade_id": "", "realized_pnl": "", "timestamp": ts},
        {
            "event": "trade_closed",
            "trade_id": "4b2b5a70-filled",
            "realized_pnl": -122.39,
            "notes": "smart_atr_stop",
            "timestamp": ts,
        },
    )
    pnls, sleeves, _inf, directional = mod._closed_trade_stats(df, datetime(2026, 10, 1))
    assert directional == 0
    assert sleeves["nyse"].realized_trades == 1
    assert pnls == [-122.39]


def test_unrelated_exit_is_not_dropped():
    mod = _load()
    df = _rows(
        {"event": "trade_closed", "symbol": "CRGY", "realized_pnl": -122.39, "trade_id": "t-crgy"},
        {
            "event": "exit",
            "symbol": "PBF",
            "trade_id": "",
            "realized_pnl": "",
            "notes": "trailing_stop 11.99%",
            "timestamp": datetime(2026, 10, 9, 13, 47, 28),
        },
    )
    selected = mod.select_closed_rows(df)
    assert set(selected["event"].astype(str)) == {"trade_closed", "exit"}
