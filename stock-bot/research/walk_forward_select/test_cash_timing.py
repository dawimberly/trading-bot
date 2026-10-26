"""Payment-date cash versus ex-date cash. No market data and no network."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ABC = Path(__file__).resolve().parent.parent / "nyse_cash_abc"
sys.path.insert(0, str(ABC))

from engine import Bar  # noqa: E402

from cash_lots import cash_session  # noqa: E402
from metrics import spy_total_return_paid  # noqa: E402
from stitch import simulate_continuous  # noqa: E402

SLIP = 5.0 / 10_000.0


def bar(px: float) -> Bar:
    return Bar(px, px + 0.4, px - 0.2, px)


def book(n: int, lots, *, end=None):
    dates = [f"2024-01-{i+1:02d}" for i in range(n)]
    bars = {"AAA": [bar(10.0 + i) for i in range(n)]}
    spec = {"ma_win": 2, "max_hold": 30, "atr_mult": 2.0}
    schedule = [None, None] + [spec] * (n - 2)
    out = simulate_continuous(
        bars,
        {"AAA": [True] * n},
        dates,
        [100.0] * n,
        schedule,
        decision_start=2,
        atr_period=1,
        max_names=1,
        sleeve=1.0,
        initial_cash=10_000,
        slip=0.0,
        dividend_lots=lots,
    )
    return dates, out


class PaymentTimingTests(unittest.TestCase):
    def test_payable_before_ex_is_not_credited_early(self):
        pay, flag = cash_session("2023-03-30", "2023-03-17", ["2023-03-17", "2023-03-30"])
        self.assertEqual(pay, "2023-03-30")
        self.assertEqual(flag, "payable_before_ex")
    def test_receivable_is_not_spendable_until_the_payable_session(self):
        dates, out = book(
            8,
            [{"symbol": "AAA", "ex_session": "2024-01-06", "pay_session": "2024-01-08", "rate": 1.0}],
        )
        self.assertTrue(out["reconciled"])
        self.assertGreater(out["dividend_dollars"], 0)
        self.assertAlmostEqual(out["dividend_unpaid"], 0.0)
        self.assertAlmostEqual(out["dividend_received"], out["dividend_dollars"])
        self.assertAlmostEqual(out["ending_cash"], out["ending_equity"])
        _dates, plain = book(8, [])
        ex_i = dates.index("2024-01-06") - 2
        self.assertAlmostEqual(out["equity_path"][ex_i] - plain["equity_path"][ex_i], out["dividend_dollars"], places=6)
        pay_i = dates.index("2024-01-08") - 2
        self.assertAlmostEqual(out["equity_path"][pay_i] - plain["equity_path"][pay_i], out["dividend_dollars"], places=4)

    def test_unpaid_receivable_remains_in_terminal_equity(self):
        _dates, out = book(
            7,
            [{"symbol": "AAA", "ex_session": "2024-01-06", "pay_session": "2024-10-30", "rate": 1.0}],
        )
        self.assertTrue(out["reconciled"])
        self.assertAlmostEqual(out["dividend_received"], 0.0)
        self.assertGreater(out["dividend_unpaid"], 0)
        self.assertAlmostEqual(out["ending_equity"] - out["ending_cash"], out["dividend_unpaid"])

    def test_same_day_payable_matches_immediate_cash(self):
        dates = [f"2024-01-{i+1:02d}" for i in range(8)]
        bars = {"AAA": [bar(10.0 + i) for i in range(8)]}
        spec = {"ma_win": 2, "max_hold": 30, "atr_mult": 2.0}
        schedule = [None, None] + [spec] * 6
        common = dict(
            bars=bars,
            member={"AAA": [True] * 8},
            dates=dates,
            spy=[100.0] * 8,
            schedule=schedule,
            decision_start=2,
            atr_period=1,
            max_names=1,
            sleeve=1.0,
            initial_cash=10_000,
            slip=0.0,
        )
        immediate = simulate_continuous(
            **common,
            dividends={"AAA": {"2024-01-06": 1.0}},
        )
        paid = simulate_continuous(
            **common,
            dividend_lots=[{"symbol": "AAA", "ex_session": "2024-01-06", "pay_session": "2024-01-06", "rate": 1.0}],
        )
        self.assertTrue(immediate["reconciled"] and paid["reconciled"])
        self.assertAlmostEqual(immediate["ending_equity"], paid["ending_equity"], places=6)
        self.assertEqual(immediate["trades"], paid["trades"])

    def test_spy_does_not_reinvest_before_the_payable_date(self):
        # Buy on 2026-09-17. The 2026-09-18 ex-date is for shares already held.
        bars = {
            "2026-09-16": bar(100),
            "2026-09-17": bar(100),
            "2026-09-18": bar(99),
            "2026-10-14": bar(110),
        }
        held = spy_total_return_paid(
            ["2026-09-16", "2026-09-17", "2026-09-18", "2026-10-14"],
            bars,
            [{"ex_session": "2026-09-18", "pay_session": "2026-10-30", "rate": 1.0}],
            slip=0.0,
            initial_cash=10_000,
        )
        self.assertEqual(held["dividend_events"], 0)
        self.assertAlmostEqual(held["dividend_unpaid"], 100.0, places=4)
        self.assertGreater(held["ending_equity"], 11_000)
        reinvested = spy_total_return_paid(
            ["2026-09-16", "2026-09-17", "2026-09-18", "2026-10-30"],
            {**bars, "2026-10-30": bar(110)},
            [{"ex_session": "2026-09-18", "pay_session": "2026-10-30", "rate": 1.0}],
            slip=0.0,
            initial_cash=10_000,
        )
        self.assertEqual(reinvested["dividend_events"], 1)
        self.assertEqual(reinvested["dividends"][0]["session"], "2026-10-30")
        self.assertAlmostEqual(reinvested["dividend_unpaid"], 0.0)
        self.assertTrue(all(row["session"] != "2026-09-18" for row in reinvested["executions"] if row["kind"] == "buy" and row["dollars"] < 10_000))


if __name__ == "__main__":
    unittest.main()
