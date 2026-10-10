"""Paper SoT paths: status heartbeat + Live Overview other-book."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import status
from dashboard_app import _other_book_id
from modules.trading_books import PAPER_SOT_BOOK_ID


def test_other_book_id_live_pairs_with_sot():
    assert _other_book_id("alpaca_live") == PAPER_SOT_BOOK_ID == "alpaca_paper_v2"
    assert _other_book_id("alpaca_paper_v2") == "alpaca_live"
    assert _other_book_id("alpaca_paper") == "alpaca_live"


def test_resolve_paper_heartbeat_uses_portal_sot(tmp_path, monkeypatch):
    monkeypatch.delenv("PAPER_CHASE_HEARTBEAT", raising=False)
    monkeypatch.setenv("PORTAL_USERNAME", "testdaw")
    monkeypatch.setattr(
        "modules.portal_paths.bind_project_root", lambda *_a, **_k: None
    )

    expected = tmp_path / "bot_heartbeat.json"

    def fake_hb(username, book_id):
        assert username == "testdaw"
        assert book_id == "alpaca_paper_v2"
        return expected

    monkeypatch.setattr("modules.portal_paths.book_heartbeat_path", fake_hb)
    assert status._resolve_paper_heartbeat_path() == expected


def test_resolve_paper_heartbeat_env_override(tmp_path, monkeypatch):
    override = tmp_path / "custom_hb.json"
    monkeypatch.setenv("PAPER_CHASE_HEARTBEAT", str(override))
    assert status._resolve_paper_heartbeat_path() == override
