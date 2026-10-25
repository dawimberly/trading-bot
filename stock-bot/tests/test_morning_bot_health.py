"""Morning health: Lab is always checked, and a startup crash is not retried."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def _load(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MORNING_BOT_HEALTH_HOLD", str(tmp_path / "hold.json"))
    monkeypatch.setenv("MORNING_BOT_HEALTH_LOG", str(tmp_path / "health.log"))
    path = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "morning_bot_health.py"
    spec = importlib.util.spec_from_file_location("morning_bot_health", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_lab_startup_crash_sets_hold_and_skips_next_morning(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    starts: list[str] = []
    notes: list[str] = []

    def book_ok(username, book_id, now):
        if book_id == mod.LAB:
            return False, "no live process"
        return True, "PID 1 heartbeat 10s"

    monkeypatch.setattr(mod, "_book_ok", book_ok)
    monkeypatch.setattr(mod, "_fix_book", lambda username, book_id: starts.append(book_id) or (True, "started"))
    monkeypatch.setattr(mod, "_process_stayed_up", lambda username, book_id, **kw: False)
    monkeypatch.setattr(mod, "_telegram", lambda text, log: notes.append(text))

    assert mod.main(["--force-weekday"]) == 1
    hold = json.loads((tmp_path / "hold.json").read_text(encoding="utf-8"))
    assert hold[mod.LAB]["reason"] == "started then exited"
    assert starts == [mod.LAB]
    assert any("left down" in note.lower() for note in notes)

    starts.clear()
    notes.clear()
    assert mod.main(["--force-weekday"]) == 0
    assert starts == []
    assert notes == []
    log = (tmp_path / "health.log").read_text(encoding="utf-8")
    assert "Not restarting" in log


def test_healthy_lab_clears_hold(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    mod.set_lab_hold("started then exited", datetime(2026, 10, 13, 10, 0, tzinfo=_ET))

    monkeypatch.setattr(mod, "_book_ok", lambda username, book_id, now: (True, "PID 9 heartbeat 4s"))
    monkeypatch.setattr(mod, "_telegram", lambda text, log: None)

    assert mod.main(["--force-weekday"]) == 0
    assert not (tmp_path / "hold.json").exists()


def test_dry_run_does_not_start_or_hold(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    starts: list[str] = []

    def book_ok(username, book_id, now):
        if book_id == mod.LAB:
            return False, "no live process"
        return True, "PID 1 heartbeat 10s"

    monkeypatch.setattr(mod, "_book_ok", book_ok)
    monkeypatch.setattr(mod, "_fix_book", lambda username, book_id: starts.append(book_id) or (True, "started"))

    assert mod.main(["--dry-run", "--force-weekday"]) == 0
    assert starts == []
    assert not (tmp_path / "hold.json").exists()
    assert "NEED FIX" in (tmp_path / "health.log").read_text(encoding="utf-8")


def test_phase4_paper_is_not_restarted(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    starts: list[str] = []

    def book_ok(username, book_id, now):
        if book_id == "alpaca_paper_v2":
            return False, "heartbeat stale 600s"
        return True, "PID 1 heartbeat 10s"

    monkeypatch.setattr(mod, "_book_ok", book_ok)
    monkeypatch.setattr(mod, "_phase4_paper_running", lambda username: True)
    monkeypatch.setattr(mod, "_fix_book", lambda username, book_id: starts.append(book_id) or (True, "started"))
    monkeypatch.setattr(mod, "_telegram", lambda text, log: None)

    assert mod.main(["--force-weekday"]) == 0
    assert starts == []
    assert "not restarting" in (tmp_path / "health.log").read_text(encoding="utf-8")
