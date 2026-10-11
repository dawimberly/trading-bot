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


def test_future_fill_is_counted_once_when_marker_is_blank():
    mod = _load()
    ts = datetime(2026, 10, 9, 11, 49, 59)
    df = _rows(
        {"event": "exit", "realized_pnl": "", "timestamp": ts},
        {
            "event": "trade_closed",
            "realized_pnl": "",
            "notes": "smart_atr_stop pnl_ledger=fill",
            "timestamp": ts,
        },
        {
            "event": "fill",
            "side": "sell",
            "realized_pnl": -122.39,
            "timestamp": ts,
        },
    )
    pnls, sleeves, _inf, directional = mod._closed_trade_stats(df, datetime(2026, 10, 1))
    assert directional == 0
    assert pnls == [-122.39]
    assert sleeves["nyse"].realized_trades == 1


def test_three_second_skew_still_pairs():
    mod = _load()
    df = _rows(
        {"event": "exit", "trade_id": "", "realized_pnl": "", "timestamp": datetime(2026, 10, 9, 11, 49, 59)},
        {
            "event": "trade_closed",
            "trade_id": "late-fill",
            "realized_pnl": -122.39,
            "notes": "smart_atr_stop",
            "timestamp": datetime(2026, 10, 9, 11, 50, 2),
        },
    )
    pnls, sleeves, _inf, directional = mod._closed_trade_stats(df, datetime(2026, 10, 1))
    assert directional == 0
    assert sleeves["nyse"].realized_trades == 1
    assert pnls == [-122.39]


def test_blank_trade_closed_keeps_percent_exit():
    mod = _load()
    ts = datetime(2026, 10, 9, 11, 49, 59)
    df = _rows(
        {"event": "exit", "trade_id": "", "realized_pnl": "", "notes": "smart_atr_stop -1.84%", "timestamp": ts},
        {"event": "trade_closed", "trade_id": "no-lot", "realized_pnl": "", "notes": "smart_atr_stop", "timestamp": ts},
    )
    pnls, sleeves, _inf, directional = mod._closed_trade_stats(df, datetime(2026, 10, 1))
    assert pnls == []
    assert directional == 1
    assert sleeves["nyse"].realized_trades == 1
    assert sleeves["nyse"].realized_wins == 0


def test_one_second_gap_blank_ids_counts_fill_once():
    mod = _load()
    fill_ts = datetime(2026, 10, 9, 11, 49, 59)
    mark_ts = datetime(2026, 10, 9, 11, 50, 0)
    df = _rows(
        {
            "event": "exit",
            "trade_id": "",
            "realized_pnl": "",
            "notes": "smart_atr_stop -1.84%",
            "timestamp": fill_ts,
        },
        {
            "event": "trade_closed",
            "trade_id": "",
            "realized_pnl": "",
            "notes": "smart_atr_stop pnl_ledger=fill",
            "timestamp": mark_ts,
        },
        {
            "event": "fill",
            "side": "sell",
            "trade_id": "",
            "order_id": "fill-1",
            "realized_pnl": -10.0,
            "timestamp": fill_ts,
        },
    )
    pnls, sleeves, _inf, directional = mod._closed_trade_stats(df, datetime(2026, 10, 1))
    assert directional == 0
    assert pnls == [-10.0]
    assert sleeves["nyse"].realized_trades == 1


def test_differing_trade_ids_within_ten_seconds_count_once():
    mod = _load()
    df = _rows(
        {
            "event": "trade_closed",
            "trade_id": "marker-id",
            "realized_pnl": -122.39,
            "timestamp": datetime(2026, 10, 9, 11, 49, 59),
        },
        {
            "event": "fill",
            "side": "sell",
            "trade_id": "fill-id",
            "order_id": "fill-2",
            "realized_pnl": -122.39,
            "timestamp": datetime(2026, 10, 9, 11, 50, 0),
        },
    )
    pnls, sleeves, _inf, _directional = mod._closed_trade_stats(df, datetime(2026, 10, 1))
    assert pnls == [-122.39]
    assert sleeves["nyse"].realized_trades == 1


