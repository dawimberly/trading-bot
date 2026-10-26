"""Focused tests for the walk-forward learner. Research only."""

from __future__ import annotations

import ast
import json
import sqlite3
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ABC = HERE.parent / "nyse_cash_abc"
WFS = HERE.parent / "walk_forward_select"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ABC))
sys.path.insert(0, str(WFS))

from engine import Bar, Distribution, usable  # noqa: E402
from plan import EVAL, TRAIN, WARMUP, build_plan, candidates  # noqa: E402
from select_rule import completed_trades, select_winner  # noqa: E402
from stitch import simulate_continuous  # noqa: E402

RULES = {"max_drawdown": 0.25, "min_completed_round_trips": 30, "baseline_id": "ma70_hold30_atr2.0"}
SLIP = 5.0 / 10_000.0


def _days(n: int, start: str = "2024-01-02") -> list[str]:
    cursor = date.fromisoformat(start)
    out = []
    while len(out) < n:
        if cursor.weekday() < 5:
            out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


def _series(dates: list[str], start: float, step: float) -> list[Bar]:
    bars = []
    for i, _day in enumerate(dates):
        close = start + step * i
        bars.append(Bar(close - 0.05, close + 0.10, close - 0.10, close))
    return bars


def _run(dates, bars, member, schedule, **kwargs):
    anchor = bars[next(iter(bars))]
    spy = [None if bar is None else bar.close for bar in anchor]
    return simulate_continuous(
        bars,
        member,
        dates,
        spy,
        schedule,
        decision_start=kwargs.pop("decision_start", 6),
        atr_period=kwargs.pop("atr_period", 2),
        slip=SLIP,
        initial_cash=100_000.0,
        liquidate_terminal=kwargs.pop("liquidate_terminal", False),
        open_book=kwargs.pop("open_book", True),
        **kwargs,
    )


class SelectionTests(unittest.TestCase):
    def test_grid_and_windows(self):
        rows = candidates()
        self.assertEqual(len(rows), 27)
        self.assertEqual(len({row["candidate_id"] for row in rows}), 27)
        calendar = _days(400)
        plan = build_plan(calendar)
        self.assertEqual(plan["train_sessions"], TRAIN)
        self.assertEqual(plan["eval_sessions"], EVAL)
        seen = []
        for fold in plan["fold_bounds"]:
            self.assertEqual(len(fold["train_scored"]), 100)
            self.assertEqual(len(fold["train_dates"]), WARMUP + 100)
            self.assertLess(fold["train_dates"][-1], fold["eval_dates"][0])
            self.assertEqual(fold["train_dates"][:WARMUP][-1] < fold["train_scored"][0], True)
            seen.extend(fold["eval_dates"])
        self.assertEqual(len(seen), len(set(seen)))

    def test_tie_break_and_fallback(self):
        base = {
            "order": 13,
            "ma_win": 70,
            "max_hold": 30,
            "atr_mult": 2.0,
            "net_return": 0.05,
            "max_drawdown": 0.10,
            "turnover": 1.0,
            "completed_round_trips": 40,
            "open_positions": 1,
            "reconciled": True,
        }
        other = dict(base, order=3, ma_win=50, max_hold=15, atr_mult=2.0, net_return=0.05, max_drawdown=0.10, turnover=1.0)
        lower_dd = dict(base, order=20, ma_win=100, max_hold=45, atr_mult=2.5, net_return=0.05, max_drawdown=0.04, turnover=3)
        chosen = select_winner([base, other, lower_dd], RULES)
        self.assertEqual(chosen["selected"]["order"], 20)
        same = select_winner([base, other], RULES)
        self.assertEqual(same["selected"]["order"], 3)
        thin = [dict(base, completed_round_trips=10), dict(other, completed_round_trips=10, max_drawdown=0.4)]
        fallback = select_winner(thin, RULES)
        self.assertEqual(fallback["choice"], "baseline_fallback")
        self.assertEqual(fallback["selected"]["candidate_id"], "ma70_hold30_atr2.0")
        peeked = dict(other, net_return=0.01, eval_return=9.0, completed_round_trips=40, max_drawdown=0.02)
        ignored = select_winner([base, peeked], RULES)
        self.assertEqual(ignored["selected"]["candidate_id"], "ma70_hold30_atr2.0")

    def test_unfinished_trades_are_not_completed(self):
        trades = [
            {"reason": "stop", "pnl": -1},
            {"reason": "final", "pnl": 5},
            {"reason": "distribution_fraction", "pnl": 1},
            {"reason": "time", "pnl": 2},
        ]
        done = completed_trades(trades)
        self.assertEqual([row["reason"] for row in done], ["stop", "time"])


