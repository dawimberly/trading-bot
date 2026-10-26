import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("tape", ROOT / "modules" / "decision_tape.py")
tape = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tape)

# SHA-256 of ast.dump(pipeline_strategies.py) before this recorder was added.
_PRE_INSTRUMENTATION_AST = "7c9e6c3c12d5dbd68f9b20973426d7d0a329d3137043bbdfc48744a325026a97"


class StripAudit(ast.NodeTransformer):
    def visit_Try(self, node):
        body = node.body
        if (
            len(body) == 1
            and isinstance(body[0], ast.ImportFrom)
            and body[0].module == "modules.decision_tape"
        ):
            return None
        return self.generic_visit(node)

    def visit_ImportFrom(self, node):
        return None if node.module == "modules.decision_tape" else node

    def visit_FunctionDef(self, node):
        node.decorator_list = [
            d
            for d in node.decorator_list
            if not (isinstance(d, ast.Name) and d.id == "_tape_audited")
        ]
        return self.generic_visit(node)

    def visit_Expr(self, node):
        if (
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id.startswith("_tape_")
        ):
            return None
        return self.generic_visit(node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == "_tape_boost":
            return self.visit(node.args[2])
        return self.generic_visit(node)


def _function(tree, name):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(name)


def _calls_named(fn, name):
    return [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    ]


class RecorderTests(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(
            [tape.band(x) for x in [-0.011, -0.01, -0.0001, 0, 0.0099, 0.01, 0.03, 0.06, 0.06001]],
            ["outside", "B0", "B0", "B1", "B1", "B2", "B3", "B3", "B4"],
        )

    def test_boost_never_recomputes_or_changes_value(self):
        obj = object()
        self.assertIs(tape.boost("mtf", "ABC", obj), obj)

    def test_write_failure_returns_original_boost(self):
        token = tape._active.set(
            dict(id="test", book="alpaca_live", path=Path("x"), events=0, failed=False)
        )
        try:
            with patch.object(Path, "mkdir", side_effect=OSError("private detail")):
                with self.assertLogs(tape._log, level="ERROR") as logs:
                    self.assertEqual(tape.boost("rvol", "ABC", 0.135), 0.135)
            self.assertTrue(tape._active.get()["failed"])
            self.assertNotIn("private detail", "".join(logs.output))
        finally:
            tape._active.reset(token)

    def test_json_and_book_isolation(self):
        with tempfile.TemporaryDirectory() as folder:
            live = Path(folder) / "alpaca_live" / "day.jsonl"
            lab = Path(folder) / "alpaca_paper" / "day.jsonl"
            live_state = dict(id="test", book="alpaca_live", path=live, events=0, failed=False)
            token = tape._active.set(live_state)
            try:
                tape.emit("ranked", scores=[[float("nan"), "ABC"]])
            finally:
                tape._active.reset(token)
            lab_state = dict(id="lab", book="alpaca_paper", path=lab, events=0, failed=False)
            token = tape._active.set(lab_state)
            try:
                tape.emit("ranked", scores=[[1.0, "LAB"]])
            finally:
                tape._active.reset(token)
            live_row = json.loads(live.read_text())
            lab_row = json.loads(lab.read_text())
            self.assertEqual(live_row["scores"], [[None, "ABC"]])
            self.assertEqual(live_row["book"], "alpaca_live")
            self.assertEqual(lab_row["book"], "alpaca_paper")
            self.assertFalse(live_row.get("confirmed_fill", False))

    def test_unidentified_book_has_no_io(self):
        with patch.dict(tape.os.environ, {"TRADING_BOOK_ID": "unknown"}):
            with patch.object(tape, "emit", side_effect=AssertionError("unexpected write")):
                self.assertEqual(tape.audited(lambda: 42)(), 42)

    def test_backtest_bar_index_has_no_io(self):
        with patch.dict(tape.os.environ, {"TRADING_BOOK_ID": "alpaca_live"}):
            with patch.object(tape, "emit", side_effect=AssertionError("unexpected write")):
                self.assertEqual(tape.audited(lambda **kwargs: 7)(bar_idx=3), 7)

    def test_write_failure_still_returns_strategy_result(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(tape, "_root", return_value=Path(folder)):
                with patch.object(Path, "mkdir", side_effect=OSError("private detail")):
                    with patch.dict(tape.os.environ, {"TRADING_BOOK_ID": "alpaca_paper_v2"}):
                        with self.assertLogs(tape._log, level="ERROR") as logs:
                            result = tape.audited(lambda: 9)()
            self.assertEqual(result, 9)
            self.assertNotIn("private detail", "".join(logs.output))
            self.assertFalse(any(Path(folder).rglob("*.jsonl")))

    def test_strategy_exception_is_not_swallowed(self):
        def boom():
            raise RuntimeError("strategy")

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(tape, "_root", return_value=Path(folder)):
                with patch.dict(tape.os.environ, {"TRADING_BOOK_ID": "alpaca_live"}):
                    with self.assertRaises(RuntimeError):
                        tape.audited(boom)()

    def test_order_return_is_not_a_confirmed_fill(self):
        fields = tape.describe_order({"id": "abc", "status": "accepted", "queued": True})
        self.assertEqual(fields["order_id"], "abc")
        self.assertEqual(fields["status"], "accepted")
        self.assertNotIn("confirmed_fill", fields)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "alpaca_live" / "day.jsonl"
            state = dict(
                id="test",
                book="alpaca_live",
                path=path,
                events=0,
                failed=False,
                buffer=[],
            )
            token = tape._active.set(state)
            try:
                tape.note_returned("ABC", {"id": "abc", "status": "accepted"})
                tape.note_unfilled(
                    "ABC",
                    {"id": "abc", "status": "accepted", "queued": True},
                    type("Exec", (), {"defer_until_fill": lambda *a, **k: None})(),
                )
                tape._flush(state)
            finally:
                tape._active.reset(token)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([row["event"] for row in rows], ["order_returned", "order_deferred"])
        self.assertTrue(all(row["confirmed_fill"] is False for row in rows))

    def test_scanner_calls_remain_single_and_strategy_ast_matches(self):
        source = (ROOT / "modules" / "pipeline_strategies.py").read_text(encoding="utf-8")
        stripped = StripAudit().visit(ast.parse(source))
        digest = hashlib.sha256(ast.dump(stripped).encode()).hexdigest()
        self.assertEqual(digest, _PRE_INSTRUMENTATION_AST)
        ranked = _function(stripped, "_equity_momentum_candidates")
        for name in (
            "momentum_rank_boost",
            "rvol_momentum_rank_boost",
            "orb_momentum_rank_boost",
            "catalyst_momentum_rank_boost",
            "multi_timeframe_momentum_rank_boost",
        ):
            self.assertEqual(len(_calls_named(ranked, name)), 1)


if __name__ == "__main__":
    unittest.main()
