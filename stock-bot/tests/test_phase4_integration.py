"""Isolated checks for the phase4 integration candidate."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "analysis"))

import weekly_review as weekly_review  # noqa: E402
from modules.alpaca_executor import AlpacaExecutor  # noqa: E402
from modules.position_exits import run_position_exits  # noqa: E402
from modules.trade_journal import (  # noqa: E402
    exit_metadata_path,
    log_fill,
    restore_journal_sidecars,
    snapshot_journal_sidecars,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "phase4_paper_journal_anon.csv"


def test_nyse_min_ticket_preserved_when_multiplier_crushes_size():
    executor = AlpacaExecutor.__new__(AlpacaExecutor)
    executor._min_notional = lambda: 100.0
    executor._wisdom_sizing_multiplier = 0.1
    executor._short_long_hedge_mult = 1.0
    executor._sleeve_pnl = None

    assert executor._apply_sizing_multiplier(250.0, sleeve_key="nyse") == 100.0
    assert executor._apply_sizing_multiplier(250.0, sleeve_key="NYSE") == 100.0
    assert executor._apply_sizing_multiplier(250.0, sleeve_key="crypto") is None
    assert executor._apply_sizing_multiplier(50.0, sleeve_key="nyse") is None

    executor._wisdom_sizing_multiplier = 1.0
    assert executor._apply_sizing_multiplier(250.0, sleeve_key="nyse") == 250.0


def _sidecar_rows(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    return [json.loads(line) for line in text.splitlines()]


def test_estimated_qty_is_not_treated_as_broker_confirmed(tmp_path):
    journal = tmp_path / "journal.csv"
    log_fill("AAA", "buy", qty=2, price=10, order_id="buy-1", journal_path=journal)
    log_fill(
        "AAA",
        "sell",
        qty=2,
        price=11,
        order_id="sell-est",
        journal_path=journal,
        order_status="filled",
        broker_qty_source="estimated",
        estimated_qty_remaining=0,
        broker_qty_remaining=0,
    )
    log_fill("BBB", "buy", qty=2, price=10, order_id="buy-2", journal_path=journal)
    log_fill(
        "BBB",
        "sell",
        qty=2,
        price=11,
        order_id="sell-conf",
        journal_path=journal,
        order_status="filled",
        broker_qty_source="confirmed",
        broker_qty_remaining=0,
    )
    rows = _sidecar_rows(exit_metadata_path(journal))
    estimated = next(row for row in rows if row["order_id"] == "sell-est")
    confirmed = next(row for row in rows if row["order_id"] == "sell-conf")
    assert estimated["broker_qty_source"] == "estimated"
    assert estimated["broker_qty_remaining"] is None
    assert estimated["estimated_qty_remaining"] == 0
    assert estimated["exit_state"] == "unknown"
    assert estimated["exit_complete"] is None
    assert confirmed["broker_qty_source"] == "confirmed"
    assert confirmed["broker_qty_remaining"] == 0
    assert confirmed["exit_state"] == "complete"
    assert confirmed["exit_complete"] is True


def test_executor_passes_estimate_separately_from_confirmation(monkeypatch):
    captured = []

    def fake_log_fill(*_args, **kwargs):
        captured.append(kwargs)

    monkeypatch.setattr("modules.trade_journal.log_fill", fake_log_fill)
    executor = AlpacaExecutor.__new__(AlpacaExecutor)
    executor.paper = True
    executor._account = SimpleNamespace(cash=100)
    executor._journal_fill(
        symbol="AAA",
        side="sell",
        qty=2,
        price=11,
        notional=22,
        order_id="ord-est",
        partial=False,
        ctx={"position_qty_before": 10, "entry_price": 10, "order_status": "filled"},
        equity_after=1000,
    )
    executor._journal_fill(
        symbol="AAA",
        side="sell",
        qty=2,
        price=11,
        notional=22,
        order_id="ord-conf",
        partial=False,
        ctx={
            "position_qty_before": 10,
            "broker_qty_confirmed": 0,
            "entry_price": 10,
            "order_status": "filled",
        },
        equity_after=1000,
    )
    estimated, confirmed = captured
    assert estimated["broker_qty_source"] == "estimated"
    assert estimated["broker_qty_remaining"] is None
    assert estimated["estimated_qty_remaining"] == 8
    assert confirmed["broker_qty_source"] == "confirmed"
    assert confirmed["broker_qty_remaining"] == 0
    assert confirmed["estimated_qty_remaining"] == 8


def test_already_filled_order_does_not_reread_positions():
    executor = AlpacaExecutor.__new__(AlpacaExecutor)
    executor._notified_order_ids = set()
    executor._order_notify_ctx = {}
    executor._emit_fill_notification = lambda *_args, **_kwargs: None

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("already-filled order touched the broker again")

    executor.refresh_cache = _forbidden
    executor._get_positions = _forbidden
    executor.client = SimpleNamespace(get_order_by_id=_forbidden)
    order = SimpleNamespace(
        id="filled-1",
        symbol="AAA",
        filled_qty="2",
        filled_avg_price="10",
        status="filled",
    )
    details = executor.order_fill_details(order, max_wait=5)
    assert details["filled"] is True
    assert details["qty"] == 2


def _exit_executor(position, calls):
    executor = AlpacaExecutor.__new__(AlpacaExecutor)
    executor._get_positions = lambda: [position]
    executor._get_account = lambda: SimpleNamespace(equity=100000)
    executor._exit_peak_prices = {}
    executor._exit_opt_meta = {}

    def _exit(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(id="exit-1")

    executor.execute_full_exit = _exit
    executor.order_filled = lambda _order: True
    return executor


def test_unknown_journal_row_does_not_change_broker_exit(monkeypatch, tmp_path):
    import config

    monkeypatch.setattr(config, "vti_core_enabled", lambda: False)
    monkeypatch.setattr(config, "is_crypto", lambda _symbol: False)
    monkeypatch.setattr(config, "paper_lab_concentrated_enabled", lambda: False)
    monkeypatch.setattr(config, "effective_smart_stops_enabled", lambda: False)
    monkeypatch.setattr(config, "effective_exit_optimization_enabled", lambda: False)
    monkeypatch.setattr(config, "paper_aggressive_context", lambda: False)
    monkeypatch.setattr(config, "effective_paper_momentum_quality_fixes", lambda: False)

    journal = tmp_path / "journal.csv"
    journal.write_text(
        "timestamp,event,symbol,is_partial\n"
        "2026-10-09 11:00:00,fill,AAA,unknown\n",
        encoding="utf-8",
    )

    class Journal:
        def log_exit(self, *_args, **_kwargs):
            return None

        def log_event(self, *_args, **_kwargs):
            return None

    losing = SimpleNamespace(
        symbol="AAA",
        qty=10,
        avg_entry_price=100,
        current_price=90,
        unrealized_plpc=-0.10,
    )
    healthy = SimpleNamespace(
        symbol="AAA",
        qty=10,
        avg_entry_price=100,
        current_price=101,
        unrealized_plpc=0.01,
    )
    stopped = []
    held = []
    assert run_position_exits(_exit_executor(losing, stopped), SimpleNamespace(_log_event=lambda *_a, **_k: None), Journal(), journal_path=str(journal)) == 1
    assert run_position_exits(_exit_executor(healthy, held), SimpleNamespace(_log_event=lambda *_a, **_k: None), Journal(), journal_path=str(journal)) == 0
    assert len(stopped) == 1
    assert stopped[0][0][0] == "AAA"
    assert stopped[0][1]["reason"] == "stop_loss"
    assert held == []

    stopped_without_journal = []
    assert run_position_exits(
        _exit_executor(losing, stopped_without_journal),
        SimpleNamespace(_log_event=lambda *_a, **_k: None),
    ) == 1
    assert stopped_without_journal[0][1]["reason"] == stopped[0][1]["reason"]


def test_anonymized_paper_journal_fixture_reconciliation():
    frame = pd.read_csv(FIXTURE)
    report = weekly_review.reconcile_realized(frame)
    assert report["duplicate_fill_count"] == 1
    assert report["raw_fill_total"] == 2.0
    assert report["deduplicated_fill_total"] == -5.0
    assert report["suppressed_marker_count"] == 1
    assert report["suppressed_marker_total"] == -10.0
    assert report["confident_total"] == 1.0
    assert report["unattributed_total"] == -8.0
    uncertain = [
        row for row in report["ambiguous_records"] if row["reason"] == "indistinguishable_executions"
    ]
    assert [row["event"] for row in uncertain] == ["fill", "fill", "trade_closed"]
    assert [row["dollars"] for row in uncertain] == [-2.0, -4.0, -2.0]
    assert sorted(report["confident_frame"]["realized_pnl"].dropna().tolist()) == [-10.0, 1.5, 2.5, 7.0]


def test_sidecar_rollback_restores_lots_and_leaves_the_csv(tmp_path):
    journal = tmp_path / "paper_journal.csv"
    log_fill("AAA", "buy", qty=10, price=5, order_id="buy-1", journal_path=journal)
    snapshot = snapshot_journal_sidecars(journal)
    csv_after_buy = journal.read_bytes()
    log_fill(
        "AAA",
        "sell",
        qty=4,
        price=6,
        order_id="sell-1",
        journal_path=journal,
        order_status="filled",
        broker_qty_source="estimated",
        estimated_qty_remaining=6,
    )
    assert journal.read_bytes() != csv_after_buy
    before_restore = journal.read_bytes()
    restore_journal_sidecars(journal, snapshot)
    assert journal.read_bytes() == before_restore
    lots = json.loads((tmp_path / "open_trade_ids.json").read_bytes())
    assert lots == json.loads(snapshot["files"]["open_trade_ids.json"])
    assert not exit_metadata_path(journal).exists()


def test_sidecar_rollback_refuses_a_portal_journal(tmp_path):
    portal = tmp_path / "data" / "portal" / "users" / "anon" / "books" / "paper" / "paper_journal.csv"
    portal.parent.mkdir(parents=True)
    portal.write_text("timestamp,event\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="portal journal"):
        snapshot_journal_sidecars(portal)
