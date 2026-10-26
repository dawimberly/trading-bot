"""Fill poller accepts Alpaca orders and DustCloseResult."""

from __future__ import annotations

import json
from types import SimpleNamespace

from modules.alpaca_executor import AlpacaExecutor
from modules.dust_cleanup import DustCloseResult
from modules.trade_journal import log_fill, sum_realized_pnl


def _executor():
    executor = AlpacaExecutor.__new__(AlpacaExecutor)
    executor._notified_order_ids = set()
    executor._order_notify_ctx = {}
    executor._emit_fill_notification = lambda *args, **kwargs: None
    return executor


def test_filled_alpaca_order_uses_id_and_does_not_poll():
    executor = _executor()
    executor.client = SimpleNamespace(
        get_order_by_id=lambda oid: (_ for _ in ()).throw(AssertionError(oid))
    )
    order = SimpleNamespace(
        id="alpaca-1",
        filled_qty="2",
        filled_avg_price="10.5",
        status="filled",
        symbol="AAPL",
    )
    details = executor.order_fill_details(order, max_wait=5)
    assert details["filled"] is True
    assert details["qty"] == 2.0
    assert details["partial"] is False


def test_dust_result_polls_order_id():
    executor = _executor()
    seen = {}

    def get_order_by_id(oid):
        seen["oid"] = oid
        return SimpleNamespace(
            id=oid,
            filled_qty="0.01",
            filled_avg_price="20",
            status="filled",
            symbol="XYZ",
        )

    executor._api = lambda name, fn, *args, **kwargs: fn(*args, **kwargs)
    executor.client = SimpleNamespace(get_order_by_id=get_order_by_id)
    dust = DustCloseResult("XYZ", 0.01, 0.2, "closed", "close_position", order_id="dust-9")
    details = executor.order_fill_details(dust, max_wait=0)
    assert details is None
    details = executor.order_fill_details(dust, max_wait=0.1)
    assert seen["oid"] == "dust-9"
    assert details["filled"] is True
    assert details["qty"] == 0.01


def test_rejected_order_is_not_a_fill():
    executor = _executor()
    executor._api = lambda name, fn, *args, **kwargs: fn(*args, **kwargs)
    executor.client = SimpleNamespace(
        get_order_by_id=lambda oid: SimpleNamespace(
            id=oid,
            filled_qty="0",
            filled_avg_price=None,
            status="rejected",
            symbol="GOLD",
        )
    )
    order = SimpleNamespace(id="rej-1", filled_qty="0", status="rejected", symbol="GOLD")
    assert executor.order_fill_details(order, max_wait=0.1) is None


def test_dust_result_without_order_id_does_not_raise():
    executor = _executor()
    dust = DustCloseResult("XYZ", 0.01, 0.2, "error", "rejected", order_id=None)
    assert executor.order_fill_details(dust, max_wait=5) is None


def test_partial_slice_is_not_closed_and_pnl_is_counted_once(tmp_path, monkeypatch):
    path = tmp_path / "paper_journal.csv"
    monkeypatch.setattr("config.PAPER_JOURNAL_CSV", str(path), raising=False)
    monkeypatch.setattr(
        "modules.trade_journal._OPEN_TRADES_PATH",
        tmp_path / "open_trade_ids.json",
        raising=False,
    )
    log_fill("GOLD", "buy", qty=10, price=40, order_id="buy-1", journal_path=str(path))
    log_fill(
        "GOLD",
        "sell",
        qty=4,
        price=41,
        reason="concentration_guard_trim",
        order_id="sell-1",
        is_partial="0",
        broker_qty_remaining=6,
        broker_qty_source="confirmed",
        order_status="filled",
        journal_path=str(path),
        atr=1.25,
        atr_asof="at_exit",
        stop_price=37.5,
    )
    import csv

    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    sells = [r for r in rows if r["event"] == "fill" and r["side"] == "sell"]
    closed = [r for r in rows if r["event"] == "trade_closed"]
    assert len(sells) == 1
    assert sells[0]["is_partial"] == "1"
    assert sells[0]["realized_pnl"] == "4.0"
    assert closed == []
    assert sum_realized_pnl(rows) == 4.0
    meta = json.loads((tmp_path / "exit_metadata.jsonl").read_text(encoding="utf-8"))
    assert meta["atr"] == "1.25"
    assert meta["atr_asof"] == "at_exit"
    assert meta["stop_price"] == "37.5"
    assert meta["exit_price"] == "41"
    assert meta["entry_price"] == "40"
    assert meta["exit_state"] == "partial"
    assert meta["is_partial"] is True
    assert meta["exit_complete"] is False
    assert meta["summable"] is True
