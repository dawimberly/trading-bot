"""Replay stays isolated: no sends, no today's tape, not called faithful."""

from __future__ import annotations

import unittest
from pathlib import Path

from research.live_replay.replay import base_ma50_rank, fixed_stop_hit, replay_decision, PointInTimeBundle
from research.live_replay.settings import live_decision_flags, resolve_effective
from research.live_replay.sim_broker import OrderSendBlocked, SimBroker


class BrokerTests(unittest.TestCase):
    def test_send_methods_raise(self):
        broker = SimBroker(cash=15.0, equity=300.0)
        for name in (
            "submit_order",
            "place_order",
            "cancel_order",
            "replace_order",
            "execute_order",
            "execute_full_exit",
            "execute_reduce_notional",
        ):
            with self.assertRaises(OrderSendBlocked):
                getattr(broker, name)()
        with self.assertRaises(OrderSendBlocked):
            broker.client.get_orders()

    def test_recorded_fill_does_not_send(self):
        broker = SimBroker(cash=20.0)
        broker.apply_recorded_fill(side="buy", notional=4.72)
        self.assertAlmostEqual(broker.cash, 15.28)
        self.assertEqual(broker.intents, [])


class ReplayTests(unittest.TestCase):
    def test_missing_bundle_is_incomplete(self):
        result = replay_decision(None)
        self.assertFalse(result.faithful)
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.ranked, [])

    def test_supplied_frame_ranks_the_stronger_name_only(self):
        weak = [100.0] * 49 + [101.0]
        strong = [100.0] * 49 + [120.0]
        ranked = base_ma50_rank({"WEAK": weak, "STRONG": strong, "OTHER": [1.0]}, ["STRONG", "WEAK"])
        self.assertEqual(ranked, ["STRONG", "WEAK"])

    def test_bundle_does_not_become_faithful(self):
        bundle = PointInTimeBundle(
            closes={"AAA": [100.0] * 49 + [110.0]},
            universe=["AAA"],
            as_of="2026-10-15T09:36:00-04:00",
            cash=15.0,
            equity=300.0,
            positions={},
            cooldown={},
            regime="RHYME_C",
        )
        result = replay_decision(bundle)
        self.assertFalse(result.faithful)
        self.assertEqual(result.ranked, ["AAA"])
        self.assertEqual(result.intents[0]["sent"], False)

    def test_gap_through_five_percent_is_the_fixed_stop(self):
        self.assertTrue(fixed_stop_hit(101.601, 92.578))
        self.assertFalse(fixed_stop_hit(100.0, 96.0))
        self.assertTrue(fixed_stop_hit(100.0, 99.0, plpc=-0.05))

    def test_module_does_not_load_the_live_database(self):
        text = Path(__file__).with_name("replay.py").read_text(encoding="utf-8")
        self.assertNotIn("load_close_matrix", text)
        self.assertNotIn("load_live_close_matrix", text)
        self.assertNotIn("TradingClient", text)


class SettingsTests(unittest.TestCase):
    def test_overlay_beats_book_and_stock_fills_missing_keys(self):
        effective = resolve_effective(
            parent_env={},
            book_env={
                "CONCENTRATION_TRIM_MIN_PCT": "0",
                "AUTO_DUST_MAX_NOTIONAL": "2",
                "LIVE_CONSERVATIVE_ENABLED": "false",
                "VTI_CORE_ENABLED": "false",
                "PAPER_TRADING": "false",
                "PER_NAME_MAX_PCT": "0.15",
                "NYSE_SLEEVE_CAP_PCT": "0.95",
                "APCA_API_KEY_ID": "hidden",
            },
            stock_env={"MAX_ACTIVE_TICKERS": "10", "AUTO_DUST_MAX_NOTIONAL": "10"},
        )
        self.assertEqual(effective["CONCENTRATION_TRIM_MIN_PCT"], "0.005")
        self.assertEqual(effective["AUTO_DUST_MAX_NOTIONAL"], "2")
        self.assertEqual(effective["MAX_ACTIVE_TICKERS"], "10")
        self.assertNotIn("APCA_API_KEY_ID", effective)
        flags = live_decision_flags(effective)
        self.assertFalse(flags["live_conservative_lock"])
        self.assertTrue(flags["fixed_stop_branch"])
        self.assertFalse(flags["smart_stops_effective"])
        self.assertFalse(flags["exit_optimization_effective"])
        self.assertFalse(flags["idle_cash_scale_in"])
        self.assertEqual(flags["stop_loss_pct"], 0.05)
        self.assertIn("LIVE_MAX_ACTIVE_TICKERS_MIN", flags["unread_book_keys"])


if __name__ == "__main__":
    unittest.main()
