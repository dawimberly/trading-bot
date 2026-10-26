"""Synthetic checks for the cash-ledger walk. No market data and no network."""

from __future__ import annotations

import unittest

from engine import Bar, Distribution, adjusted_dividend_rate, realized_vol, simulate

SLIP = 5.0 / 10_000.0


def bar(o, h, l, c) -> Bar:
    return Bar(float(o), float(h), float(l), float(c))


def always(n, symbols):
    return {s: [True] * n for s in symbols}


def spy_flat(n, px=100.0):
    return [px] * n


class EngineTests(unittest.TestCase):
    def test_dividend_rate_uses_later_splits_only(self):
        splits = [("2024-06-10", 10.0)]
        self.assertAlmostEqual(adjusted_dividend_rate(0.04, "2024-03-05", splits), 0.004)
        self.assertAlmostEqual(adjusted_dividend_rate(0.01, "2024-06-11", splits), 0.01)
        self.assertAlmostEqual(adjusted_dividend_rate(1.0, "2020-01-01", [("2021-01-01", 0.1)]), 10.0)

    def test_vol_ignores_closes_after_the_decision(self):
        import numpy as np

        base = np.array([100, 101, 100, 102, 101, 103], dtype=float)
        later = base.copy()
        later[-1] = 50
        # Index 4 is the last shared close. A crash at index 5 must not change it.
        self.assertAlmostEqual(realized_vol(base, 4, 3), realized_vol(later, 4, 3))
        self.assertNotAlmostEqual(realized_vol(base, 5, 3), realized_vol(later, 5, 3))

    def test_no_same_close_fill_and_costs_reconcile(self):
        dates = [f"2024-01-0{i+1}" for i in range(6)]
        px = [10, 11, 12, 13, 14, 15]
        bars = {
            "AAA": [
                bar(p, p + 0.4, p - 0.2, p) for p in px
            ]
        }
        out = simulate(
            bars, always(6, ["AAA"]), dates, spy_flat(6),
            arm="A", decision_start=2, ma_win=2, atr_period=1, atr_mult=2,
            max_hold=30, max_names=1, sleeve=0.95, initial_cash=10_000,
        )
        self.assertTrue(out["reconciled"])
        self.assertGreater(out["cost_dollars"], 0)
        self.assertGreaterEqual(len(out["trade_log"]), 1)
        first = out["trade_log"][0]
        self.assertEqual(first["signal_date"], "2024-01-03")
        self.assertEqual(first["entry_date"], "2024-01-04")
        self.assertNotEqual(first["signal_date"], first["entry_date"])
        self.assertAlmostEqual(first["entry_fill"], 13 * (1 + SLIP), places=6)
        self.assertAlmostEqual(out["ending_cash"], out["ending_equity"], places=4)

    def test_sale_proceeds_fund_a_later_purchase_not_the_exit_morning(self):
        dates = [f"d{i}" for i in range(6)]
        aaa = [10, 11, 12, 13, 14, 15]
        bbb = [10, 10.5, 11, 11.5, 12, 12.5]
        bars = {
            "AAA": [bar(p, p + 0.3, p - 0.2, p) for p in aaa],
            "BBB": [bar(p, p + 0.3, p - 0.2, p) for p in bbb],
        }
        # Gap AAA through the stop on d4. Open is far below any 2x ATR stop.
        bars["AAA"][4] = bar(1, 1.2, 0.8, 1.1)
        out = simulate(
            bars, always(6, ["AAA", "BBB"]), dates, spy_flat(6),
            arm="A", decision_start=2, ma_win=2, atr_period=1,
            max_hold=30, max_names=1, sleeve=0.95, initial_cash=10_000,
        )
        self.assertTrue(out["reconciled"])
        by_sym = {}
        for trade in out["trade_log"]:
            by_sym.setdefault(trade["symbol"], []).append(trade)
        self.assertIn("AAA", by_sym)
        self.assertEqual(by_sym["AAA"][0]["reason"], "stop_gap")
        self.assertEqual(by_sym["AAA"][0]["exit_date"], "d4")
        bbb_entries = [t["entry_date"] for t in by_sym.get("BBB", [])]
        self.assertNotIn("d4", bbb_entries)
        self.assertIn("d5", bbb_entries)
        self.assertGreater(by_sym["BBB"][0]["shares"] * by_sym["BBB"][0]["entry_fill"], 1000)

    def test_same_symbol_reentry_is_the_next_open_after_the_exit_close(self):
        dates = [f"d{i}" for i in range(8)]
        px = [10, 11, 12, 13, 14, 15, 16, 17]
        bars = {"AAA": [bar(p, p + 0.3, p - 0.2, p) for p in px]}
        out = simulate(
            bars, always(8, ["AAA"]), dates, spy_flat(8),
            arm="A", decision_start=2, ma_win=2, atr_period=1,
            max_hold=2, max_names=1, sleeve=0.95, initial_cash=10_000,
        )
        self.assertTrue(out["reconciled"])
        trades = out["trade_log"]
        self.assertGreaterEqual(len(trades), 2)
        self.assertEqual(trades[0]["symbol"], "AAA")
        self.assertEqual(trades[0]["reason"], "time")
        self.assertEqual(trades[0]["entry_date"], "d3")
        self.assertEqual(trades[0]["exit_date"], "d5")
        self.assertEqual(trades[1]["symbol"], "AAA")
        self.assertEqual(trades[1]["signal_date"], "d5")
        self.assertEqual(trades[1]["entry_date"], "d6")
        self.assertNotEqual(trades[1]["entry_date"], trades[0]["exit_date"])

    def test_gap_and_ambiguous_bar_fills(self):
        dates = [f"d{i}" for i in range(5)]
        # Quiet entry, then a bar that trades through both stop and target.
        both = {
            "AAA": [
                bar(10, 10.2, 9.9, 10),
                bar(11, 11.2, 10.8, 11),
                bar(12, 12.2, 11.8, 12),
                bar(13, 13.2, 12.8, 13),
                bar(13, 20, 1, 13),
            ]
        }
        amb = simulate(
            both, always(5, ["AAA"]), dates, spy_flat(5),
            arm="C", decision_start=2, ma_win=2, atr_period=1,
            max_hold=30, max_names=1, initial_cash=10_000,
        )
        self.assertEqual(amb["trade_log"][0]["reason"], "stop_same_bar")
        self.assertAlmostEqual(
            amb["trade_log"][0]["exit_raw"], amb["trade_log"][0]["entry_fill"] - 2 * 1.0, places=4
        )

        gap_up = {
            "AAA": [
                bar(10, 10.2, 9.9, 10),
                bar(11, 11.2, 10.8, 11),
                bar(12, 12.2, 11.8, 12),
                bar(13, 13.2, 12.8, 13),
                bar(20, 21, 19, 20.5),
            ]
        }
        up = simulate(
            gap_up, always(5, ["AAA"]), dates, spy_flat(5),
            arm="C", decision_start=2, ma_win=2, atr_period=1,
            max_hold=30, max_names=1, initial_cash=10_000,
        )
        self.assertEqual(up["trade_log"][0]["reason"], "target_gap")
        self.assertEqual(up["trade_log"][0]["exit_raw"], 20)
        self.assertAlmostEqual(up["trade_log"][0]["exit_fill"], 20 * (1 - SLIP), places=6)

        gap_dn = {
            "AAA": [
                bar(10, 10.2, 9.9, 10),
                bar(11, 11.2, 10.8, 11),
                bar(12, 12.2, 11.8, 12),
                bar(13, 13.2, 12.8, 13),
                bar(1, 1.2, 0.9, 1.1),
            ]
        }
        dn = simulate(
            gap_dn, always(5, ["AAA"]), dates, spy_flat(5),
            arm="A", decision_start=2, ma_win=2, atr_period=1,
            max_hold=30, max_names=1, initial_cash=10_000,
        )
        self.assertEqual(dn["trade_log"][0]["reason"], "stop_gap")
        self.assertEqual(dn["trade_log"][0]["exit_raw"], 1)
        self.assertTrue(dn["reconciled"] and up["reconciled"] and amb["reconciled"])

    def test_pit_membership_and_missing_bar_block_the_trade(self):
        dates = [f"d{i}" for i in range(5)]
        px = [10, 11, 12, 13, 14]
        good = [bar(p, p + 0.2, p - 0.1, p) for p in px]
        missing = good.copy()
        missing[2] = None
        member = always(5, ["AAA", "BBB"])
        member["AAA"] = [True, True, False, False, False]
        bars = {"AAA": good, "BBB": missing}
        out = simulate(
            bars, member, dates, spy_flat(5),
            arm="A", decision_start=2, ma_win=2, atr_period=1,
            max_hold=30, max_names=15, initial_cash=10_000,
        )
        self.assertEqual(out["trades"], 0)
        self.assertTrue(out["reconciled"])
        self.assertEqual(out["ending_cash"], 10_000)

    def test_vol_block_uses_the_decision_close_and_keeps_the_open_position(self):
        dates = [f"d{i}" for i in range(6)]
        px = [10, 11, 12, 13, 14, 15]
        bars = {"AAA": [bar(p, p + 0.2, p - 0.1, p) for p in px]}
        # Wild SPY path so realized vol at the first decision is above 20%.
        spy = [100, 130, 90, 140, 80, 150]
        blocked = simulate(
            bars, always(6, ["AAA"]), dates, spy,
            arm="B", decision_start=3, ma_win=2, atr_period=1, vol_lookback=3, vol_cap=0.20,
            max_hold=30, max_names=1, initial_cash=10_000,
        )
        open_book = simulate(
            bars, always(6, ["AAA"]), dates, spy,
            arm="A", decision_start=3, ma_win=2, atr_period=1,
            max_hold=30, max_names=1, initial_cash=10_000,
        )
        self.assertEqual(blocked["trades"], 0)
        self.assertGreater(blocked["vol_blocks"], 0)
        self.assertGreater(open_book["trades"], 0)
        self.assertTrue(blocked["reconciled"] and open_book["reconciled"])

    def test_dividend_cash_is_credited_once_on_the_split_adjusted_rate(self):
        dates = ["d0", "d1", "d2", "d3", "d4"]
        bars = {
            "AAA": [
                bar(10, 10.2, 9.8, 10),
                bar(11, 11.2, 10.8, 11),
                bar(12, 12.2, 11.8, 12),
                bar(12, 12.2, 11.8, 12),
                bar(12, 12.2, 11.8, 12),
            ]
        }
        # Flat prices, so the only equity change besides costs is the dividend.
        out = simulate(
            bars, always(5, ["AAA"]), dates, spy_flat(5),
            arm="A", decision_start=2, ma_win=2, atr_period=1,
            max_hold=30, max_names=1, sleeve=1.0, initial_cash=10_000, slip=0.0,
            dividends={"AAA": {"d4": 0.004}},
        )
        self.assertTrue(out["reconciled"])
        self.assertAlmostEqual(out["dividend_dollars"], out["trade_log"][0]["shares"] * 0.004, places=6)
        self.assertAlmostEqual(out["ending_equity"], 10_000 + out["dividend_dollars"], places=4)

    def test_close_trail_sells_the_next_open_not_the_threshold_or_the_high(self):
        dates = [f"d{i}" for i in range(8)]
        bars = {
            "AAA": [
                bar(10, 10.2, 9.95, 10),
                bar(10.05, 10.2, 10.0, 10.05),
                bar(10.10, 10.2, 10.05, 10.10),
                bar(10.10, 20.0, 10.08, 11.00),
                bar(10.90, 15.0, 10.50, 10.66),
                bar(10.40, 10.55, 10.20, 10.50),
                bar(10.45, 10.60, 10.30, 10.50),
                bar(10.50, 10.70, 10.40, 10.60),
            ]
        }
        common = dict(
            decision_start=2, ma_win=2, atr_period=1, atr_mult=2.0,
            max_hold=30, max_names=1, sleeve=0.95, initial_cash=10_000,
        )
        trailed = simulate(bars, always(8, ["AAA"]), dates, spy_flat(8), arm="P", **common)
        baseline = simulate(bars, always(8, ["AAA"]), dates, spy_flat(8), arm="A", **common)
        self.assertTrue(trailed["reconciled"])
        trade = next(row for row in trailed["trade_log"] if row["symbol"] == "AAA")
        self.assertEqual(trade["entry_date"], "d3")
        self.assertEqual(trade["exit_date"], "d5")
        self.assertEqual(trade["reason"], "trail_open")
        self.assertAlmostEqual(trade["exit_raw"], 10.40, places=6)
        self.assertNotAlmostEqual(trade["exit_raw"], 11.00 * 0.97, places=4)
        self.assertAlmostEqual(trade["exit_fill"], 10.40 * (1 - SLIP), places=6)
        self.assertNotIn("trail_open", {row["reason"] for row in baseline["trade_log"]})
        self.assertEqual(baseline["trade_log"][0]["exit_date"], "d7")

    def test_original_stop_still_sells_a_gap_after_the_trail_is_armed(self):
        dates = [f"d{i}" for i in range(7)]
        bars = {
            "AAA": [
                bar(10, 10.2, 9.95, 10),
                bar(10.05, 10.2, 10.0, 10.05),
                bar(10.10, 10.2, 10.05, 10.10),
                bar(10.10, 20.0, 10.08, 11.00),
                bar(10.95, 12.0, 10.80, 10.90),
                bar(9.0, 9.2, 8.8, 9.1),
                bar(9.2, 9.4, 9.0, 9.3),
            ]
        }
        out = simulate(
            bars, always(7, ["AAA"]), dates, spy_flat(7),
            arm="P", decision_start=2, ma_win=2, atr_period=1, atr_mult=2.0,
            max_hold=30, max_names=1, sleeve=0.95, initial_cash=10_000,
        )
        trade = next(row for row in out["trade_log"] if row["symbol"] == "AAA")
        self.assertEqual(trade["reason"], "stop_gap")
        self.assertEqual(trade["exit_date"], "d5")
        self.assertAlmostEqual(trade["exit_raw"], 9.0, places=6)


