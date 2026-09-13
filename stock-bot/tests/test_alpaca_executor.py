"""Regression tests for AlpacaExecutor safety guards."""

from __future__ import annotations

from types import SimpleNamespace

import modules
from modules import alpaca_executor as executor_module
from modules.alpaca_executor import AlpacaExecutor


def test_format_qty_for_alpaca_rounds_down_for_crypto_and_equities():
    executor = AlpacaExecutor.__new__(AlpacaExecutor)

    assert executor._format_qty_for_alpaca(1.234567891, is_crypto=True, max_qty=1.25) == 1.23456789
    assert executor._format_qty_for_alpaca(1.234567891, is_crypto=False, max_qty=1.25) == "1.234567"


def test_sanitize_notional_enforces_min_floor():
    executor = AlpacaExecutor.__new__(AlpacaExecutor)

    assert executor._sanitize_notional(0.99, min_notional=1.0) is None
    assert executor._sanitize_notional(1.004, min_notional=1.0) == 1.0


def test_validate_order_symbol_skips_blacklisted_symbols():
    executor = AlpacaExecutor.__new__(AlpacaExecutor)
    executor_module._UNKNOWN_ASSETS.add("SKY-USD")

    try:
        assert not executor._validate_order_symbol("SKY-USD")
    finally:
        executor_module._UNKNOWN_ASSETS.discard("SKY-USD")


def test_execute_exit_with_auto_dust_uses_dust_cleanup_for_tiny_positions(monkeypatch):
    executor = AlpacaExecutor.__new__(AlpacaExecutor)
    executor.dry_run = True
    executor._equity_trading_allowed = lambda symbol: True
    executor._find_position = lambda symbol: SimpleNamespace(qty=0.01, current_price=100.0)
    executor.execute_full_exit = lambda *args, **kwargs: "full-exit"

    fake_result = SimpleNamespace(status="dry_run", detail="would close full position")

    def fake_close_dust_position(self_obj, symbol, *, dry_run, max_notional, max_qty):
        assert dry_run is True
        assert max_notional == 10.0
        assert max_qty == 0.001
        return fake_result

    monkeypatch.setattr(
        modules,
        "dust_cleanup",
        SimpleNamespace(
            position_qty_notional=lambda pos: (0.01, 2.0),
            is_dust_position=lambda qty, notional, **kwargs: True,
            close_dust_position=fake_close_dust_position,
        ),
        raising=False,
    )

    result = executor.execute_exit_with_auto_dust("XYZ")
    assert result is fake_result


def test_execute_full_exit_caps_qty_to_available_xle(monkeypatch):
    """Regression: never over-request sell qty when qty_available << qty (XLE 403)."""
    executor = AlpacaExecutor.__new__(AlpacaExecutor)
    executor.dry_run = False
    executor._equity_trading_allowed = lambda symbol: True
    executor._cancel_open_orders_for = lambda symbol: None
    executor.refresh_cache = lambda: None
    executor.get_order_params = lambda symbol: ("XLE", "day", False)

    tiny_avail = 0.011234
    stale_qty = 128.0
    pos = SimpleNamespace(
        symbol="XLE",
        qty=stale_qty,
        qty_available=tiny_avail,
        current_price=90.0,
        avg_entry_price=88.0,
        market_value=stale_qty * 90.0,
    )
    executor._find_position = lambda symbol: pos
    executor._get_positions = lambda: [pos]

    # Position is not dust by full size — must sell capped available, not recurse.
    monkeypatch.setattr(
        "config.effective_auto_dust_cleaner_enabled", lambda: True, raising=False
    )
    monkeypatch.setattr(
        "config.effective_auto_dust_max_notional", lambda: 10.0, raising=False
    )

    captured = {}

    def fake_submit(order, **kwargs):
        captured["order"] = order
        captured["kwargs"] = kwargs
        return SimpleNamespace(id="ord-xle-test")

    executor._submit_order = fake_submit

    result = executor.execute_full_exit("XLE", reason="sector_rot_flat")
    assert result is not None
    submitted_qty = float(captured["order"].qty)
    assert submitted_qty <= tiny_avail
    assert submitted_qty < stale_qty
    assert submitted_qty > 0


def test_position_available_qty_prefers_qty_available():
    pos = SimpleNamespace(qty=128.0, qty_available=0.011)
    assert AlpacaExecutor._position_available_qty(pos) == 0.011
    pos_short = SimpleNamespace(qty=-50.0, qty_available=-0.5)
    assert AlpacaExecutor._position_available_qty(pos_short) == -0.5
    pos_missing = SimpleNamespace(qty=10.0)
    assert AlpacaExecutor._position_available_qty(pos_missing) == 10.0
