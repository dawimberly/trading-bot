"""Portal books must not inherit stock-bot/.env PAPER_APCA_* (Lab blotter)."""

from __future__ import annotations

from pathlib import Path

import config


def _write_book(path: Path, **keys: str) -> Path:
    path.write_text(
        "".join(f"{k}={v}\n" for k, v in keys.items()),
        encoding="utf-8",
    )
    return path


def test_v2_drops_inherited_lab_paper_apca(tmp_path):
    book = _write_book(
        tmp_path / "v2.env",
        APCA_API_KEY_ID="v2_key",
        APCA_API_SECRET_KEY="v2_secret",
    )
    env = {
        "TRADING_BOOK_ID": "alpaca_paper_v2",
        "PORTAL_MANAGED_BOT": "1",
        "PYTHONTRADING_ENV_FILE": str(book),
        "APCA_API_KEY_ID": "parent_lab_or_live",
        "APCA_API_SECRET_KEY": "parent_secret",
        "PAPER_APCA_API_KEY_ID": "lab_paper_key",
        "PAPER_APCA_API_SECRET_KEY": "lab_paper_secret",
        "PAPER_CHASE_USE_RESEARCH_KEYS": "yes",
    }
    out = config.isolate_book_alpaca_env(env)
    assert out["APCA_API_KEY_ID"] == "v2_key"
    assert out["APCA_API_SECRET_KEY"] == "v2_secret"
    assert "PAPER_APCA_API_KEY_ID" not in out
    assert "PAPER_APCA_API_SECRET_KEY" not in out
    assert out["PAPER_CHASE_USE_RESEARCH_KEYS"] == "false"


def test_lab_uses_book_apca_not_root_paper_apca(tmp_path):
    book = _write_book(
        tmp_path / "lab.env",
        APCA_API_KEY_ID="lab_book_key",
        APCA_API_SECRET_KEY="lab_book_secret",
    )
    env = {
        "TRADING_BOOK_ID": "alpaca_paper",
        "PORTAL_MANAGED_BOT": "1",
        "PYTHONTRADING_ENV_FILE": str(book),
        "PAPER_APCA_API_KEY_ID": "root_paper_key",
        "PAPER_APCA_API_SECRET_KEY": "root_paper_secret",
        "PAPER_CHASE_USE_RESEARCH_KEYS": "yes",
    }
    out = config.isolate_book_alpaca_env(env)
    assert out["APCA_API_KEY_ID"] == "lab_book_key"
    assert "PAPER_APCA_API_KEY_ID" not in out
    assert out["PAPER_CHASE_USE_RESEARCH_KEYS"] == "false"


def test_live_keeps_book_keys_and_drops_paper_apca(tmp_path):
    book = _write_book(
        tmp_path / "live.env",
        APCA_API_KEY_ID="live_key",
        APCA_API_SECRET_KEY="live_secret",
    )
    env = {
        "TRADING_BOOK_ID": "alpaca_live",
        "PORTAL_MANAGED_BOT": "1",
        "PYTHONTRADING_ENV_FILE": str(book),
        "PAPER_APCA_API_KEY_ID": "lab_paper_key",
        "PAPER_APCA_API_SECRET_KEY": "lab_paper_secret",
        "PAPER_CHASE_USE_RESEARCH_KEYS": "yes",
    }
    out = config.isolate_book_alpaca_env(env)
    assert out["APCA_API_KEY_ID"] == "live_key"
    assert "PAPER_APCA_API_KEY_ID" not in out
    assert out["PAPER_CHASE_USE_RESEARCH_KEYS"] == "false"


def test_hand_launch_without_book_id_unchanged():
    env = {
        "PAPER_APCA_API_KEY_ID": "research_key",
        "PAPER_APCA_API_SECRET_KEY": "research_secret",
        "PAPER_CHASE_USE_RESEARCH_KEYS": "yes",
    }
    out = config.isolate_book_alpaca_env(env)
    assert out["PAPER_APCA_API_KEY_ID"] == "research_key"
    assert out["PAPER_CHASE_USE_RESEARCH_KEYS"] == "yes"