def _quiet(px):
    return [bar(p, p + 0.2, p - 0.1, p) for p in px]


class RotationTests(unittest.TestCase):
    def _book(self, aaa, bbb, *, member_bbb, arm="R", max_hold=30, spy=None):
        n = len(aaa)
        dates = [f"d{i}" for i in range(n)]
        bars = {"AAA": _quiet(aaa), "BBB": _quiet(bbb)}
        member = always(n, ["AAA", "BBB"])
        member["BBB"] = member_bbb
        out = simulate(
            bars, member, dates, spy if spy is not None else spy_flat(n),
            arm=arm, decision_start=2, ma_win=2, atr_period=1,
            max_hold=max_hold, max_names=1, sleeve=1.0, initial_cash=10_000, slip=0.0,
        )
        return out

    def test_persistence_and_next_open_execution(self):
        aaa = [10, 11, 12, 13, 14, 15, 16, 17, 18]
        bbb = [10, 11, 12, 30, 33, 36, 39, 42, 45]
        member_bbb = [False, False, False, True, True, True, True, True, True]
        # A wild SPY path would block arm B. Arm R still replaces.
        wild = [100, 140, 80, 150, 70, 160, 60, 170, 50]
        out = self._book(aaa, bbb, member_bbb=member_bbb, spy=wild)
        self.assertTrue(out["reconciled"])
        self.assertEqual(out["vol_blocks"], 0)
        by_sym = {}
        for trade in out["trade_log"]:
            by_sym.setdefault(trade["symbol"], []).append(trade)
        aaa_exit = by_sym["AAA"][0]
        bbb_entry = by_sym["BBB"][0]
        self.assertEqual(aaa_exit["reason"], "rotation")
        self.assertEqual(aaa_exit["exit_date"], "d6")
        self.assertEqual(bbb_entry["signal_date"], "d5")
        self.assertEqual(bbb_entry["entry_date"], "d6")
        self.assertNotEqual(bbb_entry["signal_date"], bbb_entry["entry_date"])
        self.assertEqual(out["replacements"], 1)
        self.assertEqual(out["rotation_buys"], 1)
        early = [t for t in out["trade_log"] if t["symbol"] == "BBB" and t["entry_date"] < "d6"]
        self.assertEqual(early, [])

    def test_replacement_buy_is_capped_by_the_decision_clip_and_sale_proceeds(self):
        aaa = [10, 11, 12, 13, 14, 15, 16, 17, 18]
        bbb = [10, 11, 12, 30, 33, 36, 39, 42, 45]
        member_bbb = [False, False, False, True, True, True, True, True, True]
        out = self._book(aaa, bbb, member_bbb=member_bbb)
        self.assertTrue(out["reconciled"])
        aaa_trade = next(t for t in out["trade_log"] if t["symbol"] == "AAA")
        bbb_trade = next(t for t in out["trade_log"] if t["symbol"] == "BBB")
        sale_proceeds = aaa_trade["shares"] * aaa_trade["exit_fill"]
        decision_clip = aaa_trade["shares"] * 15.0
        spent = bbb_trade["shares"] * bbb_trade["entry_fill"]
        self.assertAlmostEqual(spent, decision_clip, places=4)
        self.assertLess(spent, sale_proceeds)
        self.assertEqual(bbb_trade["entry_date"], aaa_trade["exit_date"])

    def test_time_exit_at_the_decision_close_cancels_the_replacement(self):
        aaa = [10, 11, 12, 13, 14, 15, 16, 17, 18]
        bbb = [10, 11, 12, 30, 33, 36, 39, 42, 45]
        member_bbb = [False, False, False, True, True, True, True, True, True]
        out = self._book(aaa, bbb, member_bbb=member_bbb, max_hold=2)
        self.assertTrue(out["reconciled"])
        self.assertEqual(out["replacements"], 0)
        aaa_trade = next(t for t in out["trade_log"] if t["symbol"] == "AAA")
        self.assertEqual(aaa_trade["reason"], "time")
        self.assertEqual(aaa_trade["exit_date"], "d5")

    def test_gap_stop_supersedes_the_replacement_sale_and_funds_the_buy(self):
        aaa = [10, 11, 12, 13, 14, 15, 16, 17, 18]
        bbb = [10, 11, 12, 30, 33, 36, 39, 42, 45]
        n = len(aaa)
        dates = [f"d{i}" for i in range(n)]
        bars = {"AAA": _quiet(aaa), "BBB": _quiet(bbb)}
        bars["AAA"][6] = bar(1, 1.2, 0.8, 1.1)
        member = always(n, ["AAA", "BBB"])
        member["BBB"] = [False, False, False, True, True, True, True, True, True]
        out = simulate(
            bars, member, dates, spy_flat(n),
            arm="R", decision_start=2, ma_win=2, atr_period=1,
            max_hold=30, max_names=1, sleeve=1.0, initial_cash=10_000, slip=0.0,
        )
        self.assertTrue(out["reconciled"])
        aaa_trade = next(t for t in out["trade_log"] if t["symbol"] == "AAA")
        bbb_trade = next(t for t in out["trade_log"] if t["symbol"] == "BBB")
        self.assertEqual(aaa_trade["reason"], "stop_gap")
        self.assertEqual(aaa_trade["exit_date"], "d6")
        self.assertEqual(aaa_trade["exit_raw"], 1)
        self.assertEqual(out["replacements"], 0)
        self.assertEqual(out["rotation_preempted"], 1)
        self.assertEqual(bbb_trade["entry_date"], "d6")
        self.assertAlmostEqual(bbb_trade["shares"] * bbb_trade["entry_fill"], aaa_trade["shares"] * 1.0, places=4)

    def test_rename_keeps_one_position(self):
        dates = [f"d{i}" for i in range(5)]
        px = [10, 11, 12, 13, 14]
        bars = {
            "AAA": _quiet(px),
            "BBB": _quiet(px),
        }
        out = simulate(
            bars, always(5, ["AAA", "BBB"]), dates, spy_flat(5),
            arm="A", decision_start=2, ma_win=2, atr_period=1,
            max_hold=30, max_names=1, sleeve=1.0, initial_cash=10_000, slip=0.0,
            renames={"d4": [("AAA", "BBB")]},
        )
        self.assertTrue(out["reconciled"])
        self.assertEqual(len(out["trade_log"]), 1)
        self.assertEqual(out["trade_log"][0]["symbol"], "BBB")
        self.assertEqual(out["trade_log"][0]["entry_date"], "d3")


