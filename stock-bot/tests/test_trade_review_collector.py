"""Collector, evidence, and review-view regressions. No broker and no engine."""

from __future__ import annotations

import ast
import hashlib
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules import post_sale_review as review
from modules import trade_review_collect as collect
from modules import trade_review_view as view

# Current _submit_order with the observational plan hooks removed. Matches HEAD.
PRE_HOOK_SUBMIT_AST = "545e42376af0ba51827a322fb997cc24623414b4b60c406d6ee665dbb27980de"


def _fill(ident, order_id, side, qty, price, when, symbol="ABC"):
    return {
        "id": ident,
        "activity_type": "FILL",
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "qty": str(qty),
        "price": str(price),
        "transaction_time": when,
        "order_status": "filled",
        "leaves_qty": "0",
    }


class PaginationTests(unittest.TestCase):
    def test_short_page_is_complete_and_duplicate_ids_collapse(self):
        pages = [
            [_fill("a", "o", "buy", 1, 10, "2026-10-01T14:00:00Z"), _fill("a", "o", "buy", 1, 10, "2026-10-01T14:00:00Z")],
            [_fill("b", "o2", "sell", 1, 11, "2026-10-02T14:00:00Z")],
        ]

        def fetch(token):
            if token is None:
                return pages[0]
            if token == "a":
                return pages[1]
            return []

        result = collect.paginate_activities(fetch, page_size=2, max_pages=5)
        self.assertTrue(result["complete_from_start"])
        self.assertEqual([row["id"] for row in result["rows"]], ["a", "b"])
        self.assertFalse(result["truncated"])

    def test_full_page_budget_is_truncation_not_complete_history(self):
        def fetch(token):
            ident = "1" if token is None else "2"
            return [_fill(ident, ident, "buy", 1, 10, "2026-10-01T14:00:00Z")]

        result = collect.paginate_activities(fetch, page_size=1, max_pages=1)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["complete_from_start"])

    def test_repeated_page_is_stuck_not_complete(self):
        page = [
            _fill("a", "o", "buy", 1, 10, "2026-10-01T14:00:00Z"),
            _fill("b", "o2", "buy", 1, 11, "2026-10-01T14:01:00Z"),
        ]

        def fetch(token):
            return page

        result = collect.paginate_activities(fetch, page_size=2, max_pages=5)
        self.assertEqual(result["error"], "pagination_token_stuck")
        self.assertFalse(result["complete_from_start"])
        self.assertFalse(result["truncated"])
        self.assertEqual([row["id"] for row in result["rows"]], ["a", "b"])

    def test_conflicting_duplicate_does_not_overwrite(self):
        first = _fill("a", "o", "buy", 1, 10, "2026-10-01T14:00:00Z")
        clash = _fill("a", "o", "buy", 1, 99, "2026-10-01T14:00:00Z")
        merged, conflicts = collect.merge_executions([first], [clash])
        self.assertEqual(conflicts, ["a"])
        self.assertEqual(merged[0]["price"], "10")


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.days = []
        day = datetime(2026, 8, 3, 20, tzinfo=timezone.utc)
        while len(self.days) < 8:
            if day.weekday() < 5:
                self.days.append(day)
            day += timedelta(days=1)
        self.calendar = [{"date": d.date().isoformat(), "close": d.isoformat()} for d in self.days]
        self.fills = [
            _fill("b", "b", "buy", 10, 100, (self.days[0] - timedelta(hours=1)).isoformat()),
            _fill("s", "s", "sell", 5, 100, (self.days[1] - timedelta(hours=1)).isoformat()),
        ]

    def test_adjustment_flag_without_raw_evidence_cannot_verify_prices(self):
        block = collect.price_block(
            bars=[{"date": self.days[2].date().isoformat(), "close": 110, "high": 111, "low": 109}],
            adjustment="split",
            pages_complete=True,
            actions_complete=True,
            actions_classified={
                "position_changing_action_dates": [],
                "uncertain_action_dates": [],
                "dividend_dates_not_in_price": [],
            },
            checked_from=self.days[0].date().isoformat(),
            checked_through=self.days[-1].date().isoformat(),
            feed="iex",
        )
        block["unit_alignment_verified"] = True
        cleaned = collect.enforce_price_evidence({"ABC": block})
        self.assertFalse(cleaned["ABC"]["unit_alignment_verified"])
        sale = review.post_sale_report(
            book="alpaca_live",
            fills=self.fills,
            calendar=self.calendar,
            prices=cleaned,
            as_of=self.days[-1].isoformat(),
            initial_inventory={},
        )["sales"][0]
        self.assertIsNone(sale["milestones"]["1"]["price_return"])
        self.assertIn("price_units_unverified", sale["milestones"]["1"]["unavailable_reasons"])

    def test_missing_session_is_not_filled_from_a_later_bar(self):
        bars = []
        for index, day in enumerate(self.days):
            if index == 6:
                continue
            bars.append({"date": day.date().isoformat(), "close": 100 + index, "high": 101 + index, "low": 99 + index})
        prices = {
            "ABC": collect.price_block(
                bars=bars,
                adjustment="raw",
                pages_complete=True,
                actions_complete=True,
                actions_classified={
                    "position_changing_action_dates": [],
                    "uncertain_action_dates": [],
                    "dividend_dates_not_in_price": ["2026-08-04"],
                },
                checked_from=self.days[0].date().isoformat(),
                checked_through=self.days[-1].date().isoformat(),
                feed="iex",
            )
        }
        sale = review.post_sale_report(
            book="alpaca_live",
            fills=self.fills,
            calendar=self.calendar,
            prices=collect.enforce_price_evidence(prices),
            as_of=self.days[-1].isoformat(),
            initial_inventory={},
        )["sales"][0]
        self.assertIsNone(sale["milestones"]["5"]["price_return"])
        self.assertFalse(sale["complete_30_sessions"])
        self.assertEqual(sale["dividends"], "not_included")

    def test_split_blocks_the_comparison(self):
        prices = {
            "ABC": collect.price_block(
                bars=[
                    {"date": day.date().isoformat(), "close": 100, "high": 101, "low": 99}
                    for day in self.days
                ],
                adjustment="raw",
                pages_complete=True,
                actions_complete=True,
                actions_classified={
                    "position_changing_action_dates": [self.days[2].date().isoformat()],
                    "uncertain_action_dates": [],
                    "dividend_dates_not_in_price": [],
                },
                checked_from=self.days[0].date().isoformat(),
                checked_through=self.days[-1].date().isoformat(),
                feed="iex",
            )
        }
        sale = review.post_sale_report(
            book="alpaca_live",
            fills=self.fills,
            calendar=self.calendar,
            prices=prices,
            as_of=self.days[-1].isoformat(),
            initial_inventory={},
        )["sales"][0]
        self.assertIn(
            "position_changing_corporate_action",
            sale["milestones"]["1"]["unavailable_reasons"],
        )

    def test_unnamed_corporate_action_is_not_treated_as_verified(self):
        payload = {"corporate_actions": {"forward_splits": [{"ex_date": "2026-08-05", "new_rate": 2, "old_rate": 1}]}}
        self.assertIsNone(collect._actions_for_symbol(payload, "ABC"))

    def test_rename_fields_scope_to_the_named_symbol_only(self):
        payload = {
            "corporate_actions": {
                "name_changes": [
                    {"old_symbol": "OLD", "new_symbol": "NEW", "process_date": "2026-08-05"}
                ]
            }
        }
        other = collect._actions_for_symbol(payload, "ABC")
        renamed = collect._actions_for_symbol(payload, "NEW")
        self.assertEqual(other["position_changing_action_dates"], [])
        self.assertEqual(other["uncertain_action_dates"], [])
        self.assertEqual(renamed["uncertain_action_dates"], ["2026-08-05"])


