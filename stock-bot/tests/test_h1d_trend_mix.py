"""h1d_trend_mix: signal timing, rebalance math, dust, and fail-safe paths.

Run: python tests/test_h1d_trend_mix.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from modules import h1d_trend_mix as h1d
from modules.trading_books import BOOKS

ET = ZoneInfo("America/New_York")


def _series(last_close: float, n: int = 200) -> tuple[list[tuple[date, float]], date]:
    day = date(2020, 1, 2)
    bars: list[tuple[date, float]] = []
    for _ in range(n - 1):
        bars.append((day, 100.0))
        day += timedelta(days=1)
    bars.append((day, float(last_close)))
    return bars, day


def _open_pair(session: date, minutes_after: int = 1) -> tuple[datetime, datetime]:
    opened = datetime(session.year, session.month, session.day, 9, 30, tzinfo=ET)
    return opened, opened + timedelta(minutes=minutes_after)


class H1dTrendMixTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            key: os.environ.get(key)
            for key in (
                "H1D_TREND_MIX_ENABLED",
                "H1D_TREND_MIX_DRY_RUN",
                "H1D_TREND_MIX_BOOKS",
                "H1D_TREND_MIX_DISABLE_HARD_STOPS",
                "H1D_TREND_MIX_DUST_NOTIONAL",
                "H1D_TREND_MIX_ENTRY_FRESHNESS_MIN",
                "TRADING_BOOK_ID",
            )
        }
        for key in self._saved:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_default_off_and_not_in_book_profiles(self) -> None:
        self.assertFalse(config.effective_h1d_trend_mix_enabled())
        self.assertNotIn("H1D_TREND_MIX_ENABLED", config.REALISTIC_RESEARCH_ENV)
        self.assertIn("alpaca_live", BOOKS)
        self.assertIn("alpaca_paper", BOOKS)
        self.assertNotIn("h1d_trend_mix", BOOKS)

    def test_book_allowlist_fail_safe(self) -> None:
        os.environ["H1D_TREND_MIX_ENABLED"] = "true"
        os.environ["H1D_TREND_MIX_BOOKS"] = "alpaca_paper"
        self.assertFalse(config.effective_h1d_trend_mix_enabled())
        os.environ["TRADING_BOOK_ID"] = "alpaca_live"
        self.assertFalse(config.effective_h1d_trend_mix_enabled())
        os.environ["TRADING_BOOK_ID"] = "alpaca_paper"
        self.assertTrue(config.effective_h1d_trend_mix_enabled())

    def test_weights_sum_to_one_and_split_sleeves(self) -> None:
        each = 0.5 / len(h1d.DIVERSIFIED_SYMBOLS)
        for risk_on in (True, False):
            weights = h1d.target_weights(risk_on)
            self.assertAlmostEqual(sum(weights.values()), 1.0, places=12)
            trend = h1d.TREND_RISK_ON if risk_on else h1d.TREND_RISK_OFF
            other = h1d.TREND_RISK_OFF if risk_on else h1d.TREND_RISK_ON
            self.assertAlmostEqual(weights[trend], 0.5, places=12)
            self.assertNotIn(other, weights)
            for symbol in h1d.DIVERSIFIED_SYMBOLS:
                self.assertAlmostEqual(weights[symbol], each, places=9)
            allocated = h1d.allocate_notionals(weights, 300.0)
            self.assertAlmostEqual(sum(allocated.values()), 300.0, places=2)

    def test_signal_uses_prior_close_not_the_session_close(self) -> None:
        below, last = _series(99.0)
        session = date(2026, 10, 1)
        self.assertLess(last, session)
        self.assertIs(h1d.signal_for_session(below, session_date=session), False)
        with_today = below + [(session, 1000.0)]
        self.assertIs(h1d.signal_for_session(with_today, session_date=session), False)
        lookahead = h1d.prior_close_signal([close for _day, close in with_today])
        self.assertIs(lookahead, True)

    def test_bil_when_close_is_not_above_sma(self) -> None:
        flat, last = _series(100.0)
        session = last + timedelta(days=1)
        self.assertIs(h1d.signal_for_session(flat, session_date=session), False)
        weights = h1d.target_weights(False)
        self.assertIn("BIL", weights)
        self.assertNotIn("SSO", weights)

    def test_trend_flip_sells_sso_and_buys_bil(self) -> None:
        below, _last = _series(99.0)
        session = date(2026, 10, 15)
        opened, now = _open_pair(session, 2)
        risk_on_weights = h1d.target_weights(True)
        held = h1d.allocate_notionals(risk_on_weights, 300.0)
        plan = h1d.build_plan(
            spy_bars=below,
            session_date=session,
            now_et=now,
            session_open=opened,
            market_open=True,
            positions=held,
            equity=300.0,
            state={"signal": "risk_on", "last_rebalance_month": "2026-10"},
        )
        self.assertEqual(plan["reason"], "trend_flip")
        self.assertIs(plan["risk_on"], False)
        sides = {(row["symbol"], row["side"]) for row in plan["orders"]}
        self.assertIn(("SSO", "sell"), sides)
        self.assertIn(("BIL", "buy"), sides)
        self.assertTrue(all(row["side"] != "buy" or row["symbol"] != "SSO" for row in plan["orders"]))
        sells = [row for row in plan["orders"] if row["side"] == "sell"]
        buys = [row for row in plan["orders"] if row["side"] == "buy"]
        self.assertEqual(plan["orders"][: len(sells)], sells)
        self.assertEqual(plan["orders"][len(sells) :], buys)

    def test_month_start_rebalances_and_same_month_holds(self) -> None:
        above, _last = _series(101.0)
        session = date(2026, 10, 1)
        opened, now = _open_pair(session, 1)
        held = h1d.allocate_notionals(h1d.target_weights(True), 300.0)
        month = h1d.build_plan(
            spy_bars=above,
            session_date=session,
            now_et=now,
            session_open=opened,
            market_open=True,
            positions=held,
            equity=300.0,
            state={"signal": "risk_on", "last_rebalance_month": "2026-09"},
        )
        self.assertEqual(month["reason"], "month_start")
        self.assertAlmostEqual(sum(month["weights"].values()), 1.0, places=12)
        hold = h1d.build_plan(
            spy_bars=above,
            session_date=session,
            now_et=now,
            session_open=opened,
            market_open=True,
            positions=held,
            equity=300.0,
            state={"signal": "risk_on", "last_rebalance_month": "2026-10"},
        )
        self.assertEqual(hold["reason"], "hold")
        self.assertEqual(hold["orders"], [])

    def test_dust_threshold_skips_small_deltas(self) -> None:
        weights = h1d.target_weights(True)
        targets = h1d.allocate_notionals(weights, 300.0)
        positions = dict(targets)
        positions["XLB"] = targets["XLB"] - 0.40
        positions["XLK"] = targets["XLK"] - 5.0
        orders = h1d.plan_rebalance(weights, positions, 300.0, dust=1.0)
        symbols = {row["symbol"] for row in orders}
        self.assertNotIn("XLB", symbols)
        xlk = next(row for row in orders if row["symbol"] == "XLK")
        self.assertEqual(xlk["side"], "buy")
        self.assertGreaterEqual(xlk["notional"], 1.0)
        self.assertAlmostEqual(xlk["notional"], 5.0, places=2)

    def test_missing_data_and_closed_market_place_no_orders(self) -> None:
        short, last = _series(101.0, n=199)
        session = date(2026, 10, 1)
        opened, now = _open_pair(session, 1)
        common = dict(
            session_date=session,
            now_et=now,
            session_open=opened,
            market_open=True,
            positions={},
            equity=300.0,
            state={},
        )
        missing = h1d.build_plan(spy_bars=short, **common)
        self.assertEqual(missing["reason"], "missing_data")
        self.assertEqual(missing["orders"], [])
        self.assertLess(last, session)
        nan_bars, _ = _series(101.0)
        nan_bars[-2] = (nan_bars[-2][0], float("nan"))
        # Dropping one close leaves 199 completed sessions.
        dropped = h1d.build_plan(spy_bars=nan_bars, **common)
        self.assertEqual(dropped["orders"], [])
        self.assertEqual(dropped["reason"], "missing_data")
        above, _ = _series(101.0)
        no_equity = h1d.build_plan(spy_bars=above, **{**common, "equity": None})
        self.assertEqual(no_equity["orders"], [])
        unknown_positions = h1d.build_plan(
            spy_bars=above, **{**common, "positions": None}
        )
        self.assertEqual(unknown_positions["orders"], [])
        closed = h1d.build_plan(
            spy_bars=above, **{**common, "market_open": False}
        )
        self.assertEqual(closed["reason"], "market_closed")
        self.assertEqual(closed["orders"], [])
        unreadable = h1d.build_plan(spy_bars=above, **{**common, "state": None})
        self.assertEqual(unreadable["reason"], "state_unreadable")
        self.assertEqual(unreadable["orders"], [])

    def test_stale_decision_and_late_session_do_not_trade(self) -> None:
        above, _last = _series(101.0)
        session = date(2026, 10, 1)
        opened, now = _open_pair(session, 2)
        late = opened + timedelta(minutes=8)
        base = dict(
            spy_bars=above,
            session_date=session,
            session_open=opened,
            market_open=True,
            positions={},
            equity=300.0,
            state={},
        )
        stale_clock = h1d.build_plan(now_et=late, **base)
        self.assertEqual(stale_clock["reason"], "stale_entry")
        self.assertEqual(stale_clock["orders"], [])
        old_decision = h1d.build_plan(
            now_et=now,
            decision_at=now - timedelta(minutes=8),
            **base,
        )
        self.assertEqual(old_decision["orders"], [])
        self.assertFalse(old_decision["execute"])

    def test_unrelated_positions_are_not_sold(self) -> None:
        above, _last = _series(101.0)
        session = date(2026, 10, 1)
        opened, now = _open_pair(session, 1)
        plan = h1d.build_plan(
            spy_bars=above,
            session_date=session,
            now_et=now,
            session_open=opened,
            market_open=True,
            positions={"AAPL": 100.0, "SSO": 0.0},
            equity=300.0,
            state={},
        )
        self.assertTrue(plan["orders"])
        self.assertNotIn("AAPL", {row["symbol"] for row in plan["orders"]})

    def test_hard_stop_exemption_is_off_until_the_strategy_is_on(self) -> None:
        self.assertFalse(h1d.hard_stop_exempt("SSO"))
        self.assertFalse(h1d.hard_stop_exempt("SPY"))
        stop, atr = h1d.apply_hard_stop_exemption(
            "SSO",
            stop_hit=True,
            atr_stop_hit=True,
            smart_exit_code="smart_hard_exit",
        )
        self.assertTrue(stop)
        self.assertTrue(atr)
        os.environ["H1D_TREND_MIX_ENABLED"] = "true"
        self.assertTrue(h1d.hard_stop_exempt("SSO"))
        self.assertTrue(h1d.hard_stop_exempt("SPY"))
        self.assertFalse(h1d.hard_stop_exempt("AAPL"))
        stop, atr = h1d.apply_hard_stop_exemption(
            "BIL",
            stop_hit=True,
            atr_stop_hit=True,
            smart_exit_code="smart_hard_exit",
        )
        self.assertFalse(stop)
        self.assertFalse(atr)
        stop, atr = h1d.apply_hard_stop_exemption(
            "SSO",
            stop_hit=True,
            atr_stop_hit=False,
            smart_exit_code="",
        )
        self.assertFalse(stop)
        self.assertFalse(atr)
        stop, atr = h1d.apply_hard_stop_exemption(
            "GLD",
            stop_hit=True,
            atr_stop_hit=True,
            smart_exit_code="smart_atr_stop",
        )
        self.assertTrue(stop)
        self.assertTrue(atr)
        os.environ["H1D_TREND_MIX_DISABLE_HARD_STOPS"] = "false"
        self.assertFalse(h1d.hard_stop_exempt("SSO"))

    def test_dry_run_prints_orders_without_sending_or_persisting(self) -> None:
        above, _last = _series(101.0)
        session = date(2026, 10, 1)
        opened, now = _open_pair(session, 1)
        executor = _FakeExecutor()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            os.environ["H1D_TREND_MIX_ENABLED"] = "true"
            plan = h1d.run_h1d_trend_mix_cycle(
                executor,
                market_open=True,
                now_et=now,
                session_open=opened,
                session_date=session,
                spy_bars=above,
                positions={},
                equity=300.0,
                state={},
                dry_run=True,
                state_path=path,
                confirm_market=False,
            )
        self.assertEqual(executor.calls, [])
        self.assertFalse(path.exists())
        self.assertIn("not sent", plan["report"])
        self.assertIn("sum=100.00%", plan["report"])
        self.assertIn("SSO", plan["report"])
        self.assertTrue(plan["preview_orders"])

    def test_live_cycle_submits_notional_orders_and_stores_signal_only(self) -> None:
        above, _last = _series(101.0)
        session = date(2026, 10, 1)
        opened, now = _open_pair(session, 1)
        executor = _FakeExecutor()
        equity = max(300.0, float(config.effective_min_notional()) * 24.0 + 50.0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            os.environ["H1D_TREND_MIX_ENABLED"] = "true"
            os.environ["H1D_TREND_MIX_DRY_RUN"] = "false"
            plan = h1d.run_h1d_trend_mix_cycle(
                executor,
                market_open=True,
                now_et=now,
                session_open=opened,
                session_date=session,
                spy_bars=above,
                positions={},
                equity=equity,
                state={},
                dry_run=False,
                state_path=path,
                confirm_market=False,
            )
            self.assertTrue(plan["send"])
            self.assertTrue(executor.calls)
            self.assertTrue(all(call[2] and call[2] > 0 for call in executor.calls))
            self.assertTrue(all(call[3] == "H1D" for call in executor.calls))
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["signal"], "risk_on")
            self.assertEqual(saved["last_rebalance_month"], "2026-10")
            self.assertNotIn("equity", saved)
            self.assertNotIn(str(int(equity)), json.dumps(saved))
            held = h1d.allocate_notionals(h1d.target_weights(True), equity)
            again = _FakeExecutor()
            second = h1d.run_h1d_trend_mix_cycle(
                again,
                market_open=True,
                now_et=now,
                session_open=opened,
                session_date=session,
                spy_bars=above,
                positions=held,
                equity=equity,
                state_path=path,
                dry_run=False,
                confirm_market=False,
            )
        self.assertEqual(second["reason"], "hold")
        self.assertEqual(again.calls, [])

    def test_disabled_cycle_does_not_trade(self) -> None:
        above, _last = _series(101.0)
        session = date(2026, 10, 1)
        opened, now = _open_pair(session, 1)
        executor = _FakeExecutor()
        plan = h1d.run_h1d_trend_mix_cycle(
            executor,
            market_open=True,
            now_et=now,
            session_open=opened,
            session_date=session,
            spy_bars=above,
            positions={},
            equity=300.0,
            state={},
            dry_run=False,
            confirm_market=False,
            enabled=False,
        )
        self.assertEqual(plan["reason"], "disabled")
        self.assertEqual(plan["orders"], [])
        self.assertEqual(executor.calls, [])


class _FakeExecutor:
    dry_run = False
    equity_session_open = True

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def execute_order(self, symbol, side, notional=None, reason="", sleeve=None):
        self.calls.append((symbol, side, notional, sleeve, reason))
        return object()


if __name__ == "__main__":
    unittest.main()
