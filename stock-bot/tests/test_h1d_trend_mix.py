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
from modules.trading_books import BOOKS, book_dropdown_entries

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
                "H1D_TREND_MIX_EXCLUSIVE",
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

    def test_default_off_and_book_ids(self) -> None:
        self.assertFalse(config.effective_h1d_trend_mix_enabled())
        self.assertFalse(config.h1d_trend_mix_exclusive())
        self.assertNotIn("H1D_TREND_MIX_ENABLED", config.REALISTIC_RESEARCH_ENV)
        self.assertNotIn("H1D_TREND_MIX_EXCLUSIVE", config.REALISTIC_RESEARCH_ENV)
        live = BOOKS["alpaca_live"]
        lab = BOOKS["alpaca_paper"]
        medium = BOOKS["alpaca_paper_v2"]
        self.assertEqual(live["role"], "live")
        self.assertTrue(live["enabled"])
        self.assertFalse(live.get("hidden"))
        self.assertEqual(lab["role"], "lab")
        self.assertTrue(lab["enabled"])
        self.assertTrue(lab["paper_chase"])
        self.assertEqual(medium["role"], "medium")
        self.assertTrue(medium["enabled"])  # live registry: Medium runs
        self.assertFalse(medium.get("hidden"))
        self.assertTrue(medium["paper_chase"])
        self.assertTrue(medium["default_paper"])
        menu = [book_id for _label, book_id, _ok in book_dropdown_entries()]
        self.assertIn("alpaca_live", menu)
        self.assertIn("alpaca_paper", menu)
        self.assertIn("alpaca_paper_v2", menu)
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

    def test_hard_stop_exemption_follows_exclusive_mode(self) -> None:
        self.assertFalse(h1d.hard_stop_exempt("SSO"))
        stop, atr = h1d.apply_hard_stop_exemption(
            "SSO",
            stop_hit=True,
            atr_stop_hit=True,
            smart_exit_code="smart_hard_exit",
        )
        self.assertTrue(stop)
        self.assertTrue(atr)
        os.environ["H1D_TREND_MIX_ENABLED"] = "true"
        self.assertFalse(h1d.hard_stop_exempt("SSO"))
        self.assertFalse(h1d.hard_stop_exempt("AAPL"))
        os.environ["H1D_TREND_MIX_EXCLUSIVE"] = "true"
        self.assertTrue(h1d.hard_stop_exempt("SSO"))
        self.assertTrue(h1d.hard_stop_exempt("SPY"))
        self.assertTrue(h1d.hard_stop_exempt("BIL"))
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
        os.environ["H1D_TREND_MIX_EXCLUSIVE"] = "false"
        os.environ["H1D_TREND_MIX_DISABLE_HARD_STOPS"] = "true"
        self.assertTrue(h1d.hard_stop_exempt("SSO"))
        self.assertFalse(h1d.hard_stop_exempt("AAPL"))

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


    def test_exclusive_requires_the_strategy_and_the_book(self) -> None:
        os.environ["H1D_TREND_MIX_EXCLUSIVE"] = "true"
        self.assertFalse(config.h1d_trend_mix_exclusive())
        os.environ["H1D_TREND_MIX_ENABLED"] = "true"
        os.environ["H1D_TREND_MIX_BOOKS"] = "alpaca_paper_v2"
        os.environ["TRADING_BOOK_ID"] = "alpaca_live"
        self.assertFalse(config.h1d_trend_mix_exclusive())
        os.environ["TRADING_BOOK_ID"] = "alpaca_paper"
        self.assertFalse(config.h1d_trend_mix_exclusive())
        os.environ["TRADING_BOOK_ID"] = "alpaca_paper_v2"
        self.assertTrue(config.h1d_trend_mix_exclusive())

    def test_exclusive_wind_down_sells_only_non_target_names(self) -> None:
        above, _last = _series(101.0)
        session = date(2026, 10, 15)
        opened, now = _open_pair(session, 2)
        held = h1d.allocate_notionals(h1d.target_weights(True), 300.0)
        held["AAPL"] = 40.0
        held["VTI"] = 25.0
        common = dict(
            spy_bars=above,
            session_date=session,
            now_et=now,
            session_open=opened,
            market_open=True,
            positions=held,
            equity=300.0,
            state={"signal": "risk_on", "last_rebalance_month": "2026-10"},
        )
        stacked = h1d.build_plan(**common)
        self.assertEqual(stacked["reason"], "hold")
        self.assertNotIn("AAPL", {row["symbol"] for row in stacked["orders"]})
        plan = h1d.build_plan(exclusive=True, **common)
        self.assertEqual(plan["reason"], "wind_down")
        self.assertFalse(plan["persist"])
        symbols = {row["symbol"] for row in plan["orders"]}
        self.assertEqual(symbols, {"AAPL", "VTI"})
        self.assertTrue(all(row["side"] == "sell" for row in plan["orders"]))
        self.assertTrue(all(row.get("wind_down") for row in plan["orders"]))
        late = h1d.build_plan(
            exclusive=True,
            **{**common, "now_et": opened + timedelta(minutes=8)},
        )
        self.assertEqual(late["orders"], [])
        self.assertEqual(late["reason"], "stale_entry")
        preview = {row["symbol"] for row in late["preview_orders"]}
        self.assertEqual(preview, {"AAPL", "VTI"})
        self.assertIn("next open", h1d.format_plan(late, dry_run=True))
        self.assertIn("not sent", h1d.format_plan(late, dry_run=True))

    def test_exclusive_initial_rebalance_sells_before_buys(self) -> None:
        above, _last = _series(101.0)
        session = date(2026, 10, 1)
        opened, now = _open_pair(session, 1)
        plan = h1d.build_plan(
            spy_bars=above,
            session_date=session,
            now_et=now,
            session_open=opened,
            market_open=True,
            positions={"AAPL": 80.0},
            equity=300.0,
            state={},
            exclusive=True,
            broker_min=10.0,
        )
        self.assertEqual(plan["reason"], "initial")
        self.assertTrue(plan["persist"])
        sells = [row for row in plan["orders"] if row["side"] == "sell"]
        buys = [row for row in plan["orders"] if row["side"] == "buy"]
        self.assertEqual(plan["orders"][: len(sells)], sells)
        self.assertEqual(sells[0]["symbol"], "AAPL")
        self.assertTrue(buys)
        self.assertIn("SSO", {row["symbol"] for row in buys})

    def test_broker_min_batches_residuals_and_full_exits_wind_down(self) -> None:
        partials = h1d.apply_broker_min(
            [
                {
                    "symbol": "XLK",
                    "side": "sell",
                    "notional": 4.0,
                    "target_weight": 0.04,
                    "position_value": 16.0,
                }
            ],
            10.0,
        )
        self.assertEqual(partials[0]["status"], "deferred_below_min")
        self.assertNotIn("full_exit", partials[0])
        capped = h1d.apply_broker_min(
            [
                {
                    "symbol": "XLK",
                    "side": "sell",
                    "notional": 7.0,
                    "target_weight": 0.04,
                    "position_value": 8.0,
                },
                {
                    "symbol": "XLB",
                    "side": "sell",
                    "notional": 6.0,
                    "target_weight": 0.04,
                    "position_value": 20.0,
                },
            ],
            10.0,
        )
        self.assertTrue(all(row["status"] == "deferred_below_min" for row in capped))
        self.assertTrue(all(not row.get("full_exit") for row in capped))
        batched = h1d.apply_broker_min(
            [
                {"symbol": "XLB", "side": "buy", "notional": 4.0, "target_weight": 0.04},
                {"symbol": "XLE", "side": "buy", "notional": 4.0, "target_weight": 0.04},
                {"symbol": "XLK", "side": "buy", "notional": 5.0, "target_weight": 0.04},
            ],
            10.0,
        )
        self.assertEqual(len(batched), 1)
        self.assertEqual(batched[0]["symbol"], "XLK")
        self.assertAlmostEqual(batched[0]["notional"], 13.0)
        self.assertEqual(set(batched[0]["batched_from"]), {"XLB", "XLE"})
        closing = h1d.apply_broker_min(
            [
                {
                    "symbol": "AAPL",
                    "side": "sell",
                    "notional": 6.0,
                    "target_weight": 0.0,
                    "position_value": 6.0,
                    "wind_down": True,
                }
            ],
            10.0,
        )
        self.assertTrue(closing[0]["full_exit"])
        self.assertNotEqual(closing[0].get("status"), "deferred_below_min")
        unchanged = h1d.apply_broker_min(
            [{"symbol": "XLB", "side": "buy", "notional": 4.0, "target_weight": 0.04}],
            0.0,
        )
        self.assertNotIn("status", unchanged[0])
        self.assertAlmostEqual(unchanged[0]["notional"], 4.0)

    def test_wind_down_full_exit_does_not_persist_state(self) -> None:
        above, _last = _series(101.0)
        session = date(2026, 10, 15)
        opened, now = _open_pair(session, 1)
        held = h1d.allocate_notionals(h1d.target_weights(True), 300.0)
        held["AAPL"] = 6.0
        executor = _FakeExecutor()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(
                json.dumps({"signal": "risk_on", "last_rebalance_month": "2026-10"}),
                encoding="utf-8",
            )
            before = path.read_text(encoding="utf-8")
            os.environ["H1D_TREND_MIX_ENABLED"] = "true"
            os.environ["H1D_TREND_MIX_EXCLUSIVE"] = "true"
            plan = h1d.run_h1d_trend_mix_cycle(
                executor,
                market_open=True,
                now_et=now,
                session_open=opened,
                session_date=session,
                spy_bars=above,
                positions=held,
                equity=300.0,
                dry_run=False,
                state_path=path,
                confirm_market=False,
                exclusive=True,
                broker_min=10.0,
            )
            self.assertEqual(plan["reason"], "wind_down")
            self.assertEqual(executor.full_exits, ["AAPL"])
            self.assertEqual(executor.calls, [])
            self.assertFalse(plan.get("persisted"))
            self.assertEqual(path.read_text(encoding="utf-8"), before)
            self.assertIn("AAPL", plan["report"])
            self.assertIn("full-exit", plan["report"])

    def test_exclusive_dry_run_lists_wind_down_without_sending(self) -> None:
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
            positions={"AAPL": 40.0},
            equity=300.0,
            state={},
            dry_run=True,
            confirm_market=False,
            enabled=True,
            exclusive=True,
            broker_min=10.0,
        )
        self.assertEqual(executor.calls, [])
        self.assertEqual(executor.full_exits, [])
        self.assertIn("AAPL", plan["report"])
        self.assertIn("not sent", plan["report"])
        self.assertIn("BUY SSO", plan["report"])

    def test_other_entries_and_guards(self) -> None:
        self.assertFalse(h1d.other_entry_blocked("ORB", "buy", False, False))
        self.assertTrue(h1d.other_entry_blocked("ORB", "buy", False, True))
        self.assertTrue(h1d.other_entry_blocked("ORB", "buy", True, True))
        self.assertTrue(h1d.other_entry_blocked(None, "sell", False, True))
        self.assertFalse(h1d.other_entry_blocked("NYSE", "sell", True, True))
        self.assertFalse(h1d.other_entry_blocked("H1D", "buy", False, True))
        self.assertTrue(h1d.guards_bypass_symbol("SSO", exclusive=True))
        self.assertFalse(h1d.guards_bypass_symbol("AAPL", exclusive=True))
        self.assertFalse(h1d.guards_bypass_symbol("SSO", exclusive=False))

    def test_next_open_session_uses_completed_closes(self) -> None:
        wednesday_morning = datetime(2026, 10, 7, 10, 0, tzinfo=ET)
        self.assertEqual(wednesday_morning.weekday(), 2)
        self.assertEqual(h1d.next_open_session_date(wednesday_morning), date(2026, 10, 7))
        wednesday_close = datetime(2026, 10, 7, 16, 0, tzinfo=ET)
        self.assertEqual(h1d.next_open_session_date(wednesday_close), date(2026, 10, 8))
        friday_evening = datetime(2026, 10, 9, 16, 5, tzinfo=ET)
        self.assertEqual(h1d.next_open_session_date(friday_evening), date(2026, 10, 12))
        saturday = datetime(2026, 10, 10, 11, 0, tzinfo=ET)
        self.assertEqual(h1d.next_open_session_date(saturday), date(2026, 10, 12))

    def test_cli_has_no_submit_path(self) -> None:
        with self.assertRaises(SystemExit):
            h1d.main(["--dry-run", "--book", "alpaca_live", "--send"])
        broker = h1d._ReadOnlyBroker(object())
        with self.assertRaises(RuntimeError):
            broker.execute_order("SSO", "buy", notional=10)
        with self.assertRaises(RuntimeError):
            broker.execute_full_exit("AAPL")
        code = h1d.main(
            ["--dry-run", "--book", "alpaca_live", "--env-file", "/tmp/h1d-missing.env"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            h1d.main(["--book", "alpaca_paper_v2", "--user", "someone"]),
            2,
        )

    def test_bad_non_strategy_value_fails_closed(self) -> None:
        above, _last = _series(101.0)
        session = date(2026, 10, 1)
        opened, now = _open_pair(session, 1)
        plan = h1d.build_plan(
            spy_bars=above,
            session_date=session,
            now_et=now,
            session_open=opened,
            market_open=True,
            positions={"AAPL": -5.0},
            equity=300.0,
            state={},
        )
        self.assertEqual(plan["reason"], "missing_data")
        self.assertEqual(plan["orders"], [])


class _FakeExecutor:
    dry_run = False
    equity_session_open = True

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.full_exits: list[str] = []

    def execute_order(self, symbol, side, notional=None, reason="", sleeve=None):
        self.calls.append((symbol, side, notional, sleeve, reason))
        return object()

    def execute_full_exit(self, symbol, reason="", sleeve=None, **kwargs):
        self.full_exits.append(symbol)
        return object()


if __name__ == "__main__":
    unittest.main()
