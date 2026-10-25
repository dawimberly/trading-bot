"""Frozen-rule tests. No market data and no broker."""

from __future__ import annotations

import copy
import json
import random
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import hold_nav, simulate  # noqa: E402
from features import ENTRY_QUOTE_MAX_AGE_SECONDS, entry_quote_is_fresh, features  # noqa: E402
from gates import study_status, window_failures  # noqa: E402
from learner import learn, predict  # noqa: E402
from yahoo import parse_chart  # noqa: E402


def _rows():
    rng = random.Random(12)
    rows = []
    for day in range(1, 25):
        for _ in range(4):
            extension = rng.uniform(0.001, 0.08)
            rows.append(
                {
                    "x": [extension, 0.002, 0.6],
                    "y": extension * 2 - 0.03,
                    "entry_session": f"2025-01-{day:02d}",
                    "entry_time": f"2025-01-{day:02d}T10:00:00-05:00",
                    "exit_session": f"2025-01-{day:02d}",
                    "exit_time": f"2025-01-{day:02d}T15:00:00-05:00",
                }
            )
    return rows


def _series(n: int) -> list[float]:
    """Rally, then chop, so extension stays positive and RSI stays below 70."""
    price = 100.0
    out = []
    for i in range(n):
        if i < 36:
            price *= 1.012
        else:
            price *= 0.997 if i % 2 else 1.004
        out.append(price)
    return out


def _panel(prices: dict[str, list[float]], start: str = "2024-01-02"):
    day0 = datetime.fromisoformat(start)
    sessions = []
    cursor = day0
    while len(sessions) < max(len(v) for v in prices.values()):
        if cursor.weekday() < 5:
            sessions.append(cursor.date().isoformat())
        cursor = cursor.fromordinal(cursor.toordinal() + 1)
    bars = {}
    for symbol, path in prices.items():
        bars[symbol] = {}
        for index, close in enumerate(path):
            opened = path[index - 1] if index else close
            bars[symbol][sessions[index]] = (opened, close)
    return sessions[: len(path)], bars


class FeatureTests(unittest.TestCase):
    def test_quote_cap_is_seven_minutes(self):
        self.assertEqual(ENTRY_QUOTE_MAX_AGE_SECONDS, 420)
        self.assertTrue(entry_quote_is_fresh(420))
        self.assertFalse(entry_quote_is_fresh(421))

    def test_features_ignore_anything_before_the_last_51_closes(self):
        base = [100 + i * 0.1 for i in range(51)]
        self.assertEqual(features(base), features([50] * 20 + base))
        longer = base + [130]
        self.assertNotEqual(features(base), features(longer))
        self.assertEqual(features(base), features(longer[:-1]))

    def test_flat_extension_is_zero(self):
        values = features([20.0] * 51)
        self.assertAlmostEqual(values[0], 0.0)
        self.assertAlmostEqual(values[2], 0.5)


class LearnerTests(unittest.TestCase):
    def test_known_slope_is_accepted_and_ranks(self):
        rows = _rows()
        model, info = learn(rows)
        self.assertEqual(info["status"], "accepted_prediction_model")
        self.assertGreater(predict(model, [0.07, 0.002, 0.6]), predict(model, [0.01, 0.002, 0.6]))

    def test_constant_label_is_not_strictly_better_than_the_mean(self):
        rows = _rows()
        for row in rows:
            row["y"] = 0.01
        model, info = learn(rows)
        self.assertIsNone(model)
        self.assertEqual(info["status"], "rejected_prediction_model")

    def test_purge_drops_train_exits_that_overlap_validation_entries(self):
        rows = _rows()
        for row in rows:
            if row["exit_session"] > "2025-01-14":
                row["entry_time"] = "2025-01-01T10:00:00-05:00"
        model, info = learn(rows)
        self.assertIsNone(model)
        self.assertEqual(info["train"], 0)
        self.assertEqual(info["status"], "insufficient_purged_split")

    def test_learn_does_not_mutate_rows(self):
        rows = _rows()
        before = copy.deepcopy(rows)
        learn(rows)
        self.assertEqual(rows, before)


