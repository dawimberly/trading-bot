"""Daily Start preserves a healthy alpaca_paper_v2 process."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules import portal_bot
from scripts import owner_reset


def test_book_is_healthy_fresh_heartbeat(tmp_path, monkeypatch):
    hb = tmp_path / "bot_heartbeat.json"
    ts = datetime.now().isoformat()
    hb.write_text(json.dumps({"timestamp": ts}), encoding="utf-8")
    monkeypatch.setattr(owner_reset, "bot_pid", lambda *_a, **_k: 4242, raising=False)
    monkeypatch.setattr(
        "modules.portal_bot.bot_pid", lambda *_a, **_k: 4242
    )
    monkeypatch.setattr(
        "modules.portal_bot.book_heartbeat_path", lambda *_a, **_k: hb
    )
    ok, detail = owner_reset.book_is_healthy("dawimberly", "alpaca_paper_v2")
    assert ok is True
    assert "4242" in detail


def test_book_is_healthy_dead_pid(monkeypatch):
    monkeypatch.setattr("modules.portal_bot.bot_pid", lambda *_a, **_k: None)
    ok, detail = owner_reset.book_is_healthy("dawimberly", "alpaca_paper_v2")
    assert ok is False
    assert "no live PID" in detail


def test_book_is_healthy_stale_heartbeat(tmp_path, monkeypatch):
    hb = tmp_path / "bot_heartbeat.json"
    old = (datetime.now() - timedelta(minutes=20)).isoformat()
    hb.write_text(json.dumps({"timestamp": old}), encoding="utf-8")
    monkeypatch.setattr("modules.portal_bot.bot_pid", lambda *_a, **_k: 7)
    monkeypatch.setattr(
        "modules.portal_bot.book_heartbeat_path", lambda *_a, **_k: hb
    )
    ok, detail = owner_reset.book_is_healthy("dawimberly", "alpaca_paper_v2")
    assert ok is False
    assert "stale" in detail


def test_orphan_sweep_skips_paper_v2_tree(monkeypatch):
    stopped: list[int] = []

    def fake_bot_pid(username, book_id="alpaca_paper"):
        if book_id == "alpaca_paper_v2":
            return 111
        return None

    monkeypatch.setattr(portal_bot, "bot_pid", fake_bot_pid)
    monkeypatch.setattr(portal_bot, "_managed_bot_pids", lambda *_a, **_k: {111, 112})
    monkeypatch.setattr(
        portal_bot, "_find_script_pids", lambda name: [111, 112, 999] if name == "run_paper_bot.py" else []
    )
    monkeypatch.setattr(portal_bot, "find_bot_exe_pids", lambda: [])
    monkeypatch.setattr(
        portal_bot,
        "_is_descendant_of",
        lambda root, pid: root == 111 and pid in (111, 112),
    )
    monkeypatch.setattr(
        portal_bot,
        "_graceful_stop_pid",
        lambda pid, **_k: (stopped.append(pid) or True, "ok"),
    )
    n, _msg = portal_bot.stop_orphan_project_bots(username="dawimberly")
    assert 111 not in stopped
    assert 112 not in stopped
    assert 999 in stopped
    assert n == 1


def test_force_reset_calls_old_kill_path(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(owner_reset, "_force_reset_all", lambda *a, **k: calls.append("force") or 0)
    monkeypatch.setattr(
        owner_reset, "_daily_start_leave_healthy", lambda *a, **k: calls.append("daily") or 0
    )
    monkeypatch.setattr(owner_reset, "_run_paper_only", lambda *a, **k: calls.append("paper") or 0)
    monkeypatch.setattr(sys, "argv", ["owner_reset.py", "--force-reset", "--no-dashboard"])
    rc = owner_reset.main()
    assert rc == 0
    assert calls == ["force"]


def test_default_daily_start_uses_preserve_path(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(owner_reset, "_force_reset_all", lambda *a, **k: calls.append("force") or 0)
    monkeypatch.setattr(
        owner_reset, "_daily_start_leave_healthy", lambda *a, **k: calls.append("daily") or 0
    )
    monkeypatch.setattr(sys, "argv", ["owner_reset.py", "--no-dashboard"])
    rc = owner_reset.main()
    assert rc == 0
    assert calls == ["daily"]