class IsolationTests(unittest.TestCase):
    def _transport(self, account_id: str):
        when = "2026-10-01T15:00:00Z"

        def transport(url, headers):
            self.assertNotIn("APCA-API-SECRET-KEY", url)
            if url.endswith("/v2/account"):
                return 200, {"id": account_id, "account_number": "123456789"}, None
            if "/v2/account/activities" in url:
                return 200, [_fill("only", "only", "buy", 1, 10, when)], None
            if url.endswith("/v2/positions"):
                return 200, [], None
            if "/v2/calendar" in url:
                return 200, [{"date": "2026-10-01", "open": "09:30", "close": "16:00"}], None
            return 404, None, "missing"

        return transport

    def test_account_change_does_not_merge_fills(self):
        env = {"PAPER_TRADING": "false", "APCA_API_KEY_ID": "key", "APCA_API_SECRET_KEY": "secret"}
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = collect.refresh_book(
                book="alpaca_live",
                env=env,
                root=root,
                transport=self._transport("account-a"),
                as_of=datetime(2026, 10, 2, tzinfo=timezone.utc),
                max_pages=5,
            )
            self.assertEqual(first["fills_stored"], 1)
            stored = (root / "data" / "trade_review" / "alpaca_live" / "evidence" / "fills.jsonl").read_text(
                encoding="utf-8"
            )
            second = collect.refresh_book(
                book="alpaca_live",
                env=env,
                root=root,
                transport=self._transport("account-b"),
                as_of=datetime(2026, 10, 2, tzinfo=timezone.utc),
                max_pages=5,
            )
            self.assertEqual(second["error"], "account_isolation_mismatch")
            self.assertEqual(
                (root / "data" / "trade_review" / "alpaca_live" / "evidence" / "fills.jsonl").read_text(
                    encoding="utf-8"
                ),
                stored,
            )
            report = json.loads((root / "reports" / "trade_review" / "alpaca_live.json").read_text(encoding="utf-8"))
            self.assertEqual(report["book"], "alpaca_live")
            self.assertEqual(report["error"], "account_isolation_mismatch")

    def test_paper_flag_mismatch_refuses_the_host(self):
        with self.assertRaises(collect.CollectionError):
            collect.trading_base("alpaca_live", {"PAPER_TRADING": "true"})