class DistributionTests(unittest.TestCase):
    def test_entitlement_uses_the_child_open_and_reconciles(self):
        dates = [f"d{i}" for i in range(7)]
        aaa = [10, 11, 12, 40, 30, 31, 32]
        bbb = [None, None, None, None, bar(40, 41, 39, 40), bar(1, 1.2, 0.8, 1.1), bar(42, 43, 41, 42)]
        bars = {"AAA": _quiet(aaa), "BBB": bbb}
        member = always(7, ["AAA", "BBB"])
        member["BBB"] = [False] * 7
        slip = 0.001
        out = simulate(
            bars, member, dates, spy_flat(7),
            arm="A", decision_start=2, ma_win=2, atr_period=2,
            max_hold=30, max_names=1, sleeve=1.0, initial_cash=10_000, slip=slip,
            distributions=[Distribution("d4", "AAA", "BBB", 0.25)],
        )
        same = simulate(
            bars, member, dates, spy_flat(7),
            arm="R", decision_start=2, ma_win=2, atr_period=2,
            max_hold=30, max_names=1, sleeve=1.0, initial_cash=10_000, slip=slip,
            distributions=[Distribution("d4", "AAA", "BBB", 0.25)],
        )
        self.assertTrue(out["reconciled"] and same["reconciled"])
        self.assertEqual(out["distribution_unpriced"], 0)
        parent = next(t for t in out["trade_log"] if t["symbol"] == "AAA")
        entitled = parent["shares"] * 0.25
        whole = float(int(entitled))
        fraction = entitled - whole
        frac = next(t for t in out["trade_log"] if t["reason"] == "distribution_fraction")
        held = next(t for t in out["trade_log"] if t["symbol"] == "BBB" and t["reason"] != "distribution_fraction")
        self.assertAlmostEqual(frac["shares"], fraction, places=6)
        self.assertAlmostEqual(frac["exit_raw"], 40.0, places=6)
        self.assertAlmostEqual(frac["pnl"], fraction * 40.0 * (1.0 - slip), places=4)
        self.assertNotAlmostEqual(frac["pnl"], 1452.0, places=0)
        self.assertAlmostEqual(held["shares"], whole, places=6)
        self.assertEqual(held["entry_date"], "d4")
        self.assertEqual(held["exit_date"], "d6")
        self.assertEqual(held["reason"], "final")
        self.assertNotIn("stop_gap", {t["reason"] for t in out["trade_log"] if t["symbol"] == "BBB"})
        self.assertEqual(same["trade_log"][0]["reason"], out["trade_log"][0]["reason"])
        self.assertAlmostEqual(same["ending_equity"], out["ending_equity"], places=4)

    def test_a_purchase_on_the_distribution_open_is_not_entitled(self):
        dates = [f"d{i}" for i in range(5)]
        aaa = [10, 11, 12, 13, 14]
        bbb = [40, 40, 40, 40, 40]
        bars = {"AAA": _quiet(aaa), "BBB": _quiet(bbb)}
        out = simulate(
            bars, always(5, ["AAA", "BBB"]), dates, spy_flat(5),
            arm="A", decision_start=2, ma_win=2, atr_period=1,
            max_hold=30, max_names=1, sleeve=1.0, initial_cash=10_000, slip=0.0,
            distributions=[Distribution("d3", "AAA", "BBB", 0.25)],
        )
        self.assertTrue(out["reconciled"])
        self.assertEqual([t for t in out["trade_log"] if t["symbol"] == "BBB"], [])


if __name__ == "__main__":
    unittest.main()
