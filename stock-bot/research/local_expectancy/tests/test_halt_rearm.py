"""Recovering halt is a 21-session cooldown. Sticky stays stuck. No market download."""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import HALT_COOLDOWN_SESSIONS, simulate  # noqa: E402


def _prices(n: int, crash_at: int) -> list[float]:
    price = 100.0
    out = []
    for i in range(n):
        if i == crash_at:
            price *= 0.70
        elif i > crash_at:
            # Slight upward chop so extension can turn positive without RSI at 100.
            price *= 0.997 if i % 2 else 1.006
        elif i < 36:
            price *= 1.012
        else:
            price *= 0.997 if i % 2 else 1.004
        out.append(price)
    return out


def _panel(prices: dict[str, list[float]], start: str = "2024-01-02"):
    cursor = datetime.fromisoformat(start)
    width = max(len(v) for v in prices.values())
    sessions = []
    while len(sessions) < width:
        if cursor.weekday() < 5:
            sessions.append(cursor.date().isoformat())
        cursor = datetime.fromordinal(cursor.toordinal() + 1)
    sessions = sessions[:width]
    bars = {}
    for symbol, path in prices.items():
        bars[symbol] = {}
        for index, close in enumerate(path):
            opened = path[index - 1] if index else close
            bars[symbol][sessions[index]] = (opened, close)
    return sessions, bars


class HaltTests(unittest.TestCase):
    def test_cooldown_rearms_after_21_sessions_and_sticky_does_not(self):
        crash_at = 90
        path = _prices(220, crash_at)
        symbols = [f"N{i:02d}" for i in range(10)]
        sessions, bars = _panel({symbol: list(path) for symbol in symbols})
        common = dict(
            symbols=symbols,
            window_start=sessions[60],
            window_end=sessions[-1],
            slip_bps=5,
        )
        sticky = simulate(sessions, bars, halt_mode="sticky", **common)
        cooled = simulate(sessions, bars, halt_mode="cooldown_21", **common)
        crash_day = sessions[crash_at]

        self.assertGreater(sticky["learner"]["halt_triggers"], 0)
        self.assertEqual(sticky["learner"]["rearms"], 0)
        self.assertGreaterEqual(cooled["learner"]["rearms"], 1)
        self.assertEqual(HALT_COOLDOWN_SESSIONS, 21)

        sticky_after = [
            row["entry_session"]
            for row in sticky["trade_log"]["learner"]
            if row["entry_session"] > crash_day
        ]
        cooled_after = [
            row["entry_session"]
            for row in cooled["trade_log"]["learner"]
            if row["entry_session"] > crash_day
        ]
        self.assertEqual(sticky_after, [])
        self.assertTrue(cooled_after)
        rearm_day = cooled["learner"]["rearm_days"][0]
        self.assertGreaterEqual(sessions.index(rearm_day) - sessions.index(cooled["learner"]["halt_trigger_days"][0]), 21)
        self.assertTrue(all(day >= rearm_day for day in cooled_after))

    def test_preregistration_locks_the_cooldown_and_forbids_promotion(self):
        rules = json.loads((ROOT / "preregistration_halt_rearm.json").read_text(encoding="utf-8"))
        self.assertEqual(rules["halt"]["mode"], "cooldown_21")
        self.assertEqual(rules["halt"]["cooldown_sessions"], 21)
        self.assertEqual(rules["rank"], "twelve_seven")
        self.assertFalse(rules["promotion"])
        self.assertEqual(rules["windows"]["prior_365"]["through"], "2025-10-23")
        self.assertEqual(rules["windows"]["recent_365"]["through"], "2026-10-23")