class ExecutionTests(unittest.TestCase):
    def test_next_open_cost_and_open_mark(self):
        dates = _days(12)
        bars = {"AAA": _series(dates, 20, 0.4)}
        member = {"AAA": [True] * len(dates)}
        spec = {"ma_win": 3, "max_hold": 30, "atr_mult": 50}
        schedule = [None if i < 6 else dict(spec) for i in range(len(dates))]
        out = _run(dates, bars, member, schedule)
        buys = [row for row in out["executions"] if row["kind"] == "buy"]
        self.assertTrue(buys)
        self.assertEqual(buys[0]["session"], dates[7])
        dollars = buys[0]["dollars"]
        self.assertAlmostEqual(buys[0]["cost"], dollars * SLIP / (1.0 + SLIP), places=6)
        self.assertEqual(completed_trades(out["trade_log"]), [])
        self.assertGreater(out["ending_equity"], out["ending_cash"])
        self.assertEqual(len(out["positions"]), 1)
        from score import classify

        row = classify(out, bars, [], dates, {"AAA": {day: bar for day, bar in zip(dates, bars["AAA"])}})
        self.assertEqual(row["completed_round_trips"], 0)
        self.assertTrue(row["reconciled"])
        self.assertGreater(row["unrealized_pnl"], -50000)

    def test_future_bar_cannot_change_an_earlier_result(self):
        dates = _days(12)
        bars = {"AAA": _series(dates, 20, 0.4)}
        member = {"AAA": [True] * len(dates)}
        spec = {"ma_win": 3, "max_hold": 30, "atr_mult": 50}
        schedule = [None if i < 6 else dict(spec) for i in range(len(dates))]
        first = _run(dates, bars, member, schedule)
        future = _days(13)
        longer = {"AAA": _series(future, 20, 0.4)}
        longer["AAA"][-1] = Bar(1, 1, 1, 1)
        trimmed = {"AAA": longer["AAA"][:-1]}
        second = _run(dates, trimmed, member, schedule)
        self.assertEqual(first["ending_equity"], second["ending_equity"])
        changed = {"AAA": list(bars["AAA"])}
        last = changed["AAA"][-1]
        changed["AAA"][-1] = Bar(last.open, last.high, last.low, last.close * 1.2)
        third = _run(dates, changed, member, schedule)
        self.assertNotEqual(first["ending_equity"], third["ending_equity"])

    def test_fold_boundary_keeps_entry_contract(self):
        dates = _days(18)
        bars = {"AAA": _series(dates, 30, 0.3)}
        member = {"AAA": [True] * len(dates)}
        early = {"ma_win": 3, "max_hold": 15, "atr_mult": 50}
        late = {"ma_win": 3, "max_hold": 1, "atr_mult": 50}
        schedule = [None if i < 6 else (dict(early) if i < 12 else dict(late)) for i in range(len(dates))]
        out = _run(dates, bars, member, schedule, decision_start=6)
        self.assertEqual(len(out["positions"]), 1)
        self.assertEqual(out["positions"][0]["max_hold"], 15)
        self.assertLess(out["ending_cash"], 100_000.0)
        self.assertNotAlmostEqual(out["ending_cash"], 100_000.0, places=2)

    def test_missing_price_stays_visible(self):
        self.assertFalse(usable(Bar(10, 9, 8, 10)))
        bad = Bar(10, 9, 8, 10)
        self.assertIsNone(bad if usable(bad) else None)
        dates = _days(10)
        series = _series(dates, 15, 0.2)
        series[-2] = None
        series[-1] = None
        bars = {"AAA": series, "CHILD": [None] * len(dates)}
        member = {"AAA": [True] * len(dates)}
        spec = {"ma_win": 3, "max_hold": 2, "atr_mult": 50}
        schedule = [dict(spec) for _ in dates]
        out = _run(
            dates,
            bars,
            member,
            schedule,
            decision_start=4,
            distributions=[Distribution(dates[6], "AAA", "CHILD", 0.5)],
        )
        self.assertGreaterEqual(out["distribution_unpriced"], 1)
        held = {"ma_win": 3, "max_hold": 30, "atr_mult": 50}
        ended = _run(
            dates,
            {"AAA": series},
            {"AAA": [True] * len(dates)},
            [dict(held) for _ in dates],
            decision_start=4,
        )
        self.assertGreaterEqual(ended["stale_terminals"], 1)
        self.assertTrue(any(row["reason"] == "last_print" for row in ended["trade_log"]))


