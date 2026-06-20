"""Stat-arb book reconcile — orphan filtering tests.

Run: python tests/test_stat_arb_reconcile.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from modules.stat_arb_sleeve import reconcile_stat_arb_book


class _Pos:
    def __init__(self, symbol: str, qty: float):
        self.symbol = symbol
        self.qty = qty


class _MockExecutor:
    def __init__(self, positions: list[_Pos], book: dict | None = None):
        self._positions = positions
        self._stat_arb_open = dict(book or {})
        self._pair_symbols: set[str] = set()

    def _get_positions(self):
        return self._positions

    def _find_position(self, symbol: str):
        target = config.normalize_symbol(symbol)
        for pos in self._positions:
            if config.normalize_symbol(pos.symbol) == target:
                return pos
        return None

    def register_pair_symbols(self, long_sym: str, short_sym: str) -> None:
        self._pair_symbols.add(config.normalize_symbol(long_sym))
        self._pair_symbols.add(config.normalize_symbol(short_sym))


def _assert_no_warning_orphans(result: dict) -> None:
    assert result.get("orphans") == [], f"expected no orphans, got {result.get('orphans')}"


def run_tests() -> None:
    saved_crypto = config.CRYPTO_SLEEVE_ENABLED
    saved_stat = config.PAPER_STAT_ARB_ENABLED
    saved_paper = config.PAPER_AGGRESSIVE_ENABLED
    saved_paper_ctx = config.paper_aggressive_context()
    try:
        config.PAPER_AGGRESSIVE_ENABLED = True
        config.set_paper_aggressive_context(True)
        config.PAPER_STAT_ARB_ENABLED = True
        config.CRYPTO_SLEEVE_ENABLED = True
        # Typical paper book: VTI + SPY + NYSE longs should not warn as orphans.
        config.CRYPTO_SLEEVE_ENABLED = False
        ex = _MockExecutor(
            [
                _Pos("VTI", 1.0),
                _Pos("SPY", 2.0),
                _Pos("AAPL", 3.0),
                _Pos("BTC/USD", 0.01),
            ],
            book={"STALE/PAIR": {"long_symbol": "FOO", "short_symbol": "BAR"}},
        )
        result = reconcile_stat_arb_book(ex)
        _assert_no_warning_orphans(result)
        assert "crypto_sleeve_disabled" in result["ignored"]
        assert "VTI" in result["ignored"]["vti_core"]
        assert "SPY" in result["ignored"]["spy"]
        assert "AAPL" in result["ignored"]["nyse_long_single"]
        assert "STALE/PAIR" in result["removed"]

        # Open pair in book + both legs held → tracked, no orphans.
        ex2 = _MockExecutor(
            [
                _Pos("AAPL", 2.0),
                _Pos("MSFT", -1.0),
            ],
            book={
                "AAPL/MSFT": {
                    "long_symbol": "AAPL",
                    "short_symbol": "MSFT",
                }
            },
        )
        result2 = reconcile_stat_arb_book(ex2)
        _assert_no_warning_orphans(result2)
        assert "AAPL/MSFT" in result2["kept"]
        assert "AAPL" in ex2._pair_symbols
        assert "MSFT" in ex2._pair_symbols

        # Stale pair registry auto-resolved.
        ex3 = _MockExecutor([_Pos("VTI", 1.0)])
        ex3._pair_symbols = {"GOOG", "META"}
        result3 = reconcile_stat_arb_book(ex3)
        _assert_no_warning_orphans(result3)
        assert set(result3["resolved"]) == {"GOOG", "META"}
        assert ex3._pair_symbols == set()

        # Crypto disabled: crypto book rows purged, crypto positions ignored.
        ex4 = _MockExecutor(
            [_Pos("BTC-USD", 0.5), _Pos("ETH-USD", -0.2)],
            book={
                "BTC-USD/ETH-USD": {
                    "long_symbol": "BTC-USD",
                    "short_symbol": "ETH-USD",
                }
            },
        )
        result4 = reconcile_stat_arb_book(ex4)
        _assert_no_warning_orphans(result4)
        assert "BTC-USD/ETH-USD" in result4["removed"]
        assert all(
            config.is_crypto(sym)
            for syms in result4["ignored"].values()
            for sym in syms
        ) or result4["ignored"].get("crypto_sleeve_disabled")

        print("test_stat_arb_reconcile: all passed")
    finally:
        config.CRYPTO_SLEEVE_ENABLED = saved_crypto
        config.PAPER_STAT_ARB_ENABLED = saved_stat
        config.PAPER_AGGRESSIVE_ENABLED = saved_paper
        config.set_paper_aggressive_context(saved_paper_ctx)


if __name__ == "__main__":
    run_tests()
