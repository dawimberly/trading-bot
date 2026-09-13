"""Unit + dry-run coverage for executor concentration / dust / active-ticker guards."""

from __future__ import annotations

from types import SimpleNamespace

import config
from modules.alpaca_executor import AlpacaExecutor
from modules import dust_cleanup


def _pos(symbol: str, qty: float, price: float):
    mv = qty * price
    return SimpleNamespace(
        symbol=symbol,
        qty=qty,
        current_price=price,
        avg_entry_price=price,
        market_value=mv,
    )


def _make_executor(positions, *, equity=100_000.0, dry_run=True):
    ex = AlpacaExecutor.__new__(AlpacaExecutor)
    ex.dry_run = dry_run
    ex.paper = True
    ex._positions = list(positions)
    ex._account = SimpleNamespace(equity=equity, cash=equity * 0.2)
    ex.refresh_cache = lambda: None
    ex._get_positions = lambda: list(ex._positions)
    ex._account_equity = lambda: float(equity)
    ex._find_position = lambda symbol: next(
        (
            p
            for p in ex._positions
            if config.normalize_symbol(p.symbol) == config.normalize_symbol(symbol)
        ),
        None,
    )
    ex._normalize_pos_symbol = AlpacaExecutor._normalize_pos_symbol
    ex._min_notional = lambda: 1.0
    ex.execute_order = lambda *a, **k: SimpleNamespace(id="DRY-TEST")
    return ex


def test_concentration_cap_blocks_over_8pct():
    # Existing AAPL already 7% ($7k); buy $2k should cap to ~$1k room
    ex = _make_executor([_pos("AAPL", 70, 100.0)], equity=100_000.0)
    capped = ex._apply_concentration_cap("AAPL", 2000.0)
    assert capped is not None
    assert abs(capped - 1000.0) < 0.02


def test_concentration_cap_blocks_when_already_at_limit():
    ex = _make_executor([_pos("MSFT", 80, 100.0)], equity=100_000.0)  # 8%
    assert ex._apply_concentration_cap("MSFT", 500.0) is None


def test_core_symbols_exempt_from_concentration():
    ex = _make_executor([_pos("VTI", 500, 200.0)], equity=100_000.0)  # 100%
    assert ex._apply_concentration_cap("VTI", 5000.0) == 5000.0


def test_active_ticker_limit_blocks_26th_name():
    positions = [_pos(f"T{i:02d}", 1, 100.0) for i in range(25)]
    positions.append(_pos("VTI", 10, 200.0))  # core exempt — does not count
    ex = _make_executor(positions, equity=100_000.0)
    assert ex.count_active_tickers() == 25
    assert ex._blocks_new_active_ticker("NEWT") is True
    assert ex._blocks_new_active_ticker("T00") is False  # already held
    assert ex._blocks_new_active_ticker("VTI") is False  # core


def test_auto_dust_threshold_ten_dollars():
    assert config.effective_auto_dust_max_notional() == 10.0
    assert dust_cleanup.is_dust_position(1.0, 9.99, max_notional=10.0)
    assert not dust_cleanup.is_dust_position(1.0, 10.01, max_notional=10.0)


def test_enforce_portfolio_guards_dry_run():
    positions = [
        _pos("FAT", 100, 100.0),  # 10% → excess ~$2k at 8% cap
        _pos("DUST", 0.05, 50.0),  # $2.50 dust
        *[_pos(f"N{i:02d}", 1, 50.0) for i in range(3)],
    ]
    ex = _make_executor(positions, equity=100_000.0, dry_run=True)
    summary = ex.enforce_portfolio_guards(dry_run=True)
    assert summary["dry_run"] is True
    assert summary["max_active_tickers"] == 25
    assert summary["per_name_max_pct"] == 0.08
    assert summary["auto_dust_max_notional"] == 10.0
    assert any(r["symbol"] == "FAT" and r["status"] == "dry_run" for r in summary["concentration_trims"])
    assert any(r["symbol"] == "DUST" and r["status"] == "dry_run" for r in summary["dust_actions"])


def test_execute_exit_with_auto_dust_uses_ten_dollar_default(monkeypatch):
    ex = AlpacaExecutor.__new__(AlpacaExecutor)
    ex.dry_run = True
    ex._equity_trading_allowed = lambda symbol: True
    ex._find_position = lambda symbol: SimpleNamespace(qty=0.05, current_price=100.0, market_value=5.0)
    ex.execute_full_exit = lambda *args, **kwargs: "full-exit"

    seen = {}

    def fake_close(self_obj, symbol, *, dry_run, max_notional, max_qty):
        seen["max_notional"] = max_notional
        seen["dry_run"] = dry_run
        return SimpleNamespace(status="dry_run")

    monkeypatch.setattr(
        "modules.dust_cleanup.position_qty_notional",
        lambda pos: (0.05, 5.0),
    )
    monkeypatch.setattr(
        "modules.dust_cleanup.is_dust_position",
        lambda qty, notional, **kwargs: True,
    )
    monkeypatch.setattr(
        "modules.dust_cleanup.close_dust_position",
        fake_close,
    )

    result = ex.execute_exit_with_auto_dust("XYZ")
    assert result.status == "dry_run"
    assert seen["max_notional"] == 10.0
    assert seen["dry_run"] is True
