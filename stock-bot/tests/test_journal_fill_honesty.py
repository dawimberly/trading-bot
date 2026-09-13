"""Freeze-safe journal-at-executor blotter (observational fill rows)."""

from __future__ import annotations

import csv
from pathlib import Path

from modules.paper_journal import prefer_fill_rows, row_is_entry, row_is_exit
from modules.trade_journal import _one_line, compute_realized_pnl, log_fill


def test_one_line_collapses_multiline():
    raw = 'err\n{"code": 1,\n "msg": "x"}'
    out = _one_line(raw)
    assert "\n" not in out
    assert "code" in out


def test_compute_realized_pnl_sell():
    pnl = compute_realized_pnl(2, 10.5, 10.0, is_sell=True)
    assert pnl == 1.0
    assert compute_realized_pnl(2, 10.5, 10.0, is_sell=False) is None
    assert compute_realized_pnl(2, 10.5, None, is_sell=True) is None


def test_log_fill_writes_qty_price_sleeve(tmp_path, monkeypatch):
    path = tmp_path / "paper_journal.csv"
    open_path = tmp_path / "open_trade_ids.json"
    monkeypatch.setattr("config.PAPER_JOURNAL_CSV", str(path), raising=False)
    monkeypatch.setattr(
        "modules.trade_journal._OPEN_TRADES_PATH", open_path, raising=False
    )
    log_fill(
        "TXG",
        "sell",
        qty=1.5,
        price=20.0,
        notional=30.0,
        sleeve="NYSE",
        reason="smart_atr_stop",
        order_id="abc-123",
        equity=96000.0,
        cash=24000.0,
        book="paper",
        realized_pnl=-1.25,
        realized_pnl_pct=-4.0,
        is_partial="0",
        journal_path=str(path),
    )
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    fills = [r for r in rows if r["event"] == "fill"]
    closed = [r for r in rows if r["event"] == "trade_closed"]
    assert len(fills) == 1
    assert len(closed) == 1
    row = fills[0]
    assert row["event"] == "fill"
    assert row["symbol"] == "TXG"
    assert row["side"] == "sell"
    assert row["qty"] == "1.5"
    assert row["price"] == "20.0"
    assert row["sleeve"] == "NYSE"
    assert row["order_id"] == "abc-123"
    assert row["book"] == "paper"
    assert row["realized_pnl"] == "-1.25"
    assert row["exit_reason"] == "smart_atr_stop"
    assert "\n" not in row["notes"]
    assert closed[0]["realized_pnl"] == "-1.25"
    assert closed[0]["exit_reason"] == "smart_atr_stop"


def test_prefer_fill_and_side_split():
    import pandas as pd

    df = pd.DataFrame(
        [
            {"event": "signal", "side": "buy", "symbol": "A"},
            {"event": "fill", "side": "buy", "symbol": "B"},
            {"event": "fill", "side": "sell", "symbol": "C"},
            {"event": "exit", "side": "sell", "symbol": "D"},
        ]
    )
    fills = prefer_fill_rows(df)
    assert list(fills["symbol"]) == ["B", "C"]
    assert row_is_entry("fill", "buy") is True
    assert row_is_exit("fill", "sell") is True
    assert row_is_entry("fill", "sell") is False
    assert row_is_exit("fill", "buy") is False


OLD_JOURNAL_HEADER = (
    "timestamp,event,symbol,ticker,side,regime,pair_key,z_score,"
    "equity,cash,notional,qty,price,sleeve,exit_reason,notes"
)


def test_display_sleeve_vti_and_atr_fold_nyse():
    from modules.paper_journal import display_sleeve_for_fill

    # Passive leftover names are not counted as VTI core.
    assert display_sleeve_for_fill(sleeve_raw="", symbol="VTI") == "Vanguard leftover"
    assert display_sleeve_for_fill(sleeve_raw="core", symbol="VTI") == "Vanguard leftover"
    assert display_sleeve_for_fill(sleeve_raw="NYSE", symbol="CAMT", exit_reason="smart_atr_stop") == "NYSE"
    assert display_sleeve_for_fill(sleeve_raw="smart_atr", symbol="CAMT") == "NYSE"
    assert display_sleeve_for_fill(sleeve_raw="hygiene", symbol="AAPL") == "NYSE"
    assert display_sleeve_for_fill(sleeve_raw="", symbol="AAPL") == "NYSE"
    assert display_sleeve_for_fill(sleeve_raw="ai_burst", symbol="SMCI") == "AI-burst"


