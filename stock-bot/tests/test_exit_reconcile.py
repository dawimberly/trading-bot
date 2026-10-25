"""Exit reconciliation: held shares, dust, non-fractionable, and error notes.

Run: python tests/test_exit_reconcile.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import TimeInForce

import config
from modules.alpaca_executor import AlpacaExecutor
from modules.dust_cleanup import DustCloseResult, _close_via_qty_order, close_dust_position
from modules.exit_reconcile import (
    ExitRetryBook,
    broker_error_note,
    plan_exit_submission,
)
from modules.position_exits import run_position_exits


class Broker:
    def __init__(self, pos, *, fractionable=True, orders=None, submit_exc=None):
        self.pos = pos
        self.asset = SimpleNamespace(fractionable=fractionable, tradable=True)
        self.orders = list(orders or [])
        self.submit_exc = submit_exc
        self.submitted = []
        self.cancelled = []
        self.order_reads = 0
        self.submits = 0

    def get_account(self):
        return SimpleNamespace(equity=100000.0, cash=50000.0)

    def get_all_positions(self):
        if self.cancelled and self.pos is not None:
            self.pos.qty_available = self.pos.qty
        return [] if self.pos is None else [self.pos]

    def get_orders(self, filter=None):
        self.order_reads += 1
        if self.cancelled:
            return []
        return list(self.orders)

    def get_asset(self, symbol):
        return self.asset

    def submit_order(self, order_data=None):
        self.submits += 1
        if self.submit_exc is not None:
            raise self.submit_exc
        self.submitted.append(order_data)
        return SimpleNamespace(
            id="ord-1",
            filled_qty=0,
            status="new",
            symbol=getattr(order_data, "symbol", None),
            qty=getattr(order_data, "qty", None),
        )

    def cancel_order_by_id(self, oid):
        self.cancelled.append(oid)

    def get_order_by_id(self, oid):
        return SimpleNamespace(id=oid, filled_qty=0, status="new", symbol="SPCX")


class Pos:
    def __init__(self, qty, price, available=None, entry=None):
        self.symbol = "SPCX"
        self.qty = qty
        self.qty_available = qty if available is None else available
        self.current_price = price
        self.avg_entry_price = entry if entry is not None else price
        self.market_value = abs(qty) * price
        self.unrealized_plpc = 0.0
        self.created_at = None


class BlankHeld(Exception):
    """Broker rejection whose str() is empty but whose message is not."""

    def __init__(self):
        super().__init__("")
        self.message = (
            "insufficient qty available for order "
            "(requested: 10, available: 0, held_for_orders: 10)"
        )
        self.status_code = 403

    def __str__(self):
        return ""


class Journal:
    def __init__(self):
        self.events = []

    def log_event(self, event, **kwargs):
        self.events.append((event, kwargs))

    def log_exit(self, *args, **kwargs):
        self.events.append(("exit", kwargs))


class Risk:
    def _log_event(self, message):
        return None


def _executor(broker: Broker) -> AlpacaExecutor:
    ex = AlpacaExecutor.__new__(AlpacaExecutor)
    ex.client = broker
    ex.paper = True
    ex.dry_run = False
    ex._equity_session_open = True
    ex._account = broker.get_account()
    ex._positions = broker.get_all_positions()
    ex._order_notify_ctx = {}
    ex._notified_order_ids = set()
    ex._exit_peak_prices = {}
    ex._exit_opt_meta = {}
    ex._fractionable_cache = {}
    ex._pending_exit_notes = {}
    ex._credentials_fn = lambda: ("", "")
    ex._exit_clock = time.time
    return ex


def _qty(order) -> float:
    return float(getattr(order, "qty", 0) or 0)


def test_broker_error_note_not_blank():
    empty = APIError(
        "",
        SimpleNamespace(response=SimpleNamespace(status_code=403, reason="Forbidden", text="")),
    )
    note = broker_error_note(empty)
    assert note.strip()
    assert "403" in note

    body = APIError('{"code":40310000,"message":"insufficient qty available held_for_orders"}')
    text = broker_error_note(body)
    assert "insufficient qty available" in text
    assert "held_for_orders" in text

    blank = BlankHeld()
    assert broker_error_note(blank)
    assert "held_for_orders" in broker_error_note(blank)


def test_plan_partial_equivalent_dust_and_whole_shares():
    partial = plan_exit_submission(
        position_qty=10,
        available_qty=4,
        requested_qty=10,
        price=50,
        open_orders=[],
        fractionable=True,
        min_notional=1,
    )
    assert partial.action == "submit"
    assert partial.qty <= 4

    working = SimpleNamespace(id="stop-1", symbol="SPCX", side="sell", qty=10, filled_qty=0, status="new")
    equiv = plan_exit_submission(
        position_qty=10,
        available_qty=0,
        requested_qty=10,
        price=50,
        open_orders=[working],
        fractionable=True,
        min_notional=1,
    )
    assert equiv.action == "skip_equivalent"
    assert equiv.existing is working

    dust = plan_exit_submission(
        position_qty=0.05,
        available_qty=0.05,
        requested_qty=0.05,
        price=8,
        open_orders=[],
        fractionable=True,
        min_notional=1,
    )
    assert dust.action == "skip_dust"
    assert "leaving dust" in dust.note

    whole = plan_exit_submission(
        position_qty=5.4,
        available_qty=5.4,
        requested_qty=5.4,
        price=20,
        open_orders=[],
        fractionable=False,
        min_notional=1,
    )
    assert whole.action == "submit"
    assert whole.qty == 5

    leftover = plan_exit_submission(
        position_qty=0.4,
        available_qty=0.4,
        requested_qty=0.4,
        price=50,
        open_orders=[],
        fractionable=False,
        min_notional=1,
    )
    assert leftover.action == "skip_unsellable"


def test_retry_book_locks_code_errors_and_backs_off_holds():
    book = ExitRetryBook(now=lambda: 0.0, max_attempts=3, base_delay=60)
    book.record("SPCX", "explode-exit", fingerprint="fp", terminal=True, lock_symbol=True)
    assert book.allow("SPCX", "fp") is False
    assert book.allow("SPCX", "changed") is False

    clock = {"t": 1000.0}
    holds = ExitRetryBook(now=lambda: clock["t"], max_attempts=3, base_delay=60)
    assert holds.allow("SPCX", "10|10|o") is True
    holds.record("SPCX", "held_for_orders", fingerprint="10|10|o", terminal=False)
    assert holds.allow("SPCX", "10|10|o") is False
    clock["t"] = 1061.0
    assert holds.allow("SPCX", "10|10|o") is True
    holds.record("SPCX", "held_for_orders", fingerprint="10|10|o", terminal=False)
    holds.record("SPCX", "held_for_orders", fingerprint="10|10|o", terminal=False)
    assert holds.state["SPCX"]["terminal"] is True
    clock["t"] = 10**9
    assert holds.allow("SPCX", "10|10|o") is False


def test_successful_exit_submits_available_qty():
    broker = Broker(Pos(10, 50))
    ex = _executor(broker)
    order = ex.execute_full_exit("SPCX", reason="stop_loss")
    assert order is not None
    assert broker.submits == 1
    assert _qty(broker.submitted[0]) == 10
    assert ex.take_pending_exit_note("SPCX") == ""


def test_partial_availability_does_not_oversell():
    broker = Broker(Pos(10, 50, available=4))
    ex = _executor(broker)
    ex.execute_full_exit("SPCX", reason="stop_loss")
    assert broker.submits == 1
    sold = _qty(broker.submitted[0])
    assert 0 < sold <= 4
    assert sold < 10


def test_existing_equivalent_exit_is_not_resent():
    working = SimpleNamespace(
        id="stop-1", symbol="SPCX", side="sell", qty=10, filled_qty=0, status="accepted"
    )
    broker = Broker(Pos(10, 50, available=0), orders=[working])
    ex = _executor(broker)
    first = ex.execute_full_exit("SPCX", reason="stop_loss")
    second = ex.execute_full_exit("SPCX", reason="stop_loss")
    assert first is working
    assert second is working
    assert broker.submits == 0
    assert broker.cancelled == []
    assert ex.take_pending_exit_note("SPCX") == ""


def test_held_for_orders_is_noted_once_and_not_resent():
    broker = Broker(Pos(10, 50), submit_exc=BlankHeld())
    ex = _executor(broker)
    assert ex.execute_full_exit("SPCX", reason="stop_loss") is None
    note = ex.take_pending_exit_note("SPCX")
    assert "held_for_orders" in note
    assert "insufficient qty" in note
    assert ex.execute_full_exit("SPCX", reason="stop_loss") is None
    assert broker.submits == 1
    assert ex.take_pending_exit_note("SPCX") == ""


def test_alpaca_apierror_held_note_is_captured_once():
    err = APIError(
        '{"code":40310000,"message":"insufficient qty available for order (held_for_orders: 10)"}',
        SimpleNamespace(response=SimpleNamespace(status_code=403, reason="Forbidden", text="")),
    )
    broker = Broker(Pos(10, 50), submit_exc=err)
    ex = _executor(broker)
    assert ex.execute_full_exit("SPCX", reason="stop_loss") is None
    note = ex.take_pending_exit_note("SPCX")
    assert "insufficient qty available" in note
    assert "held_for_orders" in note
    assert ex.execute_full_exit("SPCX", reason="stop_loss") is None
    assert broker.submits == 1


def test_sub_dollar_dust_is_left_without_retry():
    broker = Broker(Pos(0.05, 8))
    ex = _executor(broker)
    assert ex.execute_full_exit("SPCX", reason="stop_loss") is None
    note = ex.take_pending_exit_note("SPCX")
    assert "leaving dust" in note
    assert "$1.00" in note or "1.00" in note
    assert ex.execute_full_exit("SPCX", reason="stop_loss") is None
    assert broker.submits == 0
    assert ex.take_pending_exit_note("SPCX") == ""


def test_non_fractionable_sells_whole_shares_only():
    broker = Broker(Pos(5.4, 20), fractionable=False)
    ex = _executor(broker)
    ex.execute_full_exit("SPCX", reason="stop_loss")
    assert broker.submits == 1
    assert _qty(broker.submitted[0]) == 5

    leftover = Broker(Pos(0.4, 50), fractionable=False)
    ex2 = _executor(leftover)
    assert ex2.execute_full_exit("SPCX", reason="stop_loss") is None
    assert leftover.submits == 0
    note = ex2.take_pending_exit_note("SPCX")
    assert "fractionable" in note or "whole share" in note
    assert ex2.execute_full_exit("SPCX", reason="stop_loss") is None
    assert leftover.submits == 0


def test_cancel_replace_when_open_order_does_not_cover():
    small = SimpleNamespace(id="tiny", symbol="SPCX", side="sell", qty=1, filled_qty=0, status="new")
    broker = Broker(Pos(10, 50, available=0), orders=[small])
    ex = _executor(broker)
    order = ex.execute_full_exit("SPCX", reason="stop_loss")
    assert broker.cancelled == ["tiny"]
    assert broker.submits == 1
    assert order is not None
    assert _qty(broker.submitted[0]) == 10


def test_unknown_exception_is_logged_once():
    broker = Broker(Pos(10, 80, entry=100), submit_exc=RuntimeError("explode-exit"))
    broker.pos.unrealized_plpc = -0.2
    ex = _executor(broker)
    journal = Journal()
    saved_smart = config.PAPER_SMART_STOPS
    saved_exit = config.EXIT_OPTIMIZATION_ENABLED
    config.PAPER_SMART_STOPS = False
    config.EXIT_OPTIMIZATION_ENABLED = False
    try:
        run_position_exits(ex, Risk(), journal, equity_session_open=True)
        run_position_exits(ex, Risk(), journal, equity_session_open=True)
    finally:
        config.PAPER_SMART_STOPS = saved_smart
        config.EXIT_OPTIMIZATION_ENABLED = saved_exit
    errors = [ev for ev in journal.events if ev[0] == "exit_error"]
    assert len(errors) == 1
    assert "explode-exit" in errors[0][1].get("notes", "")
    assert broker.submits == 1


def test_dust_close_result_has_id_and_order_filled_does_not_crash():
    result = DustCloseResult("SPCX", 0.01, 0.4, "error", "notional must be >= 1.00", order_id=None)
    assert result.id is None
    tagged = DustCloseResult("SPCX", 1, 20, "closed", "close_position", order_id="oid-9")
    assert tagged.id == "oid-9"
    ex = _executor(Broker(Pos(1, 20)))
    started = time.monotonic()
    details = ex.order_fill_details(result, max_wait=5)
    assert time.monotonic() - started < 1
    assert details is None
    assert ex.order_filled(tagged, max_wait=5) is False


def test_close_dust_skips_sub_dollar_and_whole_share_qty():
    pos = SimpleNamespace(
        symbol="SPCX", qty=0.05, current_price=8, avg_entry_price=8, market_value=0.4
    )

    class Tiny:
        def refresh_cache(self):
            return None

        def _find_position(self, symbol):
            return pos

        def _normalize_pos_symbol(self, position):
            return position.symbol

    skipped = close_dust_position(Tiny(), "SPCX", dry_run=False)
    assert skipped.status == "skipped"
    assert "leaving dust" in skipped.detail

    captured = {}

    class Whole:
        def _normalize_pos_symbol(self, position):
            return "PS"

        def get_order_params(self, symbol):
            return "PS", TimeInForce.DAY, False

        def _symbol_fractionable(self, symbol):
            return False

        def _cancel_open_orders_for(self, symbol):
            return None

        def _invalidate_cache(self):
            return None

        class client:
            @staticmethod
            def submit_order(order_data=None):
                captured["qty"] = order_data.qty
                return SimpleNamespace(id="x")

    _close_via_qty_order(Whole(), SimpleNamespace(symbol="PS"), 5.4)
    assert float(captured["qty"]) == 5


def _run() -> None:
    tests = [
        test_broker_error_note_not_blank,
        test_plan_partial_equivalent_dust_and_whole_shares,
        test_retry_book_locks_code_errors_and_backs_off_holds,
        test_successful_exit_submits_available_qty,
        test_partial_availability_does_not_oversell,
        test_existing_equivalent_exit_is_not_resent,
        test_held_for_orders_is_noted_once_and_not_resent,
        test_alpaca_apierror_held_note_is_captured_once,
        test_sub_dollar_dust_is_left_without_retry,
        test_non_fractionable_sells_whole_shares_only,
        test_cancel_replace_when_open_order_does_not_cover,
        test_unknown_exception_is_logged_once,
        test_dust_close_result_has_id_and_order_filled_does_not_crash,
        test_close_dust_skips_sub_dollar_and_whole_share_qty,
    ]
    for test in tests:
        test()
        print("ok", test.__name__)


if __name__ == "__main__":
    _run()
