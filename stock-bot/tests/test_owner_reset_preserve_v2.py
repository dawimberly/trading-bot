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


def test_clean_restart_paper_only_targets_sot(monkeypatch):
    """--paper-only / autostart / recover must restart alpaca_paper_v2, not Lab."""
    calls: list[tuple] = []

    monkeypatch.setattr(
        "modules.portal_paths.bind_project_root", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "modules.portal_paths.has_alpaca_config",
        lambda username, book_id: book_id == "alpaca_paper_v2",
    )
    monkeypatch.setattr(owner_reset, "_clear_paper_pid_only", lambda u, b=None: calls.append(("clear", b)))
    monkeypatch.setattr(
        "modules.portal_bot.bot_running",
        lambda username, book_id="alpaca_paper": False,
    )
    monkeypatch.setattr(
        "modules.portal_bot.bot_pid",
        lambda username, book_id="alpaca_paper": 55 if book_id == "alpaca_paper" else None,
    )
    monkeypatch.setattr(owner_reset, "_live_preserve_pids", lambda *_a, **_k: {99})

    def fake_orphan(*, preserve_pids=None, username=None):
        calls.append(("orphan", frozenset(preserve_pids or set()), username))
        return 0, "No orphan bot processes."

    monkeypatch.setattr("modules.portal_bot.stop_orphan_project_bots", fake_orphan)

    def fake_start(username, book_id="alpaca_paper", *, skip_orphan_stop=False):
        calls.append(("start", book_id, skip_orphan_stop))
        return True, f"started {book_id}"

    monkeypatch.setattr("modules.portal_bot.start_bot", fake_start)

    ok, msg = owner_reset.clean_restart_paper_only("dawimberly")
    assert ok is True
    assert ("clear", "alpaca_paper_v2") in calls
    assert ("start", "alpaca_paper_v2", True) in calls
    orphan = next(c for c in calls if c[0] == "orphan")
    assert 99 in orphan[1]  # live
    assert 55 in orphan[1]  # Lab preserved
    assert "alpaca_paper_v2" in msg or "started" in msg.lower()


def test_wait_for_paper_heartbeat_uses_sot_path(tmp_path, monkeypatch):
    hb = tmp_path / "bot_heartbeat.json"
    hb.write_text(
        json.dumps({"timestamp": datetime.now().isoformat()}), encoding="utf-8"
    )
    seen: list[str] = []

    def fake_path(username, book_id):
        seen.append(book_id)
        return hb

    monkeypatch.setattr("modules.portal_bot.book_heartbeat_path", fake_path)
    ok, detail = owner_reset.wait_for_paper_heartbeat("dawimberly", timeout_sec=10)
    assert ok is True
    assert seen == ["alpaca_paper_v2"]
    assert "fresh" in detail
