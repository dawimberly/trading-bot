"""Snapshot gates. No network and no writes to the frozen snapshot."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))

from lab.span_audit import adjusted_rate, pit_coverage, rate_against_close, weekday_holes  # noqa: E402


class SpanAuditTests(unittest.TestCase):
    def test_later_split_scales_the_cash_rate(self):
        rate, factor = adjusted_rate(1.0, "2021-06-01", [("2024-06-10", 10.0, 1.0)])
        self.assertEqual(factor, 10.0)
        self.assertAlmostEqual(rate, 0.1)

    def test_holiday_gap_is_not_a_hole(self):
        sessions = ["2021-01-04", "2021-01-05", "2021-01-06", "2021-01-07", "2021-01-08", "2021-01-11"]
        self.assertEqual(weekday_holes(sessions), [])

    def test_long_gap_is_reported(self):
        sessions = ["2021-01-04", "2021-01-15"]
        holes = weekday_holes(sessions)
        self.assertEqual(holes[0]["missing_weekdays"], 8)

    def test_large_rate_is_recorded_as_above_the_close(self):
        self.assertTrue(rate_against_close(0.5, 100.0))
        self.assertFalse(rate_against_close(30.0, 100.0))

    def test_missing_names_stay_in_the_coverage_denominator(self):
        calendar = [f"2022-01-{day:02d}" for day in range(3, 8)]
        intervals = [("KEEP", "2022-01-03", None), ("GONE", "2022-01-03", "2022-06-01")]
        usable = {"KEEP": set(calendar)}
        report = pit_coverage(calendar, intervals, usable)
        self.assertEqual(report["member_sessions"], 10)
        self.assertEqual(report["usable"], 5)
        self.assertFalse(report["passed"])
        symbols = {row["symbol"] for row in report["by_symbol"]}
        self.assertEqual(symbols, {"KEEP", "GONE"})
        self.assertIn("2022", report["by_year"])


if __name__ == "__main__":
    unittest.main()
