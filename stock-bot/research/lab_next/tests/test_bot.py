import math
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path

from lab_next.calendar import ET, _ORDERED, bar_starts, is_session, prior_bar
from lab_next.engine import advance, fresh, timestamp
from lab_next.market import blockers
from lab_next.model import features, learn
from lab_next.__main__ import connect, ingest, report


def frame(moment="2026-10-21T10:15:00-04:00", price=100, extra=None, events=None, high=None, low=None):
    if isinstance(moment, datetime):
        when = moment.astimezone(ET)
    else:
        when = timestamp(moment)
    bars = {"X": {"open": price, "high": high if high is not None else price, "low": low if low is not None else price, "close": price, "eligible": True}}
    if extra:
        bars.update(extra)
    day = when.date().isoformat()
    return {
        "timestamp": when.isoformat(),
        "complete": True,
        "price_basis": "raw",
        "feed_mode": "synthetic",
        "as_of": (when + timedelta(minutes=5)).isoformat(),
        "action_audit": {
            "source": "synthetic_declared",
            "independent": False,
            "coverage_from": day,
            "coverage_through": day,
            "symbols": sorted(bars),
            "events": events or [],
        },
        "bars": bars,
    }


def at(day, clock, **kwargs):
    hour, minute = clock.split(":")
    moment = datetime.combine(date.fromisoformat(day), time(int(hour), int(minute)), tzinfo=ET)
    return frame(moment, **kwargs)


def seed(state, symbol, last_clock, n=50):
    hour, minute = last_clock.split(":")
    moment = datetime.combine(date(2026, 10, 21), time(int(hour), int(minute)), tzinfo=ET)
    stamps = []
    cursor = moment
    for _ in range(n):
        stamps.append(cursor)
        cursor = prior_bar(cursor)
    stamps.reverse()
    points = []
    for i, stamp in enumerate(stamps):
        points.append([stamp.isoformat(), 100 + 0.03 * i + math.sin(i / 2)])
    state["history"][symbol] = points
    return points[-1][1] + 0.2


def holding():
    state = fresh()
    for book in state["books"].values():
        book["cash"] = 99000
        book["positions"]["X"] = {
            "qty": 10,
            "cost": 1000,
            "entry": 100,
            "peak": 120,
            "armed": True,
            "entry_time": "2026-10-20T10:15:00-04:00",
            "entry_session": "2026-10-20",
            "x": [0.01, 0.01, 0.5],
            "plan_id": "test",
        }
    return state