def test_same_trade_id_outside_ten_seconds_counts_once():
    mod = _load()
    df = _rows(
        {
            "event": "trade_closed",
            "trade_id": "same-id",
            "realized_pnl": -5.0,
            "timestamp": datetime(2026, 10, 9, 11, 49, 59),
        },
        {
            "event": "fill",
            "side": "sell",
            "trade_id": "same-id",
            "order_id": "fill-3",
            "realized_pnl": -5.0,
            "timestamp": datetime(2026, 10, 9, 11, 50, 40),
        },
    )
    pnls, sleeves, _inf, _directional = mod._closed_trade_stats(df, datetime(2026, 10, 1))
    assert pnls == [-5.0]
    assert sleeves["nyse"].realized_trades == 1


def test_same_order_qty_price_without_execution_id_is_uncertain():
    mod = _load()
    df = _rows(
        {
            "event": "fill",
            "side": "sell",
            "trade_id": "",
            "order_id": "dup-1",
            "qty": "2",
            "price": "10",
            "realized_pnl": -3.0,
            "timestamp": datetime(2026, 10, 9, 11, 49, 59),
        },
        {
            "event": "fill",
            "side": "sell",
            "trade_id": "",
            "order_id": "dup-1",
            "qty": "2",
            "price": "10",
            "realized_pnl": -3.0,
            "timestamp": datetime(2026, 10, 9, 11, 50, 0),
        },
    )
    report = mod.reconcile_realized(df)
    assert report["duplicate_fill_count"] == 0
    assert report["raw_fill_total"] == -6.0
    assert report["deduplicated_fill_total"] == -6.0
    assert report["confident_total"] == 0.0
    assert report["unattributed_total"] == -6.0
    assert [row["reason"] for row in report["ambiguous_records"]] == [
        "indistinguishable_executions",
        "indistinguishable_executions",
    ]


def test_same_execution_id_counts_once():
    mod = _load()
    df = _rows(
        {
            "event": "fill",
            "side": "sell",
            "trade_id": "",
            "order_id": "ord-1",
            "execution_id": "exec-1",
            "qty": "2",
            "price": "10",
            "realized_pnl": -3.0,
            "timestamp": datetime(2026, 10, 9, 11, 49, 59),
        },
        {
            "event": "fill",
            "side": "sell",
            "trade_id": "",
            "order_id": "ord-1",
            "execution_id": "exec-1",
            "qty": "2",
            "price": "10",
            "realized_pnl": -3.0,
            "timestamp": datetime(2026, 10, 9, 11, 50, 0),
        },
    )
    report = mod.reconcile_realized(df)
    assert report["duplicate_fill_count"] == 1
    assert report["confident_total"] == -3.0
    assert report["unattributed_total"] == 0.0
    assert report["ambiguous_records"] == []


def test_distinct_partial_fills_both_count():
    mod = _load()
    df = _rows(
        {
            "event": "fill",
            "side": "sell",
            "trade_id": "slice-a",
            "order_id": "ord-a",
            "realized_pnl": 1.5,
            "timestamp": datetime(2026, 10, 9, 11, 49, 59),
        },
        {
            "event": "fill",
            "side": "sell",
            "trade_id": "slice-b",
            "order_id": "ord-b",
            "realized_pnl": 2.5,
            "timestamp": datetime(2026, 10, 9, 11, 50, 1),
        },
    )
    pnls, sleeves, _inf, _directional = mod._closed_trade_stats(df, datetime(2026, 10, 1))
    assert sorted(pnls) == [1.5, 2.5]
    assert sleeves["nyse"].realized_trades == 2


def test_same_symbol_fills_inside_ten_seconds_stay_separate():
    mod = _load()
    df = _rows(
        {
            "event": "fill",
            "side": "sell",
            "trade_id": "slice-a",
            "order_id": "ord-a",
            "qty": "1",
            "price": "10",
            "realized_pnl": 1.5,
            "timestamp": datetime(2026, 10, 9, 11, 49, 59),
        },
        {
            "event": "fill",
            "side": "sell",
            "trade_id": "slice-b",
            "order_id": "ord-b",
            "qty": "2",
            "price": "11",
            "realized_pnl": 2.5,
            "timestamp": datetime(2026, 10, 9, 11, 50, 4),
        },
        {
            "event": "trade_closed",
            "trade_id": "other-close",
            "order_id": "ord-z",
            "realized_pnl": 9.0,
            "timestamp": datetime(2026, 10, 9, 11, 50, 6),
        },
    )
    report = mod.reconcile_realized(df)
    assert sorted(report["confident_frame"]["realized_pnl"].tolist()) == [1.5, 2.5]
    assert report["unattributed_total"] == 9.0
    assert any(row["reason"] == "timestamp_without_identity" for row in report["ambiguous_records"])


