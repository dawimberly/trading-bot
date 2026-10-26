"""Phone Telegram commands: live-safe /status, shared poll lock, freeze paper-only."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from modules import telegram_commands as tg


def _write_hb(path: Path, *, paper: bool, equity: float, cash: float, regime: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-09-11T12:00:00",
                "equity": equity,
                "cash": cash,
                "regime": regime,
                "paper": paper,
                "halted": False,
                "sleeve_exposure": {
                    "vti_core_value": 200.0 if not paper else 50.0,
                    "spy_value": 10.0,
                },
            }
        ),
        encoding="utf-8",
    )


def test_live_commands_enabled_by_default(monkeypatch):
    monkeypatch.setattr(config, "get_telegram_config", lambda: ("token", "123"))
    monkeypatch.setattr(config, "TELEGRAM_COMMANDS_ENABLED", True)
    monkeypatch.setattr(config, "TELEGRAM_COMMANDS_LIVE", True)
    monkeypatch.setattr(config, "PAPER_TRADING", False)
    assert tg.effective_telegram_commands_enabled() is True
    assert tg.freeze_commands_allowed() is False


def test_live_commands_can_be_disabled(monkeypatch):
    monkeypatch.setattr(config, "get_telegram_config", lambda: ("token", "123"))
    monkeypatch.setattr(config, "TELEGRAM_COMMANDS_ENABLED", True)
    monkeypatch.setattr(config, "TELEGRAM_COMMANDS_LIVE", False)
    monkeypatch.setattr(config, "PAPER_TRADING", False)
    assert tg.effective_telegram_commands_enabled() is False


def test_status_includes_live_and_paper(monkeypatch, tmp_path):
    live_path = tmp_path / "live_bot_heartbeat.json"
    paper_path = tmp_path / "paper_chase_heartbeat.json"
    _write_hb(live_path, paper=False, equity=312.5, cash=40.0, regime="RHYME_B")
    _write_hb(paper_path, paper=True, equity=98000.0, cash=1200.0, regime="RHYME_C")
    monkeypatch.setattr(config, "PAPER_TRADING", False)
    monkeypatch.setattr(config, "paper_chase_mode_enabled", lambda: False)
    monkeypatch.setattr(config, "paper_aggressive_context", lambda: False)
    monkeypatch.setattr(
        "modules.health_check.resolve_live_heartbeat_path", lambda root=None: live_path
    )
    monkeypatch.setattr(
        "modules.health_check.resolve_paper_heartbeat_path", lambda root=None: paper_path
    )
    monkeypatch.setattr(config, "effective_insider_monitor_enabled", lambda: False)
    text = tg.format_status_command(equity=312.5, cash=40.0, regime="RHYME_B")
    assert "LIVE" in text and "PAPER" in text
    assert "$312.50" in text
    assert "$98,000.00" in text
    assert "RHYME_B" in text
    assert "RHYME_C" in text
    assert "This process: Live" in text


def test_help_and_start(monkeypatch):
    monkeypatch.setattr(config, "PAPER_TRADING", False)
    monkeypatch.setattr(tg, "freeze_commands_allowed", lambda: False)
    help_text = tg.handle_telegram_command("/help")
    start_text = tg.handle_telegram_command("/start")
    assert help_text and "/status" in help_text
    assert "never start/stop" in help_text
    assert start_text == help_text
    assert "Freeze" not in help_text


def test_freeze_blocked_on_live(monkeypatch):
    monkeypatch.setattr(config, "PAPER_TRADING", False)
    monkeypatch.setattr(tg, "freeze_commands_allowed", lambda: False)
    reply = tg.handle_telegram_command("CONFIRM attribution_stale")
    assert reply == "Freeze commands are paper-only."


def test_freeze_hold_on_paper(monkeypatch):
    monkeypatch.setattr(config, "PAPER_TRADING", True)
    monkeypatch.setattr(tg, "freeze_commands_allowed", lambda: True)
    reply = tg.handle_telegram_command("HOLD attribution_stale")
    assert reply and reply.startswith("HOLD attribution_stale")


def test_positions_from_heartbeats(monkeypatch, tmp_path):
    live_path = tmp_path / "live.json"
    paper_path = tmp_path / "paper.json"
    _write_hb(live_path, paper=False, equity=300.0, cash=10.0, regime="RHYME_A")
    _write_hb(paper_path, paper=True, equity=1000.0, cash=10.0, regime="RHYME_A")
    monkeypatch.setattr(
        "modules.health_check.resolve_live_heartbeat_path", lambda root=None: live_path
    )
    monkeypatch.setattr(
        "modules.health_check.resolve_paper_heartbeat_path", lambda root=None: paper_path
    )
    text = tg.handle_telegram_command("/positions")
    assert text and "LIVE" in text and "PAPER" in text
    assert "VTI" in text


def test_poll_skips_when_lock_held(monkeypatch, tmp_path):
    monkeypatch.setattr(tg, "effective_telegram_commands_enabled", lambda: True)
    monkeypatch.setattr(tg, "_poller_started", False)
    lock = tmp_path / "telegram_poll.lock"
    lock.mkdir()
    monkeypatch.setattr(tg, "_lock_path", lambda: lock)
    called = {"api": False}

    def fake_api(*_a, **_k):
        called["api"] = True
        return {"ok": True, "result": []}

    monkeypatch.setattr(tg, "_api", fake_api)
    assert tg.maybe_poll_telegram_commands() == 0
    assert called["api"] is False


def test_poll_handles_authorized_status(monkeypatch, tmp_path):
    monkeypatch.setattr(tg, "effective_telegram_commands_enabled", lambda: True)
    monkeypatch.setattr(tg, "_poller_started", False)
    monkeypatch.setattr(tg, "_lock_path", lambda: tmp_path / "lock")
    monkeypatch.setattr(tg, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(config, "get_telegram_config", lambda: ("token", "99"))
    monkeypatch.setattr(
        tg,
        "handle_telegram_command",
        lambda *a, **k: "status-ok",
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(tg, "_send_reply", lambda chat, text: sent.append((chat, text)) or True)
    monkeypatch.setattr(
        tg,
        "_api",
        lambda method, params=None, timeout=12: {
            "ok": True,
            "result": [
                {
                    "update_id": 41,
                    "message": {"chat": {"id": 99}, "text": "/status"},
                }
            ],
        },
    )
    assert tg.maybe_poll_telegram_commands() == 1
    assert sent == [("99", "status-ok")]
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["telegram_update_offset"] == 42
