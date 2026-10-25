"""Isolated shadow-runner checks. No market database, no network, no orders."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import shadow_runner as shadow
from engine import Bar
from stitch import simulate_continuous


def px(price: float) -> dict:
    return {"open": price, "high": price + 0.4, "low": price - 0.2, "close": price}


def bundle(day: str, price: float, *, symbol: str = "AAA", dividends=None, include_symbol: bool = True) -> dict:
    bars = {"SPY": px(100.0 + price)}
    member = {}
    if include_symbol:
        bars[symbol] = px(price)
        member[symbol] = True
    return {"session": day, "bars": bars, "member": member, "dividends": dividends or []}


def dates(n: int, start: int = 1) -> list[str]:
    return [f"2024-01-{day:02d}" for day in range(start, start + n)]


def choice(mult: float, hold: int = 30) -> dict:
    return {"choice": "parameters", "selected": {"ma_win": 2, "max_hold": hold, "atr_mult": mult, "order": 0}}


class ShadowRunnerTests(unittest.TestCase):
    def runner(self, root: Path, **kwargs):
        calls = []
        hold = kwargs.pop("hold", 30)
        select_fn = kwargs.pop("selector", None)

        def default_select(fold, cutoff):
            calls.append((fold, cutoff))
            return choice(2.5 if fold == 0 else 9.0, hold)

        runner = shadow.ShadowRunner(
            root,
            warmup=[bundle(day, 10.0 + i) for i, day in enumerate(dates(2))],
            selector=select_fn or default_select,
            baseline={"ma_win": 2, "max_hold": hold, "atr_mult": 2.5},
            horizon=kwargs.pop("horizon", 6),
            fold_sessions=kwargs.pop("fold_sessions", 3),
            slip=kwargs.pop("slip", 0.0),
            initial_cash=10_000.0,
            atr_period=1,
            max_names=1,
            sleeve=1.0,
            **kwargs,
        )
        runner.calls = calls
        return runner

    def test_append_matches_open_book_and_restart_replays(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book = self.runner(root)
            shadow_days = dates(4, start=3)
            lines = []
            for i, day in enumerate(shadow_days):
                lines = book.advance(bundle(day, 12.0 + i))
            self.assertEqual(lines[0]["status"], "complete")
            self.assertTrue(lines[1]["positions"])
            text = book.decisions.read_bytes()
            again = self.runner(root)
            recovered = again.recover()
            self.assertEqual(recovered["sessions"], 4)
            self.assertEqual(book.decisions.read_bytes(), text)
            self.assertEqual(len(again.calls), 0)
            same = book.advance(bundle(shadow_days[-1], 15.0))
            self.assertEqual(same[0]["session"], shadow_days[-1])
            self.assertEqual(book.decisions.read_bytes(), text)

    def test_missing_payable_blocks_without_crediting_cash(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = self.runner(Path(tmp))
            first = book.advance(bundle("2024-01-03", 12.0))
            held = book.advance(bundle("2024-01-04", 13.0))
            cash = held[0]["cash"]
            blocked = book.advance(
                bundle(
                    "2024-01-05",
                    14.0,
                    dividends=[{"symbol": "AAA", "ex_session": "2024-01-05", "rate": 1.0}],
                )
            )
            self.assertEqual(blocked[0]["status"], "blocked")
            self.assertEqual(blocked[0]["reason"], "missing_payable")
            self.assertAlmostEqual(blocked[0]["cash"], cash)
            self.assertEqual(blocked[0]["executions"], [])
            self.assertTrue(any('"status":"blocked"' in line for line in book.decisions.read_text(encoding="utf-8").splitlines()))
            with self.assertRaises(shadow.ShadowError):
                book.advance(bundle("2024-01-06", 15.0))
            corrected = book.advance(
                bundle(
                    "2024-01-05",
                    14.0,
                    dividends=[{"symbol": "AAA", "ex_session": "2024-01-05", "pay_session": "2024-02-01", "rate": 1.0}],
                )
            )
            self.assertEqual(corrected[0]["status"], "corrected")
            self.assertGreater(corrected[0]["equity"] - corrected[0]["cash"], 0)
            self.assertAlmostEqual(corrected[0]["cash"], cash, places=4)
            self.assertIn("blocked", book.decisions.read_text(encoding="utf-8"))

    def test_missing_bar_does_not_invent_a_sale(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = self.runner(Path(tmp))
            book.advance(bundle("2024-01-03", 12.0))
            book.advance(bundle("2024-01-04", 13.0))
            blocked = book.advance(bundle("2024-01-05", 14.0, include_symbol=False))
            self.assertEqual(blocked[0]["reason"], "missing_bar")
            self.assertEqual(blocked[0]["exits"], [])
            self.assertTrue(blocked[0]["positions"])

    def test_spy_waits_for_the_payable_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = self.runner(Path(tmp), slip=5.0 / 10_000.0)
            book.advance(bundle("2024-01-03", 12.0))
            bought = book.advance(bundle("2024-01-04", 13.0))
            self.assertTrue(bought[2]["positions"])
            ex = book.advance(
                bundle(
                    "2024-01-05",
                    14.0,
                    dividends=[{"symbol": "SPY", "ex_session": "2024-01-05", "pay_session": "2024-01-06", "rate": 1.0}],
                )
            )
            self.assertTrue(ex[2]["dividends_booked"])
            self.assertEqual(ex[2]["dividends_paid"], [])
            self.assertGreater(ex[2]["equity"], ex[2]["cash"])
            paid = book.advance(bundle("2024-01-06", 15.0))
            self.assertTrue(paid[2]["dividends_paid"])
            self.assertGreater(paid[2]["positions"][0]["shares"], bought[2]["positions"][0]["shares"])

    def test_higher_slippage_replay_does_not_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = self.runner(Path(tmp), slip=5.0 / 10_000.0)
            for i, day in enumerate(dates(3, start=3)):
                book.advance(bundle(day, 12.0 + i))
            before = book.decisions.read_bytes()
            marked = book.replay_slip(10.0 / 10_000.0)
            self.assertEqual(book.decisions.read_bytes(), before)
            self.assertNotAlmostEqual(marked["selector"], book._read_jsonl(book.decisions)[-3]["equity"])

    def test_horizon_liquidates_and_then_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = self.runner(Path(tmp), horizon=3, fold_sessions=3)
            last = None
            for i, day in enumerate(dates(3, start=3)):
                last = book.advance(bundle(day, 12.0 + i))
            self.assertTrue(any(row["reason"] == "final" for row in last[0]["exits"]))
            self.assertEqual(last[0]["positions"], [])
            with self.assertRaises(shadow.ShadowError):
                book.advance(bundle("2024-01-06", 16.0))

    def test_fold_cutoff_keeps_the_old_parameters_for_that_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = self.runner(Path(tmp), horizon=4, fold_sessions=2, hold=1)
            book.advance(bundle("2024-01-03", 12.0))
            second = book.advance(bundle("2024-01-04", 13.0))
            self.assertEqual(second[0]["decision"]["spec"]["atr_mult"], 2.5)
            third = book.advance(bundle("2024-01-05", 14.0))
            self.assertEqual(third[0]["decision"]["spec"]["atr_mult"], 9.0)
            self.assertEqual(book.selection_map()[1]["cutoff"], "2024-01-04")

    def test_cash_fallback_queues_no_selector_buy(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = self.runner(Path(tmp), selector=lambda fold, cutoff: {"choice": "cash", "selected": None})
            last = None
            for i, day in enumerate(dates(3, start=3)):
                last = book.advance(bundle(day, 12.0 + i))
            self.assertEqual(last[0]["positions"], [])
            self.assertEqual(last[0]["pending"], [])
            self.assertTrue(last[2]["positions"])

    def test_tamper_stops_recovery_without_rewriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book = self.runner(root)
            book.advance(bundle("2024-01-03", 12.0))
            book.advance(bundle("2024-01-04", 13.0))
            rows = book.decisions.read_text(encoding="utf-8").splitlines()
            last = json.loads(rows[-3])
            last["equity"] = float(last["equity"]) + 100.0
            rows[-3] = json.dumps(last)
            book.decisions.write_text("\n".join(rows) + "\n", encoding="utf-8")
            stamped = book.decisions.read_bytes()
            with self.assertRaises(shadow.ShadowError):
                book.recover()
            self.assertEqual(book.decisions.read_bytes(), stamped)

    def test_snapshot_loader_detects_a_changed_bar(self):
        with tempfile.TemporaryDirectory() as tmp:
            stored = {}

            def load(session):
                return stored[session]

            root = Path(tmp)
            book = self.runner(root, embed_inputs=False, loader=load)
            for i, day in enumerate(dates(2, start=3)):
                stored[day] = bundle(day, 12.0 + i)
                book.advance(stored[day])
            restarted = self.runner(root, embed_inputs=False, loader=load)
            self.assertEqual(restarted.recover()["sessions"], 2)
            stored["2024-01-04"] = bundle("2024-01-04", 99.0)
            with self.assertRaises(shadow.ShadowError):
                restarted.recover()

    def test_terminal_liquidation_stays_the_research_default(self):
        days = dates(6)
        bars = {"AAA": [Bar(10.0 + i, 10.4 + i, 9.8 + i, 10.0 + i) for i in range(6)]}
        spec = {"ma_win": 2, "max_hold": 30, "atr_mult": 2.0}
        schedule = [None, None] + [spec] * 4
        common = dict(bars=bars, member={"AAA": [True] * 6}, dates=days, spy=[100.0] * 6, schedule=schedule, decision_start=2, atr_period=1, max_names=1, sleeve=1.0, initial_cash=10_000, slip=0.0)
        closed = simulate_continuous(**common)
        carried = simulate_continuous(**common, liquidate_terminal=False, open_book=True)
        self.assertTrue(any(row["reason"] == "final" for row in closed["trade_log"]))
        self.assertEqual(closed["ending_cash"], closed["ending_equity"])
        self.assertTrue(carried["positions"])
        self.assertGreater(carried["ending_equity"], carried["cash"] + 1.0)

    def test_training_window_stops_at_the_cutoff(self):
        calendar = dates(10)
        window = shadow.training_window(calendar, "2024-01-08", 2, 4)
        self.assertEqual(window[-1], "2024-01-08")
        self.assertEqual(len(window), 6)
        self.assertTrue(all(day <= "2024-01-08" for day in window))
        with self.assertRaises(shadow.ShadowError):
            shadow.training_window(calendar, "2024-01-03", 2, 4)

    def test_cli_does_not_start_or_enable_orders(self):
        self.assertFalse(shadow.ORDERS_ENABLED)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(shadow.main([]), 0)
        self.assertFalse(shadow.STATE_DIR.exists())
        source = Path(shadow.__file__).read_text(encoding="utf-8")
        for banned in ("submit_order", "TradingClient", "place_order", "api.alpaca"):
            self.assertNotIn(banned, source)
        self.assertEqual(shadow.normalize_dividend({"symbol": "ALL", "ex_session": "2023-03-30", "pay_session": "2023-03-17", "rate": 0.08})["pay_session"], "2023-03-30")

    def test_first_close_after_activation_is_not_backfilled(self):
        sessions = ["2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20"]
        after_close = datetime(2026, 10, 15, 20, 15, tzinfo=shadow.NY)
        self.assertEqual(shadow.first_decision_session(after_close, sessions), "2026-10-16")
        self.assertEqual(shadow.execution_session("2026-10-16", sessions), "2026-10-19")
        self.assertEqual(shadow.sessions_ready(after_close, sessions, after_close), [])
        self.assertEqual(
            shadow.sessions_ready(after_close, sessions, datetime(2026, 10, 16, 16, 0, tzinfo=shadow.NY)),
            ["2026-10-16"],
        )
        friday_night = datetime(2026, 10, 16, 20, 0, tzinfo=shadow.NY)
        self.assertEqual(shadow.first_decision_session(friday_night, sessions), "2026-10-19")
        self.assertNotIn("2026-10-15", shadow.sessions_ready(friday_night, sessions, datetime(2026, 10, 19, 16, 0, tzinfo=shadow.NY)))
        self.assertNotIn("2026-10-16", shadow.sessions_ready(friday_night, sessions, datetime(2026, 10, 19, 16, 0, tzinfo=shadow.NY)))
        with tempfile.TemporaryDirectory() as tmp:
            book = self.runner(Path(tmp), first_decision_session="2024-01-05")
            with self.assertRaises(shadow.ShadowError):
                book.advance(bundle("2024-01-03", 12.0))
            self.assertFalse(book.decisions.exists())
            self.assertIn("backfill", book.rejections.read_text(encoding="utf-8"))
            logged = book.advance(bundle("2024-01-05", 14.0))
            self.assertEqual(logged[0]["session"], "2024-01-05")
            self.assertNotIn("2024-01-03", book.decisions.read_text(encoding="utf-8"))

    def test_missing_membership_blocks_the_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = self.runner(Path(tmp))
            row = bundle("2024-01-03", 12.0)
            row["membership_status"] = "missing"
            blocked = book.advance(row)
            self.assertEqual(blocked[0]["reason"], "missing_membership")
            self.assertEqual(blocked[0]["status"], "blocked")
            self.assertEqual(blocked[0]["pending"], [])
            self.assertEqual(blocked[0]["executions"], [])

    def test_endpoint_keeps_unpaid_receivables_after_liquidation(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = self.runner(Path(tmp), horizon=3, fold_sessions=3, slip=0.0)
            book.advance(bundle("2024-01-03", 12.0))
            book.advance(bundle("2024-01-04", 13.0))
            dividends = [
                {"symbol": "AAA", "ex_session": "2024-01-05", "pay_session": "2024-02-01", "rate": 1.0},
                {"symbol": "SPY", "ex_session": "2024-01-05", "pay_session": "2024-02-01", "rate": 1.0},
            ]
            last = book.advance(bundle("2024-01-05", 14.0, dividends=dividends))
            self.assertEqual(len(last), 3)
            for line in last:
                endpoint = line["endpoint"]
                self.assertEqual(line["positions"], [])
                self.assertGreater(endpoint["unpaid_receivables"], 0)
                self.assertAlmostEqual(endpoint["equity"], endpoint["spendable_cash"] + endpoint["unpaid_receivables"])
                self.assertEqual(line["dividends_paid"], [])

    def test_ingestion_does_not_touch_frozen_or_production_data(self):
        config = Path("config.json").read_bytes()
        spec = Path("SPEC.md").read_bytes()
        protected = [
            Path(r"C:\Users\Owner\PythonTrading\stock-bot\market_data.db"),
            Path(r"C:\Users\Owner\PythonTrading\stock-bot\research\nyse_cash_abc\data\bars.sqlite"),
        ]
        before = [(path, path.stat().st_mtime_ns, path.stat().st_size) for path in protected]
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "shadow_inbox"
            complete = {
                "session": "2026-10-19",
                "bars": {"SPY": px(100.0), "AAA": px(12.0)},
                "membership": {"AAA": True},
                "dividends": [{"symbol": "AAA", "ex_session": "2026-10-19", "pay_session": "2026-11-02", "rate": 0.1}],
            }
            saved = shadow.ingest_observation(store, complete)
            self.assertFalse(saved["blocked"])
            updated = dict(complete)
            updated["session"] = "2026-10-20"
            updated["membership"] = {"AAA": False}
            shadow.ingest_observation(store, updated)
            self.assertTrue(json.loads((store / "inbox" / "2026-10-19.json").read_text(encoding="utf-8"))["observation"]["membership"]["AAA"])
            missing = {
                "session": "2026-10-21",
                "bars": None,
                "membership": None,
                "dividends": [{"symbol": "AAA", "ex_session": "2026-10-21", "rate": 0.2}],
            }
            blocked = shadow.ingest_observation(store, missing)
            self.assertTrue(blocked["blocked"])
            logged = (store / "inbox" / "blocks.jsonl").read_text(encoding="utf-8")
            for reason in ("missing_bar", "missing_membership", "missing_payable"):
                self.assertIn(reason, logged)
            with self.assertRaises(shadow.ShadowError):
                shadow.ingest_observation(protected[0], complete)
            with self.assertRaises(shadow.ShadowError):
                shadow.ingest_observation(protected[1], complete)
        self.assertEqual(Path("config.json").read_bytes(), config)
        self.assertEqual(Path("SPEC.md").read_bytes(), spec)
        after = [(path, path.stat().st_mtime_ns, path.stat().st_size) for path in protected]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