class EngineTests(unittest.TestCase):
    def test_stale_quote_cancels_entries(self):
        path = _series(80)
        sessions, bars = _panel({"AAA": path, "BBB": path})
        start, end = sessions[60], sessions[-1]
        result = simulate(
            sessions,
            bars,
            ["AAA", "BBB"],
            window_start=start,
            window_end=end,
            slip_bps=5,
            quote_age_seconds=15 * 60,
        )
        self.assertFalse(result["fresh_quotes"])
        self.assertEqual(result["learner"]["trades"], 0)
        self.assertEqual(result["baseline"]["trades"], 0)
        self.assertAlmostEqual(result["learner"]["return"], 0.0)

    def test_without_a_model_the_books_match(self):
        path = _series(80)
        other = _series(80)
        other = [price * 0.98 for price in other]
        sessions, bars = _panel({"AAA": path, "BBB": other})
        result = simulate(
            sessions,
            bars,
            ["AAA", "BBB"],
            window_start=sessions[60],
            window_end=sessions[-1],
            slip_bps=5,
        )
        self.assertGreater(result["baseline"]["trades"], 0)
        self.assertEqual(result["learner"]["trades"], result["baseline"]["trades"])
        self.assertAlmostEqual(result["learner"]["return"], result["baseline"]["return"])
        self.assertEqual(result["accepted_refits"], 0)

    def test_fill_is_the_next_open_not_the_signal_close(self):
        path = _series(80)
        sessions, bars = _panel({"AAA": path, "BBB": [price * 0.99 for price in path]})
        result = simulate(
            sessions,
            bars,
            ["AAA", "BBB"],
            window_start=sessions[60],
            window_end=sessions[-1],
            slip_bps=5,
        )
        trade = result["trade_log"]["baseline"][0]
        entry_index = sessions.index(trade["entry_session"])
        signal_close = bars[trade["symbol"]][sessions[entry_index - 1]][1]
        next_open = bars[trade["symbol"]][trade["entry_session"]][0]
        self.assertAlmostEqual(trade["entry_fill"], next_open * 1.0005)
        self.assertNotAlmostEqual(trade["entry_fill"], signal_close)

    def test_later_close_does_not_change_earlier_entries(self):
        path = _series(90)
        sessions, bars = _panel({"AAA": path, "BBB": [price * 0.99 for price in path]})
        shocked = copy.deepcopy(bars)
        shock_day = sessions[80]
        opened, close = shocked["AAA"][shock_day]
        shocked["AAA"][shock_day] = (opened, close * 3)
        common = dict(
            symbols=["AAA", "BBB"],
            window_start=sessions[70],
            window_end=sessions[-1],
            slip_bps=5,
        )
        left = simulate(sessions, bars, **common)
        right = simulate(sessions, shocked, **common)

        def entries(log):
            return [
                (row["symbol"], row["entry_session"], row["entry_fill"])
                for row in log
                if row["entry_session"] <= shock_day
            ]

        self.assertEqual(entries(left["trade_log"]["baseline"]), entries(right["trade_log"]["baseline"]))
        self.assertGreater(len(entries(left["trade_log"]["baseline"])), 0)

    def test_more_slippage_lowers_a_hold(self):
        path = _series(30)
        sessions, bars = _panel({"AAA": path})
        cheap = hold_nav(sessions, bars, ["AAA"], 0, len(sessions) - 1, 0.0005)
        costly = hold_nav(sessions, bars, ["AAA"], 0, len(sessions) - 1, 0.001)
        self.assertGreater(cheap[-1], costly[-1])


class GateTests(unittest.TestCase):
    def _block(self, learner_return: float) -> dict:
        learner = {
            "return": learner_return,
            "max_drawdown": 0.10,
            "first_half": 0.04,
            "second_half": 0.05,
            "bootstrap": {"median": 0.02},
        }
        return {
            "learner": learner,
            "baseline": {"return": 0.05, "max_drawdown": 0.11, "first_half": 0.02, "second_half": 0.03},
            "equal_weight_hold": {"return": 0.04},
            "vti_hold": {"return": 0.03},
            "monday": {"return": 0.08},
        }

    def test_clear_cell_passes_and_a_loss_rejects_the_study(self):
        self.assertEqual(window_failures(self._block(0.09)), [])
        windows = {
            "prior_365": {"slip_5": self._block(0.02), "slip_10": self._block(0.09)},
            "recent_365": {"slip_5": self._block(0.09), "slip_10": self._block(0.09)},
        }
        verdict = study_status(windows)
        self.assertEqual(verdict["status"], "reject")
        self.assertFalse(verdict["promotion"])

    def test_drawdown_slack_is_two_points_not_more(self):
        block = self._block(0.09)
        block["learner"]["max_drawdown"] = 0.14
        block["baseline"]["max_drawdown"] = 0.11
        self.assertIn("drawdown", window_failures(block))
        block["learner"]["max_drawdown"] = 0.13
        block["baseline"]["max_drawdown"] = 0.11
        self.assertNotIn("drawdown", window_failures(block))


class YahooTests(unittest.TestCase):
    def test_parse_scales_ohlc_by_the_adjustment_and_drops_weekends(self):
        # 2024-01-05 14:30 UTC is a Friday. 2024-01-06 14:30 UTC is a Saturday.
        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": [1704465000, 1704551400],
                        "indicators": {
                            "quote": [{"open": [100.0, 50.0], "close": [110.0, 55.0]}],
                            "adjclose": [{"adjclose": [88.0, 44.0]}],
                        },
                    }
                ]
            }
        }
        bars = parse_chart(payload)
        self.assertEqual(list(bars), ["2024-01-05"])
        opened, close = bars["2024-01-05"]
        self.assertAlmostEqual(close, 88.0)
        self.assertAlmostEqual(opened, 80.0)


class PublicationTests(unittest.TestCase):
    def test_preregistration_locks_the_seven_minute_gate_and_forbids_promotion(self):
        rules = json.loads((ROOT / "preregistration.json").read_text(encoding="utf-8"))
        self.assertEqual(rules["execution"]["entry_quote_max_age_seconds"], 420)
        self.assertFalse(rules["promotion"])
        self.assertEqual(rules["pass_rule"]["max_drawdown_not_worse_by_more_than_pp"], 2.0)
        self.assertIn("prior_365", rules["windows"]["required"])
        self.assertIn("recent_365", rules["windows"]["required"])

    def test_sources_do_not_reference_live_order_paths(self):
        for path in ROOT.glob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            self.assertNotIn("execute_order", text)
            self.assertNotIn("alpaca_live", text)
            self.assertNotIn("alpaca_paper_v2", text)
            self.assertNotIn("ufc-predictor", text)


if __name__ == "__main__":
    unittest.main()
