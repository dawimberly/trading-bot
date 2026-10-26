"""Restart must kill the Windows process tree, not just the supervisor PID."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules import portal_bot


def test_graceful_stop_uses_taskkill_tree(monkeypatch):
    calls: list[list[str]] = []
    alive = {4242: True, 4243: True}

    def fake_alive(pid: int) -> bool:
        return bool(alive.get(pid))

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        if argv[:2] == ["taskkill", "/PID"] and "/T" in argv:
            target = int(argv[2])
            alive[target] = False
            if target == 4242:
                alive[4243] = False
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(portal_bot, "_pid_alive", fake_alive)
    monkeypatch.setattr(portal_bot, "_descendant_pids", lambda pid: {4243} if pid == 4242 else set())
    monkeypatch.setattr(portal_bot.subprocess, "run", fake_run)
    monkeypatch.setattr(portal_bot.sys, "platform", "win32")

    ok, msg = portal_bot._graceful_stop_pid(4242, wait_sec=0.01)
    assert ok
    assert calls
    assert any("/T" in c for c in calls)
    assert "tree" in msg.lower()
    assert not alive[4242]
    assert not alive[4243]


def test_restart_aborts_if_old_pid_still_tracked(monkeypatch):
    monkeypatch.setattr(portal_bot, "bot_pid", lambda *_a, **_k: 99)
    monkeypatch.setattr(portal_bot, "bot_running", lambda *_a, **_k: True)
    monkeypatch.setattr(portal_bot, "_is_paper_book", lambda *_a, **_k: True)
    monkeypatch.setattr(
        portal_bot, "stop_bot", lambda *_a, **_k: (True, "stopped")
    )
    monkeypatch.setattr(portal_bot.time, "sleep", lambda *_a, **_k: None)
    ok, msg = portal_bot.restart_bot("owner", "alpaca_paper")
    assert ok is False
    assert "still running" in msg.lower()


def test_restart_all_bots_stops_then_starts_each_keyed_book(monkeypatch):
    """Dashboard Restart All 3 must use full stop_bot, not bare PID unlink."""
    stops: list[str] = []
    starts: list[tuple[str, bool]] = []
    books = ("alpaca_live", "alpaca_paper", "alpaca_paper_v2")

    monkeypatch.setattr(portal_bot, "book_enabled", lambda *_a, **_k: True)
    monkeypatch.setattr(portal_bot, "_book_has_keys", lambda *_a, **_k: True)
    monkeypatch.setattr(
        portal_bot,
        "bot_pid",
        lambda username, book_id="alpaca_paper": 100 + books.index(book_id),
    )
    monkeypatch.setattr(portal_bot, "bot_running", lambda *_a, **_k: True)
    monkeypatch.setattr(portal_bot, "_is_paper_book", lambda u, b: b != "alpaca_live")
    monkeypatch.setattr(portal_bot.time, "sleep", lambda *_a, **_k: None)

    def fake_stop(username, book_id="alpaca_paper"):
        stops.append(book_id)
        return True, f"stopped {book_id}"

    def fake_start(username, book_id="alpaca_paper", *, skip_orphan_stop=False):
        starts.append((book_id, skip_orphan_stop))
        return True, f"started {book_id}"

    monkeypatch.setattr(portal_bot, "stop_bot", fake_stop)
    monkeypatch.setattr(portal_bot, "start_bot", fake_start)

    ok, msg = portal_bot.restart_all_bots("dawimberly")
    assert ok is True
    assert stops == list(books)
    assert [b for b, _ in starts] == list(books)
    assert all(skip for _, skip in starts)
    assert "All 3 books restarted" in msg


def test_hard_update_all_bots_force_kills_then_starts(monkeypatch):
    """Update All 3 force-kills each book tree, then starts from source."""
    kills: list[int] = []
    starts: list[tuple[str, bool]] = []
    books = ("alpaca_live", "alpaca_paper", "alpaca_paper_v2")

    phase = {"started": False}

    def fake_pid(username, book_id="alpaca_paper"):
        base = 100 + books.index(book_id)
        return base + (1000 if phase["started"] else 0)

    monkeypatch.setattr(portal_bot, "book_enabled", lambda *_a, **_k: True)
    monkeypatch.setattr(portal_bot, "_book_has_keys", lambda *_a, **_k: True)
    monkeypatch.setattr(portal_bot, "bot_pid", fake_pid)
    monkeypatch.setattr(portal_bot, "bot_running", lambda *_a, **_k: False)
    monkeypatch.setattr(portal_bot, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(portal_bot, "_is_paper_book", lambda u, b: b != "alpaca_live")
    monkeypatch.setattr(portal_bot, "book_pid_path", lambda *_a, **_k: SimpleNamespace(unlink=lambda **_kw: None))
    monkeypatch.setattr(portal_bot.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(
        portal_bot, "stop_orphan_project_bots", lambda **_k: (0, "No orphan bot processes.")
    )

    def fake_force(pid: int):
        kills.append(pid)
        return True, f"Force-stopped PID {pid} (process tree)."

    def fake_start(username, book_id="alpaca_paper", *, skip_orphan_stop=False):
        phase["started"] = True
        starts.append((book_id, skip_orphan_stop))
        return True, f"started {book_id}"

    monkeypatch.setattr(portal_bot, "_force_stop_pid", fake_force)
    monkeypatch.setattr(portal_bot, "start_bot", fake_start)

    ok, msg = portal_bot.hard_update_all_bots("dawimberly")
    assert ok is True
    assert kills == [100, 101, 102]
    assert [b for b, _ in starts] == list(books)
    assert all(skip for _, skip in starts)
    assert "All 3 books hard-updated" in msg
    assert "stop_bot" not in msg


def test_start_bot_passes_username_into_orphan_cleanup(monkeypatch):
    seen = {}

    def fake_stop(preserve_pids=None, *, username=None):
        seen["preserve"] = set(preserve_pids or [])
        seen["username"] = username
        return 0, "none"

    monkeypatch.setattr(portal_bot, "bot_running", lambda *_a, **_k: False)
    monkeypatch.setattr(portal_bot, "_tracked_book_pids", lambda _username: {10, 20, 30})
    monkeypatch.setattr(portal_bot, "stop_orphan_project_bots", fake_stop)
    monkeypatch.setattr(portal_bot, "_is_paper_book", lambda *_a, **_k: False)
    monkeypatch.setattr(
        portal_bot,
        "_bot_entry_script",
        lambda *_a, **_k: Path(r"C:\__no_such_bot__\run_all.py"),
    )

    ok, _msg = portal_bot.start_bot("dawimberly", "alpaca_live")
    assert ok is False
    assert seen["username"] == "dawimberly"
    assert seen["preserve"] == {10, 20, 30}


def test_orphan_cleanup_protects_descendants_and_stops_untracked(monkeypatch):
    parents = {
        10: 1,
        11: 10,
        20: 1,
        21: 20,
        22: 21,
        23: 22,
        30: 1,
        31: 30,
        32: 31,
        33: 32,
        99: 1,
    }
    commands = {
        10: "run_all.py",
        11: "run_all.py",
        20: "run_paper_bot.py",
        21: "run_paper_bot.py",
        22: "run_all.py",
        23: "run_all.py",
        30: "run_paper_bot.py",
        31: "run_paper_bot.py",
        32: "run_all.py",
        33: "run_all.py",
        99: "run_all.py",
    }
    pidfiles = {"alpaca_live": 10, "alpaca_paper_v2": 20, "alpaca_paper": 30}
    protected = {10, 11, 20, 21, 22, 23, 30, 31, 32, 33}

    def fake_find(script):
        return [pid for pid, cmd in commands.items() if script in cmd]

    def fake_bot_pid(username, book_id="alpaca_paper"):
        if username != "dawimberly":
            return None
        return pidfiles.get(book_id)

    stopped = []

    def fake_stop(pid, **_kwargs):
        stopped.append(pid)
        return True, "stopped"

    monkeypatch.setattr(portal_bot, "_find_script_pids", fake_find)
    monkeypatch.setattr(portal_bot, "bot_pid", fake_bot_pid)
    monkeypatch.setattr(portal_bot, "_parent_pid", lambda pid: parents.get(pid))
    monkeypatch.setattr(portal_bot, "_child_pids", lambda pid: [c for c, p in parents.items() if p == pid])
    monkeypatch.setattr(portal_bot, "_pid_alive", lambda pid: pid in commands)
    monkeypatch.setattr(portal_bot, "_process_cmdline", lambda pid: commands.get(pid))
    monkeypatch.setattr(portal_bot, "_graceful_stop_pid", fake_stop)
    monkeypatch.setattr(portal_bot, "find_bot_exe_pids", lambda: [])

    portal_bot.stop_orphan_project_bots(preserve_pids={10, 20, 30}, username="dawimberly")
    assert stopped == [99]
    assert protected.isdisjoint(stopped)

    stopped.clear()
    portal_bot.stop_orphan_project_bots(preserve_pids={10, 20, 30})
    assert 99 in stopped
    assert 11 in stopped
    assert 21 in stopped
    assert 23 in stopped
    assert 10 not in stopped
    assert 20 not in stopped
    assert 30 not in stopped

    stopped.clear()
    portal_bot.stop_orphan_project_bots(username="someoneelse")
    assert protected.issubset(set(stopped))
    assert 99 in stopped
