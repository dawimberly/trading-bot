"""Exit state is partial, complete, or unknown. Unknown writes no trade_closed."""

from __future__ import annotations

import csv
import json

from modules.trade_journal import log_fill


def _isolate(tmp_path, monkeypatch):
    path = tmp_path / "paper_journal.csv"
    monkeypatch.setattr("config.PAPER_JOURNAL_CSV", str(path), raising=False)
    monkeypatch.setattr(
        "modules.trade_journal._OPEN_TRADES_PATH",
        tmp_path / "open_trade_ids.json",
        raising=False,
    )
    return path


def _rows(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _meta(tmp_path):
    return json.loads((tmp_path / "exit_metadata.jsonl").read_text(encoding="utf-8"))


def test_missing_lot_is_unknown_and_keeps_passed_slice_pnl(tmp_path, monkeypatch):
    path = _isolate(tmp_path, monkeypatch)
    log_fill(
        "GOLD",
        "sell",
        qty=4,
        price=41,
        entry_price=40,
        order_id="sell-missing",
        order_status="filled",
        broker_qty_remaining=0,
        broker_qty_source="confirmed",
        is_partial="0",
        journal_path=str(path),
    )
    rows = _rows(path)
    sells = [r for r in rows if r["event"] == "fill"]
    assert sells[0]["is_partial"] == "unknown"
    assert sells[0]["realized_pnl"] == "4.0"
    assert [r for r in rows if r["event"] == "trade_closed"] == []
    meta = _meta(tmp_path)
    assert meta["exit_state"] == "unknown"
    assert meta["exit_complete"] is None


def test_missing_entry_does_not_invent_a_basis(tmp_path, monkeypatch):
    path = _isolate(tmp_path, monkeypatch)
    log_fill(
        "GOLD",
        "sell",
        qty=4,
        price=41,
        order_id="sell-no-entry",
        order_status="filled",
        broker_qty_remaining=0,
        broker_qty_source="confirmed",
        journal_path=str(path),
    )
    sells = [r for r in _rows(path) if r["event"] == "fill"]
    assert sells[0]["realized_pnl"] == ""
    assert sells[0]["entry_price"] == ""
    assert sells[0]["is_partial"] == "unknown"


def test_conflicting_broker_quantity_is_unknown(tmp_path, monkeypatch):
    path = _isolate(tmp_path, monkeypatch)
    log_fill("GOLD", "buy", qty=10, price=40, order_id="buy-1", journal_path=str(path))
    log_fill(
        "GOLD",
        "sell",
        qty=4,
        price=41,
        order_id="sell-conflict",
        order_status="filled",
        broker_qty_remaining=0,
        broker_qty_source="confirmed",
        is_partial="0",
        journal_path=str(path),
    )
    rows = _rows(path)
    sell = next(r for r in rows if r["event"] == "fill" and r["side"] == "sell")
    assert sell["is_partial"] == "unknown"
    assert sell["realized_pnl"] == "4.0"
    assert [r for r in rows if r["event"] == "trade_closed"] == []
    assert _meta(tmp_path)["exit_complete"] is None


def test_rejected_order_is_not_complete(tmp_path, monkeypatch):
    path = _isolate(tmp_path, monkeypatch)
    log_fill("GOLD", "buy", qty=2, price=40, order_id="buy-2", journal_path=str(path))
    log_fill(
        "GOLD",
        "sell",
        qty=2,
        price=39,
        order_id="sell-rejected",
        order_status="rejected",
        broker_qty_remaining=0,
        broker_qty_source="confirmed",
        is_partial="0",
        journal_path=str(path),
    )
    rows = _rows(path)
    sell = next(r for r in rows if r["event"] == "fill" and r["side"] == "sell")
    assert sell["is_partial"] == "unknown"
    assert sell["realized_pnl"] == "-2.0"
    assert [r for r in rows if r["event"] == "trade_closed"] == []


def test_unconfirmed_order_is_not_complete(tmp_path, monkeypatch):
    path = _isolate(tmp_path, monkeypatch)
    log_fill("GOLD", "buy", qty=2, price=40, order_id="buy-3", journal_path=str(path))
    log_fill(
        "GOLD",
        "sell",
        qty=2,
        price=42,
        order_id="sell-new",
        order_status="new",
        broker_qty_remaining=0,
        broker_qty_source="confirmed",
        is_partial="0",
        journal_path=str(path),
    )
    rows = _rows(path)
    sell = next(r for r in rows if r["event"] == "fill" and r["side"] == "sell")
    assert sell["is_partial"] == "unknown"
    assert [r for r in rows if r["event"] == "trade_closed"] == []


def test_shrink_dust_still_counts_as_a_flat_exit(tmp_path, monkeypatch):
    path = _isolate(tmp_path, monkeypatch)
    log_fill("GOLD", "buy", qty=10, price=40, order_id="buy-dust", journal_path=str(path))
    sold = 10 * (1 - 1e-9)
    log_fill(
        "GOLD",
        "sell",
        qty=sold,
        price=41,
        order_id="sell-dust",
        order_status="filled",
        broker_qty_remaining=10 - sold,
        broker_qty_source="confirmed",
        is_partial="0",
        journal_path=str(path),
    )
    rows = _rows(path)
    sell = next(r for r in rows if r["event"] == "fill" and r["side"] == "sell")
    assert sell["is_partial"] == "0"
    assert len([r for r in rows if r["event"] == "trade_closed"]) == 1
    assert _meta(tmp_path)["exit_state"] == "complete"


def test_agreeing_flat_fill_is_complete_with_blank_marker(tmp_path, monkeypatch):
    path = _isolate(tmp_path, monkeypatch)
    log_fill("GOLD", "buy", qty=2, price=40, order_id="buy-4", journal_path=str(path))
    log_fill(
        "GOLD",
        "sell",
        qty=2,
        price=42,
        order_id="sell-done",
        order_status="filled",
        broker_qty_remaining=0,
        broker_qty_source="confirmed",
        is_partial="0",
        journal_path=str(path),
    )
    rows = _rows(path)
    sell = next(r for r in rows if r["event"] == "fill" and r["side"] == "sell")
    closed = [r for r in rows if r["event"] == "trade_closed"]
    assert sell["is_partial"] == "0"
    assert sell["realized_pnl"] == "4.0"
    assert len(closed) == 1
    assert closed[0]["realized_pnl"] == ""
    assert "pnl_ledger=fill" in closed[0]["notes"]
    meta = _meta(tmp_path)
    assert meta["exit_state"] == "complete"
    assert meta["exit_complete"] is True
    assert meta["broker_qty_source"] == "confirmed"
    assert meta["broker_qty_remaining"] == 0.0
    assert meta["estimated_qty_remaining"] is None


def test_estimated_quantity_is_not_broker_confirmation(tmp_path, monkeypatch):
    path = _isolate(tmp_path, monkeypatch)
    log_fill("GOLD", "buy", qty=10, price=40, order_id="buy-est", journal_path=str(path))
    log_fill(
        "GOLD",
        "sell",
        qty=10,
        price=41,
        order_id="sell-est",
        order_status="filled",
        broker_qty_source="estimated",
        estimated_qty_remaining=0,
        is_partial="0",
        journal_path=str(path),
    )
    sell = next(r for r in _rows(path) if r["event"] == "fill" and r["side"] == "sell")
    assert sell["is_partial"] == "unknown"
    assert sell["realized_pnl"] == "10.0"
    assert [r for r in _rows(path) if r["event"] == "trade_closed"] == []
    meta = _meta(tmp_path)
    assert meta["broker_qty_source"] == "estimated"
    assert meta["broker_qty_remaining"] is None
    assert meta["estimated_qty_remaining"] == 0.0
    assert meta["exit_complete"] is None


def test_stale_cached_overlap_stays_unknown(tmp_path, monkeypatch):
    path = _isolate(tmp_path, monkeypatch)
    log_fill("GOLD", "buy", qty=10, price=40, order_id="buy-stale", journal_path=str(path))
    for oid, qty in (("sell-a", 4), ("sell-b", 4)):
        log_fill(
            "GOLD",
            "sell",
            qty=qty,
            price=41,
            order_id=oid,
            order_status="filled",
            broker_qty_source="estimated",
            estimated_qty_remaining=6,
            journal_path=str(path),
        )
    sells = [r for r in _rows(path) if r["event"] == "fill" and r["side"] == "sell"]
    assert [r["is_partial"] for r in sells] == ["unknown", "unknown"]
    assert [r for r in _rows(path) if r["event"] == "trade_closed"] == []
    assert sells[0]["realized_pnl"] == "4.0"
    assert sells[1]["realized_pnl"] == "4.0"


def test_executor_labels_cached_qty_as_estimated(monkeypatch):
    from types import SimpleNamespace

    from modules.alpaca_executor import AlpacaExecutor

    captured = {}

    def _capture(*_args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("modules.trade_journal.log_fill", _capture)
    executor = AlpacaExecutor.__new__(AlpacaExecutor)
    executor.paper = True
    executor._account = SimpleNamespace(cash=1)
    executor._journal_fill(
        symbol="GOLD",
        side="sell",
        qty=4,
        price=41,
        notional=164,
        order_id="ord-1",
        partial=True,
        ctx={
            "entry_price": 40,
            "position_qty_before": 10,
            "order_status": "filled",
        },
        equity_after=100,
    )
    assert captured["broker_qty_source"] == "estimated"
    assert captured["estimated_qty_remaining"] == 6
    assert captured["broker_qty_remaining"] is None


def test_unknown_partial_flag_stays_text_for_readers():
    import pandas as pd

    from modules.csv_utils import coerce_trade_journal_df
    from modules.trade_journal import _full_exit

    assert _full_exit("unknown") is False
    assert _full_exit("0") is True
    frame = coerce_trade_journal_df(
        pd.DataFrame([{"event": "fill", "symbol": "GOLD", "is_partial": "unknown", "realized_pnl": "1.5"}])
    )
    assert frame.loc[0, "is_partial"] == "unknown"