class BotTests(unittest.TestCase):
    def test_trail_uses_close_and_exact_two_percent(self):
        state = advance(holding(), frame(price=117.6))
        self.assertEqual(state["books"]["learner"]["pending"][0]["reason"], "profit_trail")

    def test_intrabar_low_does_not_replace_the_close(self):
        state = advance(holding(), frame(price=118, high=120, low=117.6))
        self.assertEqual(state["books"]["learner"]["pending"], [])

    def test_199_percent_does_not_sell(self):
        state = advance(holding(), frame(price=117.612))
        self.assertEqual(state["books"]["learner"]["pending"], [])

    def test_hard_stop_is_inclusive_at_five_percent(self):
        state = holding()
        for book in state["books"].values():
            book["positions"]["X"]["armed"] = False
            book["positions"]["X"]["peak"] = 100
        armed = advance(state, frame(price=95))
        inside = advance(state, frame(price=95.01))
        self.assertEqual(armed["books"]["learner"]["pending"][0]["reason"], "hard_stop")
        self.assertEqual(inside["books"]["learner"]["pending"], [])

    def test_latched_exit_survives_bounce_and_cooldown_starts_at_fill(self):
        first = advance(holding(), frame(price=117.6))
        second = advance(first, frame("2026-10-21T10:20:00-04:00", 130))
        book = second["books"]["learner"]
        self.assertFalse(book["positions"])
        self.assertEqual(book["cooldown"]["X"], "2026-10-21T10:50:00-04:00")
        self.assertAlmostEqual(book["closed"][0]["exit"], 130 * 0.9995)
        self.assertFalse(book["closed"][0]["gap_fill"])

    def test_gap_sell_is_labeled_and_gap_buy_is_cancelled(self):
        first = advance(holding(), frame(price=117.6))
        second = advance(first, frame("2026-10-21T10:25:00-04:00", 130))
        self.assertTrue(second["books"]["learner"]["closed"][0]["gap_fill"])
        state = fresh()
        price = seed(state, "X", "10:10")
        decided = advance(state, frame(price=price))
        self.assertEqual(decided["books"]["learner"]["pending"][0]["side"], "buy")
        skipped = advance(decided, frame("2026-10-21T10:25:00-04:00", price))
        self.assertNotIn("X", skipped["books"]["learner"]["positions"])
        self.assertIn("non_adjacent_entry_cancelled", [row["reason"] for row in skipped["books"]["learner"]["skips"]])

    def test_hard_stop_gap_uses_next_observed_open(self):
        state = holding()
        for book in state["books"].values():
            book["positions"]["X"]["armed"] = False
            book["positions"]["X"]["peak"] = 100
        latched = advance(state, frame(price=94))
        filled = advance(latched, frame("2026-10-21T10:20:00-04:00", 80))
        trade = filled["books"]["learner"]["closed"][0]
        self.assertEqual(trade["reason"], "hard_stop")
        self.assertAlmostEqual(trade["exit"], 79.96)

    def test_plan_is_saved_before_the_fill(self):
        state = fresh()
        price = seed(state, "X", "10:10")
        decided = advance(state, frame(price=price))
        book = decided["books"]["learner"]
        self.assertFalse(book["positions"])
        self.assertEqual(book["plans"][0]["hard_stop_fraction"], 0.05)
        self.assertIn("CLOSE", book["plans"][0]["peak_definition"])
        filled = advance(decided, frame("2026-10-21T10:20:00-04:00", price))
        self.assertIn("X", filled["books"]["learner"]["positions"])
        self.assertGreater(filled["books"]["learner"]["costs"], 0)

    def test_open_gap_daily_loss_blocks_queued_buy(self):
        state = holding()
        for book in state["books"].values():
            book["anchor"] = 120000
            book["equity"] = 120000
            book["pending"] = [
                {
                    "side": "buy",
                    "symbol": "Y",
                    "dollars": 1000,
                    "x": [0, 0, 0],
                    "plan_id": "x",
                    "decision_time": "2026-10-21T10:10:00-04:00",
                }
            ]
        extra = {"Y": {"open": 100, "high": 100, "low": 100, "close": 100, "eligible": True}}
        result = advance(state, frame(extra=extra))
        self.assertNotIn("Y", result["books"]["learner"]["positions"])

    def test_missing_held_price_does_not_mutate_state(self):
        state = holding()
        original = json_copy(state)
        broken = frame()
        broken["bars"] = {}
        broken["action_audit"]["symbols"] = []
        with self.assertRaises(ValueError):
            advance(state, broken)
        self.assertEqual(state, original)

    def test_boolean_action_flag_is_not_an_audit(self):
        payload = frame()
        del payload["action_audit"]
        payload["actions_checked"] = True
        with self.assertRaisesRegex(ValueError, "not an independent"):
            advance(fresh(), payload)

    def test_split_adjusted_prices_are_rejected(self):
        payload = frame()
        payload["price_basis"] = "split"
        with self.assertRaisesRegex(ValueError, "raw"):
            advance(fresh(), payload)

    def test_unfinished_bar_is_rejected(self):
        payload = frame()
        payload["as_of"] = payload["timestamp"]
        with self.assertRaisesRegex(ValueError, "not completed"):
            advance(fresh(), payload)

    def test_holiday_and_early_close_clock(self):
        self.assertFalse(is_session("2026-11-26"))
        with self.assertRaisesRegex(ValueError, "not an exchange session"):
            advance(fresh(), at("2026-11-26", "10:15"))
        with self.assertRaisesRegex(ValueError, "early close"):
            advance(fresh(), at("2026-11-27", "13:00"))
        state = fresh()
        clocks = bar_starts("2026-11-27")
        self.assertEqual(len(clocks), 42)
        for clock in clocks[:-1]:
            state = advance(state, at("2026-11-27", clock))
        self.assertEqual(state["completed_sessions"], [])
        state = advance(state, at("2026-11-27", clocks[-1]))
        self.assertEqual(state["completed_sessions"], ["2026-11-27"])

    def test_missing_symbol_bar_breaks_indicator_continuity(self):
        state = fresh()
        seed(state, "Y", "10:10")
        payload = frame("2026-10-21T10:20:00-04:00")
        payload["bars"]["Y"] = dict(payload["bars"]["X"])
        payload["action_audit"]["symbols"] = ["X", "Y"]
        result = advance(state, payload)
        self.assertEqual(len(result["history"]["Y"]), 1)
        self.assertIsNone(features(result["history"]["Y"]))

    def test_split_and_cash_dividend_and_blocked_merger(self):
        split = at("2026-10-21", "09:30", price=60, events=[{"type": "split", "symbol": "X", "ratio": 2}])
        result = advance(holding(), split)
        position = result["books"]["learner"]["positions"]["X"]
        self.assertEqual(position["qty"], 20)
        self.assertEqual(position["entry"], 50)
        self.assertEqual(position["cost"], 1000)
        dividend = at("2026-10-21", "09:30", price=100, events=[{"type": "cash_dividend", "symbol": "X", "amount": 1}])
        paid = advance(holding(), dividend)
        self.assertEqual(paid["books"]["learner"]["dividends"], 10)
        self.assertEqual(paid["books"]["learner"]["cash"], 99010)
        merger = at("2026-10-21", "09:30", events=[{"type": "merger", "symbol": "X"}])
        with self.assertRaisesRegex(ValueError, "merger"):
            advance(holding(), merger)

    def test_naive_and_duplicate_times_rejected(self):
        with self.assertRaises(ValueError):
            advance(fresh(), frame("2026-10-21T10:15:00"))
        state = advance(fresh(), frame())
        with self.assertRaises(ValueError):
            advance(state, frame())

    def test_learning_uses_only_matured_baseline_trades_and_then_freezes(self):
        state = fresh()
        window = list(_ORDERED[:100])
        state["completed_sessions"] = window
        state["books"]["baseline"]["closed"] = [
            {
                "entry_session": "2020-01-01",
                "exit_session": window[50],
                "exit_time": datetime.combine(date.fromisoformat(window[50]), time(10, 0), tzinfo=ET).isoformat(),
                "x": [0, 0, 0],
                "y": 0.01,
            },
            {
                "entry_session": window[98],
                "exit_session": window[99],
                "exit_time": "2027-01-01T10:00:00-05:00",
                "x": [0, 0, 0],
                "y": 999,
            },
        ]
        trained = advance(state, frame())
        self.assertEqual(trained["learning"][0]["samples"], 1)
        self.assertEqual(trained["learning"][0]["fallback"], True)
        self.assertIn("feature_hash", trained["learning"][0])
        self.assertEqual(trained["learning"][0]["portfolio_promotion"], False)
        frozen = advance(trained, frame("2026-10-22T10:15:00-04:00"))
        self.assertEqual(len(frozen["learning"]), 1)

    def test_books_match_without_a_model(self):
        state = fresh()
        price = seed(state, "X", "10:10")
        for step in range(4):
            moment = datetime(2026, 10, 21, 10, 15 + 5 * step, tzinfo=ET)
            state = advance(state, frame(moment, price + 0.1 * step))
        baseline, learner = state["books"].values()
        for key in ("cash", "equity", "costs", "closed"):
            self.assertEqual(baseline[key], learner[key])

    def test_exact_cooldown_survives_restart(self):
        run = Path(__file__).resolve().parents[1] / "runtime" / "test_restart"
        database = run / "paper.sqlite"
        if database.exists():
            database.unlink()
        state = fresh()
        price = seed(state, "X", "10:35")
        state["last"] = datetime(2026, 10, 21, 10, 35, tzinfo=ET).isoformat()
        state["sessions"] = ["2026-10-21"]
        for book in state["books"].values():
            book["cooldown"]["X"] = "2026-10-21T10:45:00-04:00"
        connection, _path = connect("test_restart")
        connection.execute("INSERT OR REPLACE INTO state VALUES(1,?)", (json_dump(state),))
        connection.commit()
        connection.close()
        connection, _path = connect("test_restart")
        try:
            self.assertTrue(ingest(connection, frame(datetime(2026, 10, 21, 10, 40, tzinfo=ET), price)))
            saved = json_load(connection)
            self.assertEqual(saved["books"]["learner"]["pending"], [])
            connection.close()
            connection, _path = connect("test_restart")
            self.assertTrue(ingest(connection, frame(datetime(2026, 10, 21, 10, 45, tzinfo=ET), price + 0.1)))
            saved = json_load(connection)
            self.assertEqual(saved["books"]["learner"]["pending"][0]["side"], "buy")
            self.assertTrue(report(connection, run)["books"]["learner"]["ledger_residual"] == 0)
        finally:
            connection.close()

    def test_revised_frame_and_replay_do_not_double_apply(self):
        run = Path(__file__).resolve().parents[1] / "runtime" / "test_replay"
        database = run / "paper.sqlite"
        if database.exists():
            database.unlink()
        connection, _path = connect("test_replay")
        try:
            self.assertTrue(ingest(connection, frame()))
            self.assertFalse(ingest(connection, frame()))
            changed = frame(price=101)
            with self.assertRaises(ValueError):
                ingest(connection, changed)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM frames").fetchone()[0], 1)
        finally:
            connection.close()

    def test_prediction_check_is_not_a_portfolio_gate(self):
        rows = [
            {
                "x": [(i % 7) / 10, (i % 3) / 100, 0.5],
                "y": 0.2 * ((i % 7) / 10) + 0.01,
                "exit_session": f"2026-01-{i // 2 + 1:02d}",
            }
            for i in range(80)
        ]
        model, info = learn(rows)
        self.assertIsNotNone(model)
        self.assertLess(info["mae"], info["constant_mae"])
        self.assertGreater(model["weights"][0], 0)
        self.assertNotIn("promoted", info["status"])

    def test_market_feed_blocks_without_an_independent_audit(self):
        reasons = blockers(
            {"adjustment": "split", "spy_total_return": False, "vti_total_return": False},
            has_action_ledger=False,
            universe="intraday_cache_snapshot",
        )
        self.assertTrue(any("not raw" in reason for reason in reasons))
        self.assertTrue(any("not a historical S&P" in reason for reason in reasons))
        self.assertEqual(
            blockers(
                {"adjustment": "raw", "spy_total_return": True, "vti_total_return": True},
                has_action_ledger=True,
                universe="explicit_frozen_asof_universe",
            ),
            [],
        )

    def test_feature_and_spec_are_stable(self):
        self.assertEqual(features([100] * 51), [0, 0, 0.5])
        from lab_next.engine import spec_hash

        self.assertEqual(spec_hash(), spec_hash())


def json_copy(value):
    import json

    return json.loads(json.dumps(value))


def json_dump(value):
    import json

    return json.dumps(value)


def json_load(connection):
    import json

    return json.loads(connection.execute("SELECT payload FROM state WHERE id=1").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
