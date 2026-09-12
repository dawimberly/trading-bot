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