def test_sleeve_pnl_vti_realized_na_until_portal_fill(tmp_path: Path):
    from modules.paper_journal import (
        VTI_REALIZED_NA,
        compute_paper_sleeve_pnl,
        format_paper_sleeve_pnl_table,
    )

    nyse_old = (
        "2026-08-19 08:52:14,fill,CAMT,CAMT,sell,,nyse_fat_loser_trim,,"
        "96784.27,24488.89,26.33,0.174809,150.594,NYSE,smart_atr_stop,"
        "nyse_fat_loser_trim,oid-1,paper,,-1.25,-1.3,0"
    )
    nyse_new = (
        "2026-08-21 10:00:00,fill,DBB,DBB,sell,,DBB,,"
        "96400.00,24000.00,50.00,2,25.00,NYSE,,"
        "notes,oid-2,paper,,-0.40,-1.6,0"
    )
    journal = tmp_path / "paper_journal.csv"
    journal.write_text(
        "\n".join([OLD_JOURNAL_HEADER, nyse_old, nyse_new]) + "\n",
        encoding="utf-8",
    )
    positions = [
        {
            "symbol": "VTI",
            "sleeve": "VTI",
            "qty": 100.0,
            "avg": 380.0,
            "mark": 377.0,
            "value": 37700.0,
            "unrealized": -300.0,
            "cost": 38000.0,
        },
        {
            "symbol": "CAMT",
            "sleeve": "NYSE",
            "qty": 1.0,
            "avg": 10.0,
            "mark": 11.0,
            "value": 11.0,
            "unrealized": 1.0,
            "cost": 10.0,
        },
    ]
    report = compute_paper_sleeve_pnl(
        journal_path=journal,
        positions=positions,
        equity=96000.0,
        cash=24000.0,
        spy_off=True,
        write_snapshot=False,
    )
    vti = report["sleeves"].get("VTI") or report["sleeves"]["Vanguard leftover"]
    nyse = report["sleeves"]["NYSE"]
    assert report["vti_fills"] == 0
    assert vti["realized_ok"] is False
    assert "leftover" in str(vti["realized_label"]).lower() or vti["realized_label"] == VTI_REALIZED_NA
    assert vti["unrealized"] == -300.0
    assert nyse["realized_ok"] is True
    assert round(nyse["realized"], 2) == -1.65
    assert round(nyse["since_realized"], 2) == -0.40
    assert report["spy_status"] == "OFF"
    text = format_paper_sleeve_pnl_table(report, compact=False)
    assert "SPY OFF" in text
    assert "Cash" in text


def test_sleeve_pnl_forward_mark_snapshot_once(tmp_path: Path):
    from modules.paper_journal import FORWARD_MARK_NAME, compute_paper_sleeve_pnl

    journal = tmp_path / "paper_journal.csv"
    journal.write_text(OLD_JOURNAL_HEADER + "\n", encoding="utf-8")
    positions = [
        {
            "symbol": "VTI",
            "sleeve": "VTI",
            "qty": 10.0,
            "avg": 100.0,
            "mark": 100.0,
            "value": 1000.0,
            "unrealized": 0.0,
            "cost": 1000.0,
        }
    ]
    first = compute_paper_sleeve_pnl(
        journal_path=journal,
        positions=positions,
        equity=10000.0,
        cash=5000.0,
        write_snapshot=True,
    )
    mark = tmp_path / FORWARD_MARK_NAME
    assert mark.is_file()
    assert first["snapshot_existed"] is False
    moved = [
        {
            "symbol": "VTI",
            "sleeve": "VTI",
            "qty": 10.0,
            "avg": 100.0,
            "mark": 110.0,
            "value": 1100.0,
            "unrealized": 100.0,
            "cost": 1000.0,
        }
    ]
    second = compute_paper_sleeve_pnl(
        journal_path=journal,
        positions=moved,
        equity=10100.0,
        cash=5000.0,
        write_snapshot=True,
    )
    assert second["snapshot_existed"] is True
    vti_s = second["sleeves"].get("VTI") or second["sleeves"]["Vanguard leftover"]
    # Mark snapshot may live beside the journal; since_unrealized is informational.
    assert "since_unrealized" in vti_s
    import json

    payload = json.loads(mark.read_text(encoding="utf-8"))
    assert float(payload["positions"][0]["mark"]) == 100.0
