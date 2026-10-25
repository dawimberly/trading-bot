import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from lab_next.calendar import ET, bar_starts, first_open_after, is_session
from lab_next.collect import open_store, save_observation
from lab_next.engine import advance, fresh
from lab_next.provider import data_get, execution_class, normalize_actions, regular_bars, session_audit
from test_bot import at, holding, seed


def forward(moment, price, received, execution="executable"):
    payload = at("2026-10-21", moment, price=price)
    payload["feed_mode"] = "forward_observation"
    payload["execution"] = execution
    payload["received_at"] = received
    payload["action_audit"] = {
        "source": "alpaca_data_v1_corporate_actions",
        "independent": True,
        "coverage_from": "2026-10-21",
        "coverage_through": "2026-10-21",
        "symbols": ["X"],
        "events": [],
        "pages_complete": True,
    }
    return payload


class ForwardTests(unittest.TestCase):
    def test_completed_bar_does_not_fill_an_open_that_already_started(self):
        state = fresh()
        price = seed(state, "X", "10:10")
        state = advance(state, forward("10:15", price, "2026-10-21T10:20:05-04:00"))
        pending = state["books"]["baseline"]["pending"]
        self.assertEqual(pending[0]["decision_at"], "2026-10-21T10:20:05-04:00")
        self.assertEqual(state["books"]["baseline"]["positions"], {})
        state = advance(state, forward("10:20", price, "2026-10-21T10:25:02-04:00"))
        self.assertEqual(state["books"]["baseline"]["positions"], {})
        self.assertEqual(state["books"]["baseline"]["pending"][0]["decision_at"], "2026-10-21T10:20:05-04:00")
        state = advance(state, forward("10:25", price, "2026-10-21T10:30:02-04:00"))
        position = state["books"]["baseline"]["positions"]["X"]
        buy = next(event for event in state["events"] if event.get("event") == "virtual_buy")
        self.assertTrue(buy["modeled_fill"])
        self.assertEqual(buy["slippage_bps"], 5)
        self.assertTrue(buy["bar_start"].startswith("2026-10-21T10:25"))
        self.assertEqual(buy["execution_observed_at"], "2026-10-21T10:30:02-04:00")
        self.assertGreater(position["entry"], price)

    def test_in_session_late_bar_does_not_void_the_training_day(self):
        payload = forward("10:15", 100, "2026-10-21T10:20:05-04:00", execution="collect_only")
        result = advance(fresh(), payload)
        self.assertEqual(result["collect_only_days"], [])
        self.assertEqual(result["books"]["baseline"]["pending"], [])

    def test_collect_only_keeps_history_and_does_not_fill_or_train(self):
        payload = at("2026-10-21", "10:15", price=100)
        payload["execution"] = "collect_only"
        payload["feed_mode"] = "forward_observation"
        payload["received_at"] = "2026-10-21T18:00:00-04:00"
        payload["action_audit"]["source"] = "alpaca_data_v1_corporate_actions"
        payload["action_audit"]["independent"] = True
        result = advance(holding(), payload)
        self.assertEqual(result["books"]["learner"]["pending"], [])
        self.assertEqual(result["books"]["learner"]["positions"]["X"]["qty"], 10)
        self.assertEqual(len(result["history"]["X"]), 1)
        self.assertEqual(result["completed_sessions"], [])
        self.assertIn("2026-10-21", result["collect_only_days"])

    def test_dividend_cash_waits_for_payable_date(self):
        self.assertTrue(is_session("2026-10-23"))
        event = {"type": "cash_dividend", "symbol": "X", "amount": 1, "payable_date": "2026-10-23"}
        state = advance(holding(), at("2026-10-21", "09:30", price=118, events=[event]))
        book = state["books"]["learner"]
        self.assertEqual(book["dividends"], 10)
        self.assertEqual(book["cash"], 99000)
        self.assertEqual(book["receivable"], 10)
        self.assertEqual(book["positions"]["X"]["qty"], 10)
        state = advance(state, at("2026-10-22", "09:30", price=118))
        self.assertEqual(state["books"]["learner"]["cash"], 99000)
        state = advance(state, at("2026-10-23", "09:30", price=118))
        self.assertEqual(state["books"]["learner"]["cash"], 99010)
        self.assertEqual(state["books"]["learner"]["receivable"], 0)
        self.assertEqual(state["books"]["learner"]["dividends"], 10)

    def test_regular_session_drops_the_close_stamp_and_reports_gaps(self):
        day = "2026-10-21"
        clocks = bar_starts(day)
        rows = []
        for index, clock in enumerate(clocks):
            if clock == "10:15":
                continue
            hour, minute = clock.split(":")
            start = datetime(2026, 10, 21, int(hour), int(minute), tzinfo=ET)
            rows.append({"t": start.isoformat(), "o": 1, "h": 1, "l": 1, "c": 1, "v": index})
        close = datetime(2026, 10, 21, 16, 0, tzinfo=ET)
        rows.append({"t": close.isoformat(), "o": 1, "h": 1, "l": 1, "c": 1, "v": 1})
        found, gaps, extras = regular_bars({"AAPL": rows}, day, ["AAPL"])
        self.assertEqual(gaps["AAPL"], ["10:15"])
        self.assertEqual(extras, [{"symbol": "AAPL", "clock": "16:00"}])
        self.assertNotIn("16:00", found["AAPL"])
        self.assertEqual(len(found["AAPL"]), 77)

    def test_early_close_expects_42_bars(self):
        self.assertEqual(len(bar_starts("2026-11-27")), 42)
        with self.assertRaises(ValueError):
            bar_starts("2026-11-26")

    def test_closed_market_receipt_is_not_executable(self):
        start = datetime(2026, 10, 21, 15, 55, tzinfo=ET)
        received = datetime(2026, 10, 21, 19, 11, tzinfo=ET)
        self.assertEqual(execution_class(start, received, start), "collect_only")
        self.assertEqual(first_open_after(received).strftime("%Y-%m-%d %H:%M"), "2026-10-22 09:30")

    def test_actions_keep_ex_date_and_block_a_merger(self):
        pages = [
            {
                "payload": {
                    "corporate_actions": {
                        "cash_dividends": [
                            {"symbol": "MSFT", "ex_date": "2026-08-20", "payable_date": "2026-09-10", "rate": 0.91}
                        ],
                        "forward_splits": [
                            {"symbol": "AMZN", "ex_date": "2022-06-06", "old_rate": 1, "new_rate": 20}
                        ],
                        "cash_mergers": [
                            {"acquiree_symbol": "XYZ", "effective_date": "2026-10-21", "payable_date": "2026-10-21"}
                        ],
                    }
                }
            }
        ]
        events, unscoped = normalize_actions(pages)
        self.assertFalse(unscoped)
        audit = session_audit(events, ["MSFT", "AMZN", "XYZ"], "2026-10-21", True, False)
        self.assertEqual(audit["events"], [{"type": "cash_merger", "symbol": "XYZ"}])
        saved = {event["symbol"]: event for event in events}
        self.assertEqual(saved["MSFT"]["payable_date"], "2026-09-10")
        self.assertEqual(saved["AMZN"]["ratio"], 20)

    def test_observations_resume_and_keep_the_first_print(self):
        with tempfile.TemporaryDirectory() as folder:
            connection = open_store(Path(folder))
            row = {
                "symbol": "AAPL",
                "bar_start": "2026-10-21T15:55:00-04:00",
                "bar_end": "2026-10-21T16:00:00-04:00",
                "session_date": "2026-10-21",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "volume": 1,
                "feed": "sip",
                "adjustment": "raw",
                "provider": "alpaca_data",
                "received_at": "2026-10-21T19:11:00-04:00",
                "raw_sha256": "a",
            }
            self.assertEqual(save_observation(connection, row), "inserted")
            connection.commit()
            self.assertEqual(save_observation(connection, row), "duplicate")
            changed = dict(row, close=99, raw_sha256="b", received_at="2026-10-21T19:12:00-04:00")
            self.assertEqual(save_observation(connection, changed), "revised")
            connection.commit()
            stored = connection.execute("SELECT close FROM observations").fetchone()[0]
            revisions = connection.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
            connection.close()
            self.assertEqual(stored, 10.5)
            self.assertEqual(revisions, 1)

    def test_adapter_refuses_a_non_data_host(self):
        with self.assertRaises(Exception):
            data_get("https://paper-api.alpaca.markets/v2/account", {}, transport=lambda url, headers: (200, {}))


if __name__ == "__main__":
    unittest.main()