class IsolationTests(unittest.TestCase):
    def test_sources_do_not_import_broker_or_scheduler(self):
        banned = ("alpaca_client", "alpaca_executor", "portal_bot", "schtasks", "run_all", "submit_order")
        for name in ("runner.py", "market.py", "score.py", "books.py", "select_rule.py", "plan.py"):
            text = (HERE / name).read_text(encoding="utf-8")
            tree = ast.parse(text)
            modules = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules.append(node.module)
            self.assertFalse(any(any(word in module for word in banned) for module in modules), name)
            self.assertNotIn("schtasks", text)

    def test_market_opens_readonly(self):
        text = (HERE / "market.py").read_text(encoding="utf-8")
        self.assertIn("mode=ro", text)
        self.assertNotIn("market_data.db", text)


class TrustedBaselineTests(unittest.TestCase):
    def test_prior_window_matches_saved_run_and_stitch(self):
        from engine import WARMUP as ENGINE_WARMUP
        from engine import simulate
        from membership import research_membership
        from run_abc import dividend_map, load_tables, member_on, membership_index
        from run_abc import panel_for as abc_panel
        from run_rotation import GEV_DISTRIBUTION, alias_fi

        calendar = json.loads((ABC / "data" / "calendar.json").read_text(encoding="utf-8"))
        need = ENGINE_WARMUP + 365
        self.assertGreaterEqual(len(calendar), ENGINE_WARMUP + 2 * 365)
        span = calendar[-(ENGINE_WARMUP + 2 * 365) :]
        prior = span[: ENGINE_WARMUP + 365]
        self.assertEqual(prior[ENGINE_WARMUP], "2023-11-15")
        self.assertEqual(prior[-1], "2025-05-01")
        conn = sqlite3.connect(f"file:{(ABC / 'data' / 'bars.sqlite').as_posix()}?mode=ro", uri=True)
        bars, divs, splits, _notes = load_tables(conn)
        conn.close()
        lookup = {}
        for rec in bars.itertuples(index=False):
            lookup.setdefault(rec.symbol, {})[rec.session] = Bar(rec.open, rec.high, rec.low, rec.close)
        alias_fi(lookup)
        membership = membership_index(research_membership())
        symbols = sorted(
            s for s in set(lookup) - {"SPY"} if any(member_on(membership, s, day) for day in prior[ENGINE_WARMUP:])
        )
        panel, member, spy = abc_panel(prior, symbols, lookup, membership)
        dividends = dividend_map(divs, splits, prior)
        out = simulate(
            panel,
            member,
            prior,
            spy,
            arm="A",
            decision_start=ENGINE_WARMUP,
            dividends=dividends,
            distributions=[GEV_DISTRIBUTION],
        )
        self.assertTrue(out["reconciled"])
        self.assertEqual(out["stale_terminals"], 0)
        self.assertAlmostEqual(out["ending_equity"], 96334.80026644812, places=6)
        self.assertAlmostEqual(out["max_drawdown"], 0.22934488901978775, places=8)
        self.assertEqual(out["trades"], 426)
        self.assertAlmostEqual(out["cost_dollars"], 2794.60012183656, places=6)
        schedule = [
            None if i < ENGINE_WARMUP else {"ma_win": 70, "max_hold": 30, "atr_mult": 2.0} for i in range(len(prior))
        ]
        stitched = simulate_continuous(
            panel,
            member,
            prior,
            spy,
            schedule,
            decision_start=ENGINE_WARMUP,
            dividends=dividends,
            distributions=[GEV_DISTRIBUTION],
            liquidate_terminal=True,
        )
        self.assertAlmostEqual(stitched["ending_equity"], out["ending_equity"], places=6)
        self.assertEqual(stitched["trades"], out["trades"])
        self.assertAlmostEqual(stitched["cost_dollars"], out["cost_dollars"], places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