def test_blank_order_id_partials_are_preserved():
    mod = _load()
    df = _rows(
        {
            "event": "fill",
            "side": "sell",
            "trade_id": "",
            "order_id": "",
            "qty": "1",
            "price": "10",
            "realized_pnl": 1.25,
            "timestamp": datetime(2026, 10, 9, 11, 49, 59),
        },
        {
            "event": "fill",
            "side": "sell",
            "trade_id": "",
            "order_id": "",
            "qty": "3",
            "price": "10.5",
            "realized_pnl": 4.5,
            "timestamp": datetime(2026, 10, 9, 11, 50, 2),
        },
    )
    report = mod.reconcile_realized(df)
    assert report["raw_fill_total"] == 5.75
    assert report["deduplicated_fill_total"] == 5.75
    assert report["confident_total"] == 5.75
    assert report["ambiguous_records"] == []


def test_identical_blank_fills_are_flagged_not_discarded():
    mod = _load()
    df = _rows(
        {
            "event": "fill",
            "side": "sell",
            "trade_id": "",
            "order_id": "",
            "qty": "2",
            "price": "10",
            "realized_pnl": -3.0,
            "timestamp": datetime(2026, 10, 9, 11, 49, 59),
        },
        {
            "event": "fill",
            "side": "sell",
            "trade_id": "",
            "order_id": "",
            "qty": "2",
            "price": "10",
            "realized_pnl": -3.0,
            "timestamp": datetime(2026, 10, 9, 11, 50, 0),
        },
    )
    report = mod.reconcile_realized(df)
    assert report["raw_fill_total"] == -6.0
    assert report["confident_total"] == 0.0
    assert report["unattributed_total"] == -6.0
    assert len(report["ambiguous_records"]) == 2
    assert {row["reason"] for row in report["ambiguous_records"]} == {"identical_blank_fill"}


def test_same_order_partials_with_different_qty_both_count():
    mod = _load()
    df = _rows(
        {
            "event": "fill",
            "side": "sell",
            "order_id": "same-order",
            "qty": "1",
            "price": "10",
            "realized_pnl": 1.0,
            "timestamp": datetime(2026, 10, 9, 11, 49, 59),
        },
        {
            "event": "fill",
            "side": "sell",
            "order_id": "same-order",
            "qty": "2",
            "price": "10.5",
            "realized_pnl": 2.0,
            "timestamp": datetime(2026, 10, 9, 11, 50, 1),
        },
    )
    report = mod.reconcile_realized(df)
    assert report["duplicate_fill_count"] == 0
    assert report["confident_total"] == 3.0


def test_historical_marker_with_same_order_is_not_added_twice():
    mod = _load()
    df = _rows(
        {
            "event": "fill",
            "side": "sell",
            "order_id": "ord-hist",
            "trade_id": "t-hist",
            "qty": "4",
            "price": "20",
            "realized_pnl": -122.39,
            "timestamp": datetime(2026, 10, 9, 11, 49, 59),
        },
        {
            "event": "trade_closed",
            "order_id": "ord-hist",
            "trade_id": "t-hist",
            "qty": "4",
            "price": "20",
            "realized_pnl": -122.39,
            "timestamp": datetime(2026, 10, 9, 11, 50, 8),
        },
    )
    report = mod.reconcile_realized(df)
    assert report["raw_fill_total"] == -122.39
    assert report["historical_close_marker_total"] == -122.39
    assert report["suppressed_marker_total"] == -122.39
    assert report["confident_total"] == -122.39
    assert report["unattributed_total"] == 0.0



def test_historical_fill_is_not_added_to_dollar_trade_closed():
    mod = _load()
    ts = datetime(2026, 10, 9, 11, 49, 59)
    df = _rows(
        {"event": "trade_closed", "realized_pnl": -122.39, "timestamp": ts},
        {
            "event": "fill",
            "side": "sell",
            "realized_pnl": -122.39,
            "timestamp": ts,
        },
    )
    pnls, sleeves, _inf, _directional = mod._closed_trade_stats(df, datetime(2026, 10, 1))
    assert pnls == [-122.39]
    assert sleeves["nyse"].realized_trades == 1