class InventoryAndOpenPositionTests(unittest.TestCase):
    def test_unresolved_corporate_action_keeps_inventory_unknown(self):
        inventory, label, detail = collect.starting_inventory(
            True,
            {
                "complete": True,
                "unscoped": False,
                "position_changing_dates": ["2026-07-02"],
                "uncertain_dates": [],
            },
        )
        self.assertIsNone(inventory)
        self.assertEqual(label, "unknown")
        self.assertEqual(detail, "position_changing_corporate_action_not_applied")
        incomplete, incomplete_label, _ = collect.starting_inventory(False, None)
        self.assertIsNone(incomplete)
        self.assertEqual(incomplete_label, "unknown")

    def test_resolved_empty_actions_can_be_flat(self):
        inventory, label, detail = collect.starting_inventory(
            True,
            {
                "complete": True,
                "unscoped": False,
                "position_changing_dates": [],
                "uncertain_dates": [],
            },
        )
        self.assertEqual(inventory, {})
        self.assertEqual(label, "flat_at_start_of_complete_fill_history")
        self.assertEqual(detail, "no_unresolved_position_changing_actions")

    def test_open_meta_is_not_a_sale_and_older_sales_stay(self):
        older = {
            "order_id": "older-meta-sale",
            "symbol": "META",
            "position_effect": "partial_position_sale",
            "fill_price": 587.382,
        }
        report = {"purchases": [], "sales": [older]}
        collect.annotate_unsold_positions(
            report,
            [{"symbol": "META", "qty": "0.060009066", "avg_entry_price": "735.22"}],
        )
        self.assertEqual(report["sales"], [older])
        self.assertNotIn("sale_follow_through", report["sales"][0])
        self.assertEqual(len(report["open_positions"]), 1)
        self.assertEqual(
            report["open_positions"][0]["sale_follow_through"],
            "not applicable—no sale yet.",
        )
        self.assertIsNone(report["open_positions"][0]["original_plan"])
        text = view.render_dashboard(
            {
                "book": "alpaca_live",
                "purchases": [],
                "sales": [older | {"quantity": 0.001821, "sale_date_et": "2026-08-13",
                                   "milestones": {}, "extrema_basis": "daily_high_low",
                                   "observed_sessions": 0, "elapsed_sessions": 0,
                                   "complete_30_sessions": False, "lowest_move": None,
                                   "highest_move": None,
                                   "replacement_status": "not_attributable_from_next_buy_alone",
                                   "adherence": "unknown_without_linked_original_plan_and_exit_decision"}],
                "open_positions": report["open_positions"],
                "coverage": {"initial_inventory": "unknown", "expected_drawdown_rule": "not_activated"},
            },
            selected_book="alpaca_live",
            meta={"loaded_at": "now", "banner": "file"},
        )
        self.assertIn("not applicable—no sale yet.", text)
        self.assertIn("partial_position_sale", text)
        self.assertEqual(text.count("not applicable—no sale yet."), 1)
        sale_at = text.index("Sales — price follow-through only")
        open_at = text.index("Open positions — not sales")
        self.assertLess(open_at, sale_at)
        self.assertNotIn("not applicable—no sale yet.", text[sale_at:])


