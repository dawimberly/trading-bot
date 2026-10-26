"""Phase 4 Monday review scores a session without touching bots."""

from __future__ import annotations

import importlib.util
from pathlib import Path

JOURNAL = str(
    Path(__file__).resolve().parents[1]
    / "data"
    / "portal"
    / "users"
    / "dawimberly"
    / "books"
    / "alpaca_paper_v2"
    / "paper_journal.csv"
)


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "phase4_monday_review.py"
    spec = importlib.util.spec_from_file_location("phase4_monday_review", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _base(**overrides):
    snapshot = {
        "session_date": "2026-10-12",
        "journal_path": JOURNAL,
        "paper_cmds": [r"C:\Users\Owner\PythonTrading-phase4\stock-bot\run_paper_bot.py"],
        "run_all_cmds": [
            r"C:\Users\Owner\PythonTrading-phase4\stock-bot\run_all.py",
            r"C:\Users\Owner\PythonTrading\stock-bot\run_all.py",
        ],
        "live_cmd": r"C:\Users\Owner\PythonTrading\stock-bot\run_all.py",
        "paper_task_status": "Disabled",
        "journal_rows": [],
        "broker_orders": [],
        "lots": {"lots": {}},
        "exit_metadata": [],
        "paper_heartbeat": {"paper": True, "halted": False, "error": "", "age_s": 30},
        "live_heartbeat": {"paper": False, "halted": False},
    }
    snapshot.update(overrides)
    return snapshot


def test_closed_session_without_fills_is_not_yet_tested():
    mod = _load()
    result = mod.evaluate(_base())
    assert result["verdict"] == "NOT_YET_TESTED"
    statuses = {item["name"]: item["status"] for item in result["checks"]}
    assert statuses["paper_process"] == "PASS"
    assert statuses["live_process"] == "PASS"
    assert statuses["paper_restart_task"] == "PASS"
    assert statuses["broker_match"] == "NOT_YET_TESTED"
    assert statuses["realized_pnl"] == "NOT_YET_TESTED"
    assert statuses["exit_metadata"] == "NOT_YET_TESTED"


def test_trade_closed_dollars_fail_the_session():
    mod = _load()
    rows = [
        {
            "timestamp": "2026-10-12 10:01:00",
            "event": "fill",
            "side": "sell",
            "symbol": "ABC",
            "order_id": "ord-1",
            "realized_pnl": "-12.50",
        },
        {
            "timestamp": "2026-10-12 10:01:01",
            "event": "trade_closed",
            "side": "sell",
            "symbol": "ABC",
            "order_id": "ord-1",
            "realized_pnl": "-12.50",
        },
    ]
    result = mod.evaluate(
        _base(
            journal_rows=rows,
            broker_orders=[{"id": "ord-1", "symbol": "ABC", "status": "filled", "filled_qty": "5"}],
            exit_metadata=[
                {
                    "timestamp": "2026-10-12 10:01:00",
                    "order_id": "ord-1",
                    "realized_pnl": "-12.50",
                    "pnl_role": "slice",
                }
            ],
        )
    )
    assert result["verdict"] == "FAIL"
    statuses = {item["name"]: item["status"] for item in result["checks"]}
    assert statuses["trade_closed_pnl"] == "FAIL"


def test_matching_sell_is_a_pass():
    mod = _load()
    rows = [
        {
            "timestamp": "2026-10-12 10:01:00",
            "event": "fill",
            "side": "sell",
            "symbol": "ABC",
            "order_id": "ord-1",
            "realized_pnl": "-12.50",
        },
        {
            "timestamp": "2026-10-12 10:01:01",
            "event": "trade_closed",
            "side": "sell",
            "symbol": "ABC",
            "order_id": "ord-1",
            "realized_pnl": "",
        },
    ]
    result = mod.evaluate(
        _base(
            journal_rows=rows,
            broker_orders=[{"id": "ord-1", "symbol": "ABC", "status": "filled", "filled_qty": "5"}],
            exit_metadata=[
                {
                    "timestamp": "2026-10-12 10:01:00",
                    "order_id": "ord-1",
                    "realized_pnl": "-12.50",
                    "pnl_role": "slice",
                }
            ],
        )
    )
    assert result["verdict"] == "PASS"
    assert result["fill_realized_pnl"] == -12.5


def test_enabled_restart_task_fails():
    mod = _load()
    result = mod.evaluate(_base(paper_task_status="Ready"))
    statuses = {item["name"]: item["status"] for item in result["checks"]}
    assert statuses["paper_restart_task"] == "FAIL"
    assert result["verdict"] == "FAIL"
