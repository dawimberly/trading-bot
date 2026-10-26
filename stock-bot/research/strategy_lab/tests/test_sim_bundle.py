"""Snapshot selection, rename continuity, dividend entitlement, and merger accounting."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
WALK = LAB.parent / "walk_forward_select"
ABC = LAB.parent / "nyse_cash_abc"
sys.path.insert(0, str(LAB))
sys.path.insert(0, str(WALK))
sys.path.insert(0, str(ABC))

from engine import Bar  # noqa: E402
from stitch import Grant, Settlement, simulate_continuous  # noqa: E402

from lab.accounting import classify_actions, dividend_resolutions  # noqa: E402
from lab.bounds import conclusion_changes  # noqa: E402
from lab.paths import BARS, SIM_BUNDLE, SIM_V1, SIM_V2, SPAN_BUNDLE  # noqa: E402
from lab.sim_v2 import V1_SNAPSHOT  # noqa: E402


def _book(dates, bars, member, **kwargs):
    schedule = [None, None] + [{"ma_win": 2, "max_hold": 30, "atr_mult": 2.0}] * (len(dates) - 2)
    return simulate_continuous(
        bars,
        member,
        dates,
        [100.0] * len(dates),
        schedule,
        decision_start=2,
        atr_period=1,
        max_names=2,
        sleeve=1.0,
        slip=0.0,
        initial_cash=1_000.0,
        **kwargs,
    )


class AccountingDecisionTests(unittest.TestCase):
    def test_four_dividend_exceptions_are_not_second_entitlements(self):
        payments = [
            {"symbol": "LB", "ex_date": "2021-06-03", "rate": 0.15, "payable_date": "2021-06-18"},
            {"symbol": "BBWI", "ex_date": "2021-06-03", "rate": 0.15, "payable_date": "2021-06-18"},
            {"symbol": "DFS", "ex_date": "2025-05-23", "rate": 0.7, "payable_date": "2025-06-05"},
            {"symbol": "FLIR", "ex_date": "2021-05-20", "rate": 0.17, "payable_date": "2021-06-04"},
            {"symbol": "KSU", "ex_date": "2021-12-30", "rate": 0.54, "payable_date": "2022-01-19"},
        ]
        rows = dividend_resolutions(payments)
        self.assertEqual([row["resolution"] for row in rows], ["not_entitled"] * 4)
        self.assertIn("LB", rows[0]["reason"])

    def test_conflicting_para_terms_do_not_invent_proceeds(self):
        rows = [
            {"kind": "cash_mergers", "acquiree_symbol": "PARA", "acquirer_symbol": "PSKY", "rate": 15.0, "effective_date": "2025-08-07"},
            {"kind": "stock_mergers", "acquiree_symbol": "PARA", "acquirer_symbol": "PSKY", "acquirer_rate": 1.0, "acquiree_rate": 1.0, "effective_date": "2025-08-07"},
        ]
        decision = classify_actions(
            rows,
            intervals={"PARA": [("2022-02-17", "2025-08-08")]},
            sessions={"PARA": {"2025-08-06"}, "SPY": {"2025-08-07"}},
            unusable={("PARA", "2025-08-06")},
            closes={},
        )
        self.assertEqual(decision["settlements"], [])
        self.assertFalse(decision["material_unresolved"])
        self.assertTrue(decision["simulation_ready"])


class SimulatorAccountingTests(unittest.TestCase):
    def test_cash_merger_uses_the_stated_rate(self):
        dates = [f"2024-01-0{i}" for i in range(1, 6)]
        held = [Bar(10 + i, 12 + i, 9 + i, 10 + i) for i in range(4)]
        bars = {"AAA": held + [None], "BBB": [Bar(20, 21, 19, 20)] * 5}
        out = _book(
            dates,
            bars,
            {"AAA": [True] * 5, "BBB": [True] * 5},
            settlements={"2024-01-05": [Settlement("AAA", cash_per_share=12.0)]},
        )
        self.assertEqual(out["unresolved_holdings"], 0)
        self.assertTrue(any(trade["reason"] == "merger_consideration" and trade["exit_fill"] == 12.0 for trade in out["trade_log"]))
        self.assertNotIn("AAA", {row["symbol"] for row in out.get("positions", [])})

    def test_missing_acquirer_price_keeps_the_holding(self):
        dates = [f"2024-01-0{i}" for i in range(1, 6)]
        held = [Bar(10 + i, 12 + i, 9 + i, 10 + i) for i in range(4)]
        bars = {"AAA": held + [None]}
        out = _book(
            dates,
            bars,
            {"AAA": [True] * 5},
            settlements={"2024-01-05": [Settlement("AAA", acquirer="BBB", share_ratio=1.0)]},
            strict_actions=True,
            liquidate_terminal=True,
            open_book=True,
        )
        self.assertGreater(out["unresolved_holdings"], 0)
        self.assertEqual(out["positions"][0]["symbol"], "AAA")
        self.assertFalse(any(trade["reason"] == "last_print" for trade in out["trade_log"]))

    def test_failed_rename_is_not_a_sale(self):
        dates = [f"2024-01-0{i}" for i in range(1, 6)]
        held = [Bar(10 + i, 12 + i, 9 + i, 10 + i) for i in range(4)]
        bars = {"AAA": held + [None]}
        out = _book(
            dates,
            bars,
            {"AAA": [True] * 5},
            renames={"2024-01-05": [("AAA", "BBB")]},
            strict_actions=True,
            open_book=True,
        )
        self.assertGreater(out["unresolved_holdings"], 0)
        self.assertEqual(out["positions"][0]["symbol"], "AAA")
        self.assertFalse(any(trade["reason"] in {"last_print", "final_stale"} for trade in out["trade_log"]))


class BundleIsolationTests(unittest.TestCase):
    def test_selected_bundle_is_separate_from_the_frozen_tape(self):
        if not (SIM_BUNDLE / "manifest.json").is_file():
            self.skipTest("simulation bundle has not been built")
        from lab.adapter import market_for
        from lab.snapshots import identify

        frozen_before = BARS.stat().st_mtime_ns
        span_before = (SPAN_BUNDLE / "bars.sqlite").stat().st_mtime_ns
        frozen = market_for(identify()["snapshot_id"])
        sim = market_for(SIM_BUNDLE.name)
        self.assertEqual(frozen["kind"], "frozen")
        self.assertEqual(sim["kind"], "sim")
        self.assertEqual(frozen["calendar"][0], "2023-01-03")
        self.assertEqual(sim["calendar"][0], "2021-01-04")
        self.assertEqual(sim["calendar"][-1], "2026-10-14")
        self.assertNotIn("PARA", sim["lookup"])
        self.assertEqual(sim["lookup"]["NVDA"]["2024-06-10"].high, 123.1)
        self.assertEqual(sim["lookup"]["FI"]["2023-06-07"].close, sim["lookup"]["FISV"]["2023-06-07"].close)
        self.assertNotIn("2021-06-01", sim["lookup"]["FI"])
        self.assertTrue(any(row["old"] == "FI" and row["new"] == "FISV" for row in sim["accounting"]["renames"]))
        entitled = {(lot["symbol"], lot["ex_date"]) for lot in sim["lots"]}
        for key in (("BBWI", "2021-06-03"), ("DFS", "2025-05-23"), ("FLIR", "2021-05-20"), ("KSU", "2021-12-30")):
            self.assertNotIn(key, entitled)
        self.assertTrue(sim["spy_lot_list"])
        self.assertTrue(all(lot["symbol"] == "SPY" for lot in sim["spy_lot_list"]))
        self.assertEqual(BARS.stat().st_mtime_ns, frozen_before)
        self.assertEqual((SPAN_BUNDLE / "bars.sqlite").stat().st_mtime_ns, span_before)


class EntitlementBoundTests(unittest.TestCase):
    def test_cash_leg_keeps_the_right_inside_its_contract_range(self):
        dates = [f"2024-01-0{i}" for i in range(1, 6)]
        held = [Bar(10 + i, 12 + i, 9 + i, 10 + i) for i in range(4)]
        bars = {"AAA": held + [None], "BBB": [Bar(20, 21, 19, 20)] * 5}
        out = _book(
            dates,
            bars,
            {"AAA": [True] * 5, "BBB": [True] * 5},
            settlements={
                "2024-01-05": [
                    Settlement(
                        "AAA",
                        cash_per_share=76.0,
                        residual_name="436CVR021",
                        residual_per_share=1.0,
                        residual_low=0.0,
                        residual_high=3.0,
                        residual_terminal="bounded",
                    )
                ]
            },
        )
        cash_leg = [trade for trade in out["trade_log"] if trade["reason"] == "merger_cash_leg"]
        self.assertEqual(len(cash_leg), 1)
        self.assertEqual(cash_leg[0]["exit_fill"], 76.0)
        self.assertFalse(any(trade["symbol"] == "436CVR021" for trade in out["trade_log"]))
        right = out["entitlements"][0]
        self.assertEqual(right["name"], "436CVR021")
        self.assertEqual(right["units"], cash_leg[0]["shares"])
        self.assertEqual(right["value_low"], 0.0)
        self.assertEqual(right["value_high"], 3.0)
        self.assertEqual(out["equity_low"], out["listed_equity"])
        self.assertAlmostEqual(out["equity_high"], out["listed_equity"] + 3.0 * right["units"])
        self.assertFalse(out["valuation_inconclusive"])
        self.assertTrue(conclusion_changes(out["equity_low"], out["equity_high"], lambda value: value > out["listed_equity"]))
        self.assertFalse(conclusion_changes(out["equity_low"], out["equity_high"], lambda value: value > out["equity_high"]))

    def test_unpriced_distribution_keeps_fractional_units(self):
        dates = [f"2024-01-0{i}" for i in range(1, 6)]
        held = [Bar(10 + i, 12 + i, 9 + i, 10 + i) for i in range(5)]
        bars = {"AAA": held, "BBB": [Bar(20, 21, 19, 20)] * 5}
        plain = _book(dates, bars, {"AAA": [True] * 5, "BBB": [True] * 5}, liquidate_terminal=False, open_book=True)
        granted = _book(
            dates,
            bars,
            {"AAA": [True] * 5, "BBB": [True] * 5},
            grants=[Grant("2024-01-05", "AAA", "THNPF", 0.2, value_low=10.89, value_high=15.33)],
            liquidate_terminal=False,
            open_book=True,
        )
        self.assertEqual(granted["ending_cash"], plain["ending_cash"])
        self.assertFalse(any(trade["reason"] == "distribution_fraction" for trade in granted["trade_log"]))
        stub = granted["entitlements"][0]
        held_shares = granted["positions"][0]["shares"]
        self.assertAlmostEqual(stub["units"], held_shares * 0.2)
        self.assertIsNone(stub["value_low"])
        self.assertEqual(stub["grant_value_low"], 10.89)
        self.assertEqual(stub["grant_value_high"], 15.33)
        self.assertIsNone(granted["equity_low"])
        self.assertIsNone(granted["equity_high"])
        self.assertTrue(granted["valuation_inconclusive"])
        self.assertTrue(conclusion_changes(granted["equity_low"], granted["equity_high"], lambda value: value > 0))


class V2BundleTests(unittest.TestCase):
    def test_v2_keeps_the_names_and_blocks_search(self):
        if not (SIM_V2 / "manifest.json").is_file():
            self.skipTest("v2 bundle has not been built")
        import csv
        import json

        from lab.adapter import _payable_book, market_for
        from lab.snapshots import identify
        from lab.specs import SpecError

        v1 = json.loads((SIM_V1 / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(v1["snapshot_id"], V1_SNAPSHOT)
        self.assertFalse(v1["simulation_ready"])
        self.assertEqual(v1["material_unresolved"], 3)
        v1_accounting = json.loads((SIM_V1 / "accounting.json").read_text(encoding="utf-8"))
        self.assertEqual(len(v1_accounting["material_unresolved"]), 3)

        frozen_before = BARS.stat().st_mtime_ns
        v2 = market_for(SIM_V2.name)
        frozen = market_for(identify()["snapshot_id"])
        self.assertEqual(frozen["calendar"][0], "2023-01-03")
        self.assertEqual(v2["calendar"][0], "2021-01-04")
        self.assertFalse(v2["simulation_ready"])
        self.assertTrue(v2["search_blocked"])
        self.assertFalse(v2["representative_universe"])
        self.assertNotIn("PARA", v2["lookup"])
        with (SIM_V2 / "membership.csv").open(encoding="utf-8", newline="") as handle:
            names = {row["ticker"] for row in csv.DictReader(handle)}
        for name in ("FTI", "HOLX", "WBA", "PARA"):
            self.assertIn(name, names)
        rights = {row.get("residual_name") for row in v2["accounting"]["settlements"]}
        self.assertIn("436CVR021", rights)
        self.assertIn("931CVR013", rights)
        self.assertEqual(v2["accounting"]["grants"][0]["name"], "THNPF")
        self.assertEqual(v2["accounting"]["grants"][0]["terminal"], "unpriced_path")
        limitations = json.loads((SIM_V2 / "limitations.json").read_text(encoding="utf-8"))
        self.assertFalse(limitations["representative_universe"])
        self.assertTrue(limitations["para"]["in_requested_universe"])
        self.assertFalse(limitations["para"]["tradable"])
        with self.assertRaises(SpecError) as caught:
            _payable_book(["2021-01-04"], [], 0.0, 0, v2)
        self.assertIn("Strategy search is blocked", str(caught.exception))
        self.assertIn("PARA", str(caught.exception))
        self.assertEqual(BARS.stat().st_mtime_ns, frozen_before)
        self.assertEqual(json.loads((SIM_V1 / "manifest.json").read_text(encoding="utf-8"))["snapshot_id"], V1_SNAPSHOT)


if __name__ == "__main__":
    unittest.main()