class HookAndViewTests(unittest.TestCase):
    def test_stripping_plan_hooks_restores_submit_order_ast(self):
        source = (ROOT / "modules" / "alpaca_executor.py").read_text(encoding="utf-8")
        kept = []
        skipping = False
        for line in source.splitlines(keepends=True):
            if "OBSERVATIONAL_PLAN_HOOK_BEGIN" in line:
                skipping = True
                continue
            if "OBSERVATIONAL_PLAN_HOOK_END" in line:
                skipping = False
                continue
            if not skipping:
                kept.append(line)
        tree = ast.parse("".join(kept))
        function = next(
            node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_submit_order"
        )
        digest = hashlib.sha256(ast.dump(function, include_attributes=False).encode()).hexdigest()
        self.assertEqual(digest, PRE_HOOK_SUBMIT_AST)

    def test_book_switch_drops_the_previous_books_report(self):
        board = view.ReviewBoard()
        first = board.begin_switch("alpaca_live")
        second = board.begin_switch("alpaca_paper_v2")
        live_report = {"book": "alpaca_live", "purchases": [{"symbol": "META"}], "sales": []}
        self.assertFalse(board.apply(first, "alpaca_live", live_report, {"loaded_at": "t", "banner": "live"}))
        self.assertNotIn("META", board.text)
        medium = {"book": "alpaca_paper_v2", "purchases": [], "sales": [], "coverage": {}}
        self.assertTrue(board.apply(second, "alpaca_paper_v2", medium, {"loaded_at": "t2", "banner": "medium"}))
        self.assertIn("alpaca_paper_v2", board.text)
        self.assertNotIn("META", board.text)

    def test_later_rise_is_not_called_a_wrong_sale(self):
        report = {
            "book": "alpaca_live",
            "as_of": "2026-10-17T20:00:00+00:00",
            "purchases": [],
            "sales": [
                {
                    "symbol": "META",
                    "position_effect": "partial_position_sale",
                    "fill_price": 100,
                    "quantity": 1,
                    "sale_date_et": "2026-10-01",
                    "milestones": {
                        "1": {"price_return": 0.05, "unavailable_reasons": []},
                        "5": {"price_return": None, "unavailable_reasons": ["not_mature"]},
                        "10": {"price_return": None, "unavailable_reasons": ["not_mature"]},
                        "20": {"price_return": None, "unavailable_reasons": ["not_mature"]},
                        "30": {"price_return": None, "unavailable_reasons": ["not_mature"]},
                    },
                    "lowest_move": -0.01,
                    "highest_move": 0.08,
                    "extrema_basis": "daily_high_low",
                    "observed_sessions": 1,
                    "elapsed_sessions": 1,
                    "complete_30_sessions": False,
                    "replacement_status": "not_attributable_from_next_buy_alone",
                    "adherence": "unknown_without_linked_original_plan_and_exit_decision",
                }
            ],
            "coverage": {"complete_from_start": False, "expected_drawdown_rule": "not_activated"},
        }
        text = view.render_dashboard(report, selected_book="alpaca_live", meta={"loaded_at": "now", "banner": "file"})
        self.assertIn("+1 +5.00%", text)
        self.assertIn("not a verdict on the sale", text)
        self.assertNotIn("wrong", text.lower())
        self.assertIn("Plan adherence", text)
        self.assertIn("original plan unavailable", text)

    def test_mismatched_file_is_not_rendered_as_this_book(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = view.report_file(root, "alpaca_live")
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"book": "alpaca_paper", "purchases": [], "sales": []}), encoding="utf-8")
            payload, meta = view.load_prepared_report(root, "alpaca_live")
            self.assertIsNone(payload)
            self.assertIn("Not shown", meta["banner"])

    def test_collector_transport_issues_get_only(self):
        seen = {}

        def fake_open(request, timeout=0):
            seen["method"] = request.get_method()
            raise AssertionError("stop")

        with patch("modules.trade_review_collect.urllib.request.urlopen", fake_open):
            with self.assertRaises(AssertionError):
                collect.http_get_json("https://example.test/v2/account", {})
        self.assertEqual(seen["method"], "GET")
        blocked = Request("https://example.test", method="POST")
        self.assertNotEqual(blocked.get_method(), "GET")

    def test_view_module_does_not_fetch_or_trade(self):
        source = (ROOT / "modules" / "trade_review_view.py").read_text(encoding="utf-8")
        self.assertNotIn("submit_order", source)
        self.assertNotIn("urlopen", source)
        self.assertNotIn("trade_review_collect", source)


if __name__ == "__main__":
    unittest.main()
