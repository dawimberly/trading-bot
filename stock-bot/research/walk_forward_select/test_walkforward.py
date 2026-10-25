"""Synthetic checks for selection, isolation, and fold boundaries. No market data."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ABC = Path(__file__).resolve().parent.parent / "nyse_cash_abc"
sys.path.insert(0, str(ABC))

from engine import Bar, simulate  # noqa: E402

from data_access import schedule
from select import candidates, select_winner
from stitch import simulate_continuous

SLIP = 5.0 / 10_000.0


def bar(o, h, l, c) -> Bar:
    return Bar(float(o), float(h), float(l), float(c))


def rising(n, start=10.0, step=1.0):
    return [bar(p, p + 0.4, p - 0.2, p) for p in [start + step * i for i in range(n)]]


CFG = {
    "grid": {"ma_win": [50, 70, 100], "max_hold": [15, 30, 45], "atr_mult": [1.5, 2.0, 2.5]},
    "baseline_A": {"ma_win": 70, "max_hold": 30, "atr_mult": 2.0},
    "eligibility": {"max_drawdown_at_most": 0.25, "min_closed_trades": 100},
    "warmup_sessions": 115,
    "train_sessions": 504,
    "eval_sessions": 63,
    "folds": 4,
}


def scored(**kwargs):
    row = {
        "order": 0,
        "ma_win": 70,
        "max_hold": 30,
        "atr_mult": 2.0,
        "net_return": 0.1,
        "max_drawdown": 0.1,
        "turnover": 1.0,
        "trades": 120,
        "reconciled": True,
        "distribution_unpriced": 0,
    }
    row.update(kwargs)
    return row


class SelectionTests(unittest.TestCase):
    def test_grid_contains_baseline_once(self):
        rows = candidates(CFG)
        self.assertEqual(len(rows), 27)
        hits = [r for r in rows if (r["ma_win"], r["max_hold"], r["atr_mult"]) == (70, 30, 2.0)]
        self.assertEqual(len(hits), 1)

    def test_tie_break_and_cash_fallback(self):
        rows = [
            scored(order=5, ma_win=100, net_return=0.2, max_drawdown=0.2, turnover=3),
            scored(order=1, ma_win=50, net_return=0.2, max_drawdown=0.2, turnover=3),
            scored(order=2, ma_win=70, net_return=0.2, max_drawdown=0.1, turnover=9),
            scored(order=3, net_return=0.5, max_drawdown=0.26, trades=200),
            scored(order=4, net_return=0.9, trades=99),
        ]
        choice = select_winner(rows, CFG)
        self.assertEqual(choice["selected"]["order"], 2)
        blocked = [scored(net_return=0.4, max_drawdown=0.3), scored(trades=10)]
        self.assertEqual(select_winner(blocked, CFG)["choice"], "cash")

    def test_unreconciled_is_not_eligible(self):
        choice = select_winner([scored(reconciled=False, net_return=5)], CFG)
        self.assertEqual(choice["choice"], "cash")
        self.assertEqual(choice["candidates"][0]["reject_reason"], "unreconciled")


class BookTests(unittest.TestCase):
    def test_constant_schedule_matches_engine(self):
        n = 8
        dates = [f"2024-01-{i+1:02d}" for i in range(n)]
        bars = {"AAA": rising(n), "BBB": rising(n, 20, 0.5)}
        member = {s: [True] * n for s in bars}
        spec = {"ma_win": 2, "max_hold": 3, "atr_mult": 2.0}
        schedule_rows = [None, None] + [spec] * (n - 2)
        left = simulate(
            bars, member, dates, [100.0] * n,
            arm="A", decision_start=2, ma_win=2, atr_period=1, atr_mult=2.0,
            max_hold=3, max_names=1, sleeve=0.95, initial_cash=10_000, slip=SLIP,
        )
        right = simulate_continuous(
            bars, member, dates, [100.0] * n, schedule_rows,
            decision_start=2, atr_period=1, max_names=1, sleeve=0.95,
            initial_cash=10_000, slip=SLIP,
        )
        self.assertTrue(left["reconciled"])
        self.assertTrue(right["reconciled"])
        self.assertAlmostEqual(left["ending_equity"], right["ending_equity"], places=6)
        self.assertAlmostEqual(left["max_drawdown"], right["max_drawdown"], places=8)
        self.assertEqual(left["trades"], right["trades"])
        self.assertAlmostEqual(left["cost_dollars"], right["cost_dollars"], places=6)
        for a, b in zip(left["trade_log"], right["trade_log"]):
            self.assertEqual((a["symbol"], a["entry_date"], a["exit_date"], a["reason"]), (b["symbol"], b["entry_date"], b["exit_date"], b["reason"]))
            self.assertAlmostEqual(a["pnl"], b["pnl"], places=6)

    def test_warmup_mark_is_still_cash_and_hold_survives_the_boundary(self):
        n = 9
        dates = [f"d{i}" for i in range(n)]
        bars = {"AAA": rising(n), "BBB": rising(n, 30, 1)}
        member = {"AAA": [True] * n, "BBB": [False, False, False, False, True, True, True, True, True]}
        old = {"ma_win": 2, "max_hold": 30, "atr_mult": 2.0}
        new = {"ma_win": 2, "max_hold": 1, "atr_mult": 1.5}
        # Sessions 2 and 3 use the long hold. Session 4 onward uses the short hold.
        schedule_rows = [None, None, old, old, new, new, new, new, new]
        out = simulate_continuous(
            bars, member, dates, [100.0] * n, schedule_rows,
            decision_start=2, atr_period=1, max_names=2, sleeve=0.95,
            initial_cash=10_000, slip=SLIP,
        )
        self.assertAlmostEqual(out["warmup_mark"], 10_000)
        self.assertTrue(out["reconciled"])
        held = [t for t in out["trade_log"] if t["symbol"] == "AAA"]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["entry_max_hold"], 30)
        self.assertGreater(held[0]["bars_held"], 1)
        self.assertEqual(held[0]["reason"], "final")
        later = [t for t in out["trade_log"] if t["symbol"] == "BBB"]
        self.assertGreaterEqual(len(later), 1)
        self.assertTrue(all(t["entry_max_hold"] == 1 for t in later))
        self.assertEqual(later[0]["reason"], "time")
        self.assertEqual(later[0]["bars_held"], 1)
        self.assertEqual(later[0]["entry_date"], "d5")
        self.assertNotEqual(held[0]["exit_date"], "d4")

    def test_pending_order_keeps_the_queuing_parameters(self):
        n = 7
        dates = [f"d{i}" for i in range(n)]
        bars = {"AAA": rising(n)}
        member = {"AAA": [True] * n}
        old = {"ma_win": 2, "max_hold": 30, "atr_mult": 2.0}
        new = {"ma_win": 2, "max_hold": 30, "atr_mult": 9.0}
        # Decision on d2 queues the only buy. d3 is the new fold and is the fill.
        schedule_rows = [None, None, old, new, new, new, new]
        out = simulate_continuous(
            bars, member, dates, [100.0] * n, schedule_rows,
            decision_start=2, atr_period=1, max_names=1, sleeve=1.0,
            initial_cash=10_000, slip=0.0,
        )
        trade = out["trade_log"][0]
        self.assertEqual(trade["entry_date"], "d3")
        self.assertEqual(trade["entry_atr_mult"], 2.0)
        self.assertEqual(trade["signal_date"], "d2")

    def test_cash_fold_makes_no_entries(self):
        n = 6
        dates = [f"d{i}" for i in range(n)]
        bars = {"AAA": rising(n)}
        out = simulate_continuous(
            bars, {"AAA": [True] * n}, dates, [100.0] * n, [None] * n,
            decision_start=2, atr_period=1, max_names=1, initial_cash=10_000,
        )
        self.assertEqual(out["trades"], 0)
        self.assertAlmostEqual(out["ending_equity"], 10_000)

    def test_training_dates_stop_at_the_cutoff(self):
        calendar = [f"{i:04d}" for i in range(900)]
        plan = schedule(calendar, CFG)
        self.assertEqual(len(plan["folds"]), 4)
        seen = []
        for fold in plan["folds"]:
            self.assertEqual(len(fold["train_scored"]), 504)
            self.assertEqual(len(fold["eval"]), 63)
            self.assertEqual(len(fold["train_dates"]), 115 + 504)
            self.assertLess(fold["cutoff"], fold["eval"][0])
            self.assertTrue(all(day <= fold["cutoff"] for day in fold["train_dates"]))
            seen.append(fold["eval"][0])
        self.assertEqual(seen, sorted(seen))
        for left, right in zip(plan["folds"], plan["folds"][1:]):
            self.assertLess(left["eval"][-1], right["eval"][0])


if __name__ == "__main__":
    unittest.main()
