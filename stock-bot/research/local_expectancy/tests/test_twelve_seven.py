"""12-to-7 score and the one-knob rank. No market download."""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import simulate  # noqa: E402
from features import ENTRY_QUOTE_MAX_AGE_SECONDS  # noqa: E402
from momentum import last_session_on_or_before, shift_months, twelve_seven_return  # noqa: E402


def _series(n: int) -> list[float]:
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
    cursor = datetime.fromisoformat(start)
    sessions = []
    while len(sessions) < max(len(v) for v in prices.values()):
        if cursor.weekday() < 5:
            sessions.append(cursor.date().isoformat())
        cursor = datetime.fromordinal(cursor.toordinal() + 1)
    bars = {}
    width = max(len(v) for v in prices.values())
    sessions = sessions[:width]
    for symbol, path in prices.items():
        bars[symbol] = {}
        for index, close in enumerate(path):
            opened = path[index - 1] if index else close
            bars[symbol][sessions[index]] = (opened, close)
    return sessions, bars


class CalendarTests(unittest.TestCase):
    def test_month_shift_clamps_the_day(self):
        self.assertEqual(shift_months("2025-10-23", 7), "2025-03-23")
        self.assertEqual(shift_months("2025-10-23", 12), "2024-10-23")
        self.assertEqual(shift_months("2025-03-31", 1), "2025-02-28")

    def test_last_session_is_on_or_before_the_target(self):
        sessions = ["2024-10-22", "2024-10-23", "2024-10-24"]
        self.assertEqual(last_session_on_or_before(sessions, "2024-10-23"), "2024-10-23")
        self.assertEqual(last_session_on_or_before(sessions, "2024-10-25"), "2024-10-24")
        self.assertIsNone(last_session_on_or_before(sessions, "2024-10-21"))

    def test_score_is_the_endpoint_ratio_and_ignores_the_decision_close(self):
        sessions = ["2024-10-23", "2025-03-21", "2025-10-23"]
        bars = {
            "2024-10-23": (80.0, 100.0),
            "2025-03-21": (110.0, 120.0),
            "2025-10-23": (200.0, 250.0),
        }
        self.assertAlmostEqual(twelve_seven_return(sessions, bars, "2025-10-23"), 0.20)
        bars["2025-10-23"] = (200.0, 999.0)
        self.assertAlmostEqual(twelve_seven_return(sessions, bars, "2025-10-23"), 0.20)

    def test_missing_endpoint_is_ineligible(self):
        sessions = ["2024-10-23", "2025-03-21", "2025-10-23"]
        bars = {"2025-03-21": (1.0, 2.0), "2025-10-23": (2.0, 3.0)}
        self.assertIsNone(twelve_seven_return(sessions, bars, "2025-10-23"))


class RankTests(unittest.TestCase):
    def test_learner_uses_momentum_and_baseline_uses_extension(self):
        path = _series(80)
        sessions, bars = _panel({"AAA": path, "BBB": path})

        def momentum_at(symbol, _day):
            return 0.9 if symbol == "BBB" else 0.1

        result = simulate(
            sessions,
            bars,
            ["AAA", "BBB"],
            window_start=sessions[60],
            window_end=sessions[-1],
            slip_bps=5,
            momentum_at=momentum_at,
        )
        self.assertGreater(result["learner"]["trades"], 0)
        self.assertEqual(result["trade_log"]["learner"][0]["symbol"], "BBB")
        self.assertEqual(result["trade_log"]["baseline"][0]["symbol"], "AAA")
        self.assertEqual(result["accepted_refits"], 0)

    def test_quote_cap_is_unchanged(self):
        rules = json.loads((ROOT / "preregistration_12_7.json").read_text(encoding="utf-8"))
        self.assertEqual(ENTRY_QUOTE_MAX_AGE_SECONDS, 420)
        self.assertFalse(rules["promotion"])
        self.assertEqual(rules["windows"]["prior_365"]["through"], "2025-10-23")
        self.assertEqual(rules["windows"]["recent_365"]["through"], "2026-10-23")
