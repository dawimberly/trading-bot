"""Selector timing, the random null, and turnover. No market download."""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pick_quality import (  # noqa: E402
    decision_rows,
    forward_open,
    momentum_score,
    portfolio_book,
    select_symbol,
    signal_change_rate,
    summarize_horizon,
    trade_exits,
    turnover_parts,
)


def _sessions(n: int, start: str = "2024-01-02") -> list[str]:
    cursor = datetime.fromisoformat(start)
    out = []
    while len(out) < n:
        if cursor.weekday() < 5:
            out.append(cursor.date().isoformat())
        cursor = datetime.fromordinal(cursor.toordinal() + 1)
    return out


def _bars(sessions, paths: dict[str, list[float]]):
    bars = {}
    for symbol, closes in paths.items():
        bars[symbol] = {}
        for index, close in enumerate(closes):
            opened = closes[index - 1] if index else close
            bars[symbol][sessions[index]] = (opened, close)
    return bars


class SelectorTests(unittest.TestCase):
    def test_flat_prices_are_eligible_and_the_tie_breaks_alphabetically(self):
        closes = [100.0] * 50
        self.assertAlmostEqual(momentum_score(closes), 0.0)
        self.assertEqual(select_symbol([(0.0, "BBB"), (0.0, "AAA")]), "AAA")

    def test_a_name_at_or_below_the_one_percent_band_is_out(self):
        closes = [100.0] * 49 + [98.0]
        self.assertIsNone(momentum_score(closes))
        closes[-1] = 99.0
        self.assertIsNotNone(momentum_score(closes))

    def test_forward_return_uses_later_opens_not_the_signal_close(self):
        opens = [50.0, 100.0, 110.0, 120.0]
        self.assertAlmostEqual(forward_open(opens, 1, 2), 0.20)
        self.assertIsNone(forward_open(opens, 1, 3))

    def test_the_picked_fill_is_the_next_open(self):
        sessions = _sessions(80)
        slow = [100.0 + i * 0.1 for i in range(80)]
        fast = [100.0 + i * 0.4 for i in range(80)]
        bars = _bars(sessions, {"AAA": slow, "BBB": fast, "VTI": slow})
        rows = decision_rows(
            sessions,
            bars,
            ["AAA", "BBB"],
            window_start=sessions[55],
            window_end=sessions[70],
            horizon=1,
        )
        self.assertTrue(rows)
        self.assertEqual(rows[0]["symbol"], "BBB")
        entry = rows[0]["entry"]
        self.assertEqual(bars["BBB"][entry][0], fast[sessions.index(entry) - 1])
        self.assertNotEqual(rows[0]["pick_return"], 0.0)

    def test_stop_sells_the_next_open_and_the_gap_is_measured(self):
        sessions = _sessions(40)
        closes = [100.0] * 40
        closes[10] = 94.0
        bars = _bars(sessions, {"AAA": closes, "VTI": [100.0] * 40})
        # Open of the crash day is the prior close, 100. The next open is 94.
        bars["AAA"][sessions[11]] = (90.0, 90.0)
        row = {
            "entry_index": 5,
            "symbol": "AAA",
            "pick_return": 0.0,
        }
        # Force entry open to 100.
        bars["AAA"][sessions[5]] = (100.0, 100.0)
        result = trade_exits(sessions, bars, [row], last_index=39)
        self.assertEqual(result["stopped_trades"], 1)
        self.assertAlmostEqual(result["mean_stop_or_30_next_open"], 90.0 / 100.0 - 1.0)
        self.assertAlmostEqual(result["mean_stop_or_30_fantasy_close"], 94.0 / 100.0 - 1.0)
        self.assertAlmostEqual(result["mean_overnight_gap_on_stops"], 90.0 / 94.0 - 1.0)

    def test_identical_names_do_not_clear_the_random_null(self):
        sessions = _sessions(80)
        path = [100.0 + i for i in range(80)]
        bars = _bars(sessions, {"AAA": path, "BBB": path, "VTI": path})
        rows = decision_rows(
            sessions,
            bars,
            ["AAA", "BBB"],
            window_start=sessions[55],
            window_end=sessions[-1],
            horizon=1,
        )
        summary = summarize_horizon(rows, bars, sessions, 1, draws=40, seed=0)
        self.assertAlmostEqual(summary["excess_vs_random_mean"], 0.0)
        self.assertGreater(summary["p_value_vs_random"], 0.5)

    def test_turnover_is_a_window_total_and_an_annualized_rate(self):
        parts = turnover_parts(0.5, 0.5, [1.0, 1.0, 1.0])
        self.assertAlmostEqual(parts["one_way_buy"], 0.5)
        self.assertAlmostEqual(parts["two_sided"], 0.5)
        self.assertAlmostEqual(parts["one_way_buy_annualized"], 0.5 * (252 / 3))
        self.assertAlmostEqual(signal_change_rate(["AAA", "AAA", "BBB"]), 0.5)

    def test_a_full_book_deploys_the_pick_and_a_clip_does_not(self):
        sessions = _sessions(20)
        path = [100.0 * (1.002 ** i) for i in range(20)]
        bars = _bars(sessions, {"AAA": path, "VTI": path})
        decisions = {index: "AAA" for index in range(5, 15)}
        full = portfolio_book(
            sessions,
            bars,
            decisions,
            window_start=sessions[0],
            window_end=sessions[-1],
            clip=1.0,
            name_cap=1.0,
            sleeve=1.0,
            max_names=1,
            use_stop=False,
            use_halt=False,
            slip_bps=0,
        )
        clip = portfolio_book(
            sessions,
            bars,
            decisions,
            window_start=sessions[0],
            window_end=sessions[-1],
            clip=0.095,
            name_cap=0.10,
            sleeve=0.95,
            max_names=10,
            use_stop=False,
            use_halt=False,
            slip_bps=0,
        )
        self.assertGreater(full["turnover"]["one_way_buy"], clip["turnover"]["one_way_buy"])

    def test_preregistration_locks_the_flaws_and_forbids_promotion(self):
        rules = json.loads((ROOT / "preregistration_pick_quality.json").read_text(encoding="utf-8"))
        self.assertFalse(rules["promotion"])
        self.assertEqual(rules["primary"]["horizon"], 20)
        self.assertEqual(rules["fills"]["entry"].split(".")[0], "Open of the next shared session")
        self.assertIn("prospective_250", rules["spent_holdouts"]["clean_evaluation"]["id"])
        self.assertFalse(rules["alpaca_free_sip"]["used_in_this_run"])
        self.assertEqual(rules["turnover"]["two_sided"].split(".")[0][:1], "(")
