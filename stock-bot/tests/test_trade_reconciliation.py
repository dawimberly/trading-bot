"""Path + trailing-field mapping for trade_reconciliation (read-only)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "analysis"))

import trade_reconciliation as recon  # noqa: E402

OLD_HEADER = (
    "timestamp,event,symbol,ticker,side,regime,pair_key,z_score,"
    "equity,cash,notional,qty,price,sleeve,exit_reason,notes"
)
SELL_FILL = (
    "2026-08-19 08:52:14,fill,CAMT,CAMT,sell,,nyse_fat_loser_trim,,"
    "96784.27,24488.89,26.33,0.174809,150.594,NYSE,nyse_fat_loser_trim,"
    "nyse_fat_loser_trim,a54a5b0e-36cd-4109-9a3e-58cec1645fc3,paper,,-0.35,-1.3123,0"
)
BUY_FILL = (
    "2026-08-19 08:38:04,fill,DBB,DBB,buy,,DBB/MA50|rvol+mtf,,"
    "96974.06,24462.56,328.13,13.03137411,25.18,NYSE,,"
    "DBB/MA50|rvol+mtf,df14b5d4-a4ee-4b54-95e2-0953809627bf,paper,,,,0"
)
CYCLE = "2026-08-19 08:19:05,cycle,,,,RHYME_A: Euphoric_Volatility,,,97026.5,24742.3,,,,,,notes"


def test_read_maps_trailing_realized_pnl(tmp_path: Path):
    p = tmp_path / "paper_journal.csv"
    p.write_text("\n".join([OLD_HEADER, CYCLE, BUY_FILL, SELL_FILL]) + "\n", encoding="utf-8")
    df, warnings = recon.read_journal_csv(p)
    assert any("mapped trailing extras" in w for w in warnings)
    assert "realized_pnl" in df.columns
    fills = df.loc[df["event"] == "fill"]
    camt = fills.loc[fills["symbol"] == "CAMT"].iloc[0]
    assert camt["realized_pnl"] == "-0.35"
    assert camt["order_id"].startswith("a54a5b0e")
    assert camt["book"] == "paper"
    dbb = fills.loc[fills["symbol"] == "DBB"].iloc[0]
    assert not recon._nonempty_cell(dbb["realized_pnl"])


def test_read_named_realized_pnl_header(tmp_path: Path):
    p = tmp_path / "paper_journal.csv"
    header = ",".join(recon.JOURNAL_FIELDS)
    row = {k: "" for k in recon.JOURNAL_FIELDS}
    row.update(
        {
            "timestamp": "2026-08-19 08:52:14",
            "event": "fill",
            "symbol": "CAMT",
            "side": "sell",
            "realized_pnl": "-1.25",
        }
    )
    p.write_text(header + "\n" + ",".join(str(row[k]) for k in recon.JOURNAL_FIELDS) + "\n", encoding="utf-8")
    df, warnings = recon.read_journal_csv(p)
    assert df.iloc[0]["realized_pnl"] == "-1.25"
    assert not any("mapped trailing extras" in w for w in warnings)


def test_resolve_prefers_portal(tmp_path: Path, monkeypatch):
    portal = tmp_path / "portal" / "paper_journal.csv"
    root = tmp_path / "paper_journal.csv"
    portal.parent.mkdir(parents=True)
    portal.write_text("timestamp,event\n", encoding="utf-8")
    root.write_text("timestamp,event\n", encoding="utf-8")
    monkeypatch.setattr(recon, "PORTAL_PAPER_JOURNAL", portal)
    monkeypatch.setattr(recon, "ROOT_PAPER_JOURNAL", root)
    assert recon.resolve_paper_journal_path() == portal


def test_resolve_falls_back_to_root(tmp_path: Path, monkeypatch):
    portal = tmp_path / "missing" / "paper_journal.csv"
    root = tmp_path / "paper_journal.csv"
    root.write_text("timestamp,event\n", encoding="utf-8")
    monkeypatch.setattr(recon, "PORTAL_PAPER_JOURNAL", portal)
    monkeypatch.setattr(recon, "ROOT_PAPER_JOURNAL", root)
    assert recon.resolve_paper_journal_path() == root
