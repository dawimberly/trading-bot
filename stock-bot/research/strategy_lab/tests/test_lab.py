"""Lab checks. Parity tests hit the frozen engines read-only; job tests use a temporary database."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))
ABC = LAB.parent / "nyse_cash_abc"
WALK = LAB.parent / "walk_forward_select"
sys.path.insert(0, str(ABC))
sys.path.insert(0, str(WALK))

os.environ["STRATEGY_LAB_DB"] = str(Path(tempfile.mkdtemp()) / "lab.sqlite")

from engine import Bar  # noqa: E402

from lab.jobs import claim, enqueue, job_detail, set_control  # noqa: E402
from lab.reports import result_tables  # noqa: E402
from lab.runner import execute, run_plan  # noqa: E402
from lab.specs import SpecError, UNTOUCHED_RANGE, default_research_spec, resolve_windows, validate_spec  # noqa: E402
from lab.store import connect  # noqa: E402
from stitch import simulate_continuous  # noqa: E402


def weekdays(n: int) -> list[str]:
    from datetime import date, timedelta

    day = date(2024, 1, 2)
    out = []
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def spec_on(calendar: list[str]) -> dict:
    spec = default_research_spec("unit")
    spec["schedule"]["anchor"] = calendar[-1]
    spec["schedule"]["warmup_sessions"] = 85
    spec["schedule"]["train_sessions"] = 20
    spec["schedule"]["eval_sessions"] = 10
    spec["schedule"]["folds"] = 1
    spec["budget_hours"] = 1
    return spec


class SpecTests(unittest.TestCase):
    def test_missing_anchor_is_not_replaced(self):
        calendar = weekdays(200)
        spec = spec_on(calendar)
        spec["schedule"]["anchor"] = "1999-01-04"
        with self.assertRaises(SpecError) as caught:
            validate_spec(spec, calendar)
        self.assertIn("1999-01-04", str(caught.exception))
        self.assertIn("not a session", str(caught.exception))

    def test_short_history_is_not_clipped(self):
        calendar = weekdays(30)
        spec = spec_on(calendar)
        with self.assertRaises(SpecError) as caught:
            resolve_windows(spec, calendar)
        self.assertIn("was not shortened", str(caught.exception))

    def test_locked_range_blocks_search(self):
        calendar = weekdays(200)
        spec = spec_on(calendar)
        windows = resolve_windows(spec, calendar)
        spec["locked_validation"] = {"start": windows["eval_dates"][0], "end": windows["eval_dates"][-1]}
        with self.assertRaises(SpecError) as caught:
            validate_spec(spec, calendar)
        self.assertIn("locked validation", str(caught.exception))

    def test_no_untouched_range_is_stated(self):
        self.assertIsNone(UNTOUCHED_RANGE)
        spec = validate_spec(spec_on(weekdays(200)), weekdays(200))
        self.assertIn("No genuinely untouched range", spec["untouched_reason"])
        self.assertEqual(spec["data_role"], "development")


class AccountingTests(unittest.TestCase):
    def test_receivable_is_not_spendable_until_payable(self):
        dates = [f"2024-01-{i+1:02d}" for i in range(8)]
        bars = {"AAA": [Bar(10.0 + i, 10.4 + i, 9.8 + i, 10.0 + i) for i in range(8)]}
        spec = {"ma_win": 2, "max_hold": 30, "atr_mult": 2.0}
        out = simulate_continuous(
            bars,
            {"AAA": [True] * 8},
            dates,
            [100.0] * 8,
            [None, None] + [spec] * 6,
            decision_start=2,
            atr_period=1,
            max_names=1,
            sleeve=1.0,
            initial_cash=10_000,
            slip=0.0,
            dividend_lots=[{"symbol": "AAA", "ex_session": "2024-01-06", "pay_session": "2024-10-30", "rate": 1.0}],
        )
        self.assertTrue(out["reconciled"])
        self.assertAlmostEqual(out["dividend_received"], 0.0)
        self.assertGreater(out["dividend_unpaid"], 0)
        self.assertAlmostEqual(out["ending_equity"] - out["ending_cash"], out["dividend_unpaid"])


class JobTests(unittest.TestCase):
    def setUp(self):
        path = Path(os.environ["STRATEGY_LAB_DB"])
        if path.exists():
            path.unlink()
        for suffix in ("-wal", "-shm"):
            side = Path(str(path) + suffix)
            if side.exists():
                side.unlink()

    def _job(self, hours: float = 1):
        calendar = weekdays(200)
        spec = spec_on(calendar)
        spec["budget_hours"] = hours
        job_id = enqueue(spec, calendar=calendar, snapshot_id="test-snapshot", candidates_total=2)
        claimed = claim("tester")
        self.assertEqual(claimed["id"], job_id)
        return claimed, spec

    def test_pause_and_cancel_between_candidates(self):
        job, _spec = self._job()
        seen = {"paused": False}

        def run_one(item):
            if item["ordinal"] == 0:
                set_control(job["id"], "pause")
            return {"folds_completed": 1, "folds_required": 1, "net_return": 0.01, "fold": 1}

        def sleep(_seconds):
            seen["paused"] = True
            set_control(job["id"], "resume")

        status = run_plan(job, "tester", [{"ordinal": 0, "phase": "training", "params": {"order": 0}}, {"ordinal": 1, "phase": "training", "params": {"order": 1}}], run_one, sleep=sleep)
        self.assertTrue(seen["paused"])
        self.assertEqual(status, "completed")
        detail = job_detail(job["id"])
        self.assertEqual(len(detail["candidates"]), 2)

        job2, _spec = self._job()

        def cancel_one(item):
            set_control(job2["id"], "cancel")
            return {"folds_completed": 1, "folds_required": 1, "net_return": 0.01, "fold": 1}

        status = run_plan(job2, job2["owner"], [{"ordinal": 0, "phase": "training", "params": {}}, {"ordinal": 1, "phase": "training", "params": {}}], cancel_one, sleep=lambda _s: None)
        self.assertEqual(status, "cancelled")
        rows = job_detail(job2["id"])["candidates"]
        self.assertEqual(rows[-1]["status"], "incomplete")
        self.assertEqual(rows[-1]["comparable"], 0)

    def test_duplicate_and_budget_and_recovery(self):
        calendar = weekdays(200)
        spec = spec_on(calendar)
        spec["budget_hours"] = 1 / 3600
        first = enqueue(spec, calendar=calendar, snapshot_id="snap", candidates_total=2)
        with self.assertRaises(RuntimeError) as caught:
            enqueue(spec, calendar=calendar, snapshot_id="snap", candidates_total=2)
        self.assertIn("duplicate", str(caught.exception))
        claimed = claim("tester")
        clock = {"t": 0.0}

        def now():
            return clock["t"]

        def run_one(_item):
            clock["t"] = 10.0
            return {"folds_completed": 1, "folds_required": 1, "net_return": 0.2, "fold": 1, "selection_row": {"order": 0, "net_return": 0.2, "max_drawdown": 0.1, "turnover": 1, "trades": 5, "reconciled": True, "distribution_unpriced": 0}}

        status = run_plan(
            claimed,
            "tester",
            [{"ordinal": 0, "phase": "training", "params": {"order": 0}}, {"ordinal": 1, "phase": "training", "params": {"order": 1}}],
            run_one,
            sleep=lambda _s: None,
            now=now,
        )
        self.assertEqual(status, "completed")
        rows = job_detail(first)["candidates"]
        self.assertEqual(rows[0]["status"], "complete")
        self.assertEqual(rows[1]["status"], "incomplete")
        self.assertIn("budget", rows[1]["error"])
        view = result_tables(first)
        self.assertTrue(view["evaluation"].empty)
        self.assertEqual(len(view["training"]), 1)

        conn = connect()
        conn.execute("UPDATE jobs SET status = 'running', owner = 'dead', heartbeat_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (first,))
        conn.commit()
        conn.close()
        recovered = claim("restarted")
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["id"], first)
        self.assertEqual(recovered["owner"], "restarted")


class ParityTests(unittest.TestCase):
    def test_baseline_and_payable_selector_match_saved_results(self):
        from lab.adapter import reproduce_baseline_a, reproduce_payable_evaluation

        baseline = reproduce_baseline_a("recent")
        self.assertAlmostEqual(baseline["ending_equity"], baseline["saved"]["ending_equity"], places=6)
        self.assertAlmostEqual(baseline["net_return"], baseline["saved"]["net_return"], places=8)
        self.assertEqual(baseline["trades"], baseline["saved"]["trades"])
        payable = reproduce_payable_evaluation()
        self.assertAlmostEqual(payable["selector_equity"], payable["selector_saved"], places=4)
        self.assertAlmostEqual(payable["baseline_equity"], payable["baseline_saved"], places=4)
        self.assertEqual(payable["selector_unpaid"], 0)


class SmokeTests(unittest.TestCase):
    def test_short_research_job_reaches_displayed_results(self):
        from lab.adapter import calendar, data_health

        days = calendar()
        spec = default_research_spec("smoke-fixed-copy")
        spec["budget_hours"] = 1
        spec["schedule"]["anchor"] = days[180]
        spec["schedule"]["warmup_sessions"] = 85
        spec["schedule"]["train_sessions"] = 20
        spec["schedule"]["eval_sessions"] = 10
        spec["schedule"]["folds"] = 1
        spec["risk"]["min_closed_trades"] = 1
        validated = validate_spec(spec, days)
        from lab.specs import planned_rows

        job_id = enqueue(validated, calendar=days, snapshot_id=data_health()["snapshot_id"], candidates_total=planned_rows(validated))
        claimed = claim("smoke")
        self.assertEqual(claimed["id"], job_id)
        status = execute(claimed, "smoke")
        self.assertEqual(status, "completed")
        view = result_tables(job_id)
        self.assertFalse(view["evaluation"].empty)
        self.assertEqual(int(view["evaluation"].iloc[0]["comparable"]), 1)
        self.assertFalse(view["training"].empty)
        self.assertEqual(int(view["training"].iloc[0]["comparable"]), 0)
        detail = job_detail(job_id)
        self.assertTrue(detail["artifacts"])
        self.assertTrue(Path(detail["artifacts"][0]["path"]).is_file())


class PlanTests(unittest.TestCase):
    def test_one_fold_grid_matches_stored_fold_and_is_not_queued(self):
        from lab.adapter import calendar, data_health
        from lab.duplicates import DuplicateSearch, find_duplicate, proposed_one_fold_spec

        spec = proposed_one_fold_spec()
        report = find_duplicate(spec, calendar())
        self.assertIsNotNone(report)
        self.assertEqual(report["fold"]["fold"], 4)
        self.assertEqual(report["fold"]["train_start"], "2024-07-12")
        self.assertEqual(report["fold"]["train_end"], "2026-07-16")
        self.assertEqual(report["fold"]["eval_start"], "2026-07-17")
        self.assertEqual(report["fold"]["eval_end"], "2026-10-14")
        self.assertEqual(report["fold"]["selected"]["ma_win"], 100)
        self.assertEqual(report["fold"]["selected"]["max_hold"], 30)
        self.assertEqual(report["fold"]["selected"]["atr_mult"], 2.0)
        self.assertEqual(len(report["fold"]["training"]), 27)
        self.assertGreater(report["fold"]["stitched_segment"]["entering_equity"], 100000)
        before = connect().execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        with self.assertRaises(DuplicateSearch):
            enqueue(spec, calendar=calendar(), snapshot_id=data_health()["snapshot_id"], candidates_total=28)
        after = connect().execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        self.assertEqual(before, after)

    def test_prepared_families_stay_unscored(self):
        from lab.families import feasibility, specifications

        for spec in specifications():
            self.assertFalse(spec["queue"])
            self.assertEqual(spec["candidates"], 3)
            blocked = default_research_spec()
            blocked["strategy"] = spec["family"]
            with self.assertRaises(SpecError) as caught:
                validate_spec(blocked, weekdays(300))
            self.assertIn("not queued", str(caught.exception))
        self.assertEqual(len(feasibility(100, "2023-01-03", "2026-10-14")), 2)

    def test_expansion_preserves_the_frozen_snapshot(self):
        import sqlite3

        from lab.expansion import ExpansionError, inventory, register_snapshot
        from lab.paths import BARS

        before = BARS.stat().st_mtime_ns
        info = inventory()
        self.assertTrue(all(row["status"] == "absent" for row in info["regimes"]))
        self.assertIn("Not opened", next(row["blocker"] for row in info["sources"] if row["name"] == "market_data.db"))
        with self.assertRaises(ExpansionError):
            register_snapshot(BARS)
        self.assertEqual(BARS.stat().st_mtime_ns, before)

        root = Path(tempfile.mkdtemp())
        bundle = root / "incoming"
        bundle.mkdir()
        db = bundle / "bars.sqlite"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE bars (symbol TEXT, session TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL)")
        conn.execute("CREATE TABLE cash_dividends (symbol TEXT, ex_date TEXT, rate REAL)")
        conn.execute("INSERT INTO bars VALUES ('SPY', '2024-01-02', 1, 1, 1, 1, 1)")
        conn.execute("INSERT INTO cash_dividends VALUES ('SPY', '2024-01-02', 1.0)")
        conn.commit()
        conn.close()
        (bundle / "manifest.json").write_text('{"source": "test", "feed": "sip", "adjustment": "split"}', encoding="utf-8")
        (bundle / "membership.csv").write_text("ticker,start_date,end_date\nSPY,2024-01-02,\n", encoding="utf-8")
        (bundle / "dividend_payments.json").write_text('{"rows": []}', encoding="utf-8")
        with self.assertRaises(ExpansionError) as caught:
            register_snapshot(db, root=root / "stored")
        self.assertIn("payable date", str(caught.exception))
        self.assertFalse((root / "stored").exists())
        self.assertEqual(BARS.stat().st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main()
