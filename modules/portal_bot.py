"""Start/stop run_all.py with per-user environment."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from modules.portal_paths import (
    PROJECT_ROOT,
    user_env_path,
    user_heartbeat_path,
    user_journal_path,
    user_pid_path,
)

WISDOM_SCORECARD = PROJECT_ROOT / "wisdom_scorecard.json"
WISDOM_JOURNAL = PROJECT_ROOT / "wisdom_journal.csv"


def _python() -> str:
    venv = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    if venv.is_file():
        return str(venv)
    return sys.executable


def user_bot_env(username: str) -> dict[str, str]:
    env = os.environ.copy()
    ud = user_env_path(username).parent
    env["PYTHONTRADING_ENV_FILE"] = str(user_env_path(username))
    env["HEARTBEAT_FILE"] = str(user_heartbeat_path(username))
    env["PAPER_JOURNAL_CSV"] = str(user_journal_path(username))
    env["WISDOM_SCORECARD_FILE"] = str(ud / "wisdom_scorecard.json")
    env["WISDOM_JOURNAL_FILE"] = str(ud / "wisdom_journal.csv")
    return env


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}"],
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return str(pid).encode() in out
        except (subprocess.CalledProcessError, OSError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def bot_pid(username: str) -> int | None:
    path = user_pid_path(username)
    if not path.is_file():
        return None
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None
    if _pid_alive(pid):
        return pid
    path.unlink(missing_ok=True)
    return None


def bot_running(username: str) -> bool:
    return bot_pid(username) is not None


def start_bot(username: str) -> tuple[bool, str]:
    if bot_running(username):
        return False, "Bot is already running for this account."
    run_all = PROJECT_ROOT / "run_all.py"
    if not run_all.is_file():
        return False, "run_all.py not found in project root."
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [_python(), str(run_all)],
        cwd=str(PROJECT_ROOT),
        env=user_bot_env(username),
        creationflags=flags,
    )
    user_pid_path(username).write_text(str(proc.pid), encoding="utf-8")
    return True, f"Bot started (PID {proc.pid})."


def stop_bot(username: str) -> tuple[bool, str]:
    pid = bot_pid(username)
    if pid is None:
        return False, "No bot running for this account."
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                check=True,
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            subprocess.run(["kill", "-TERM", str(pid)], check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        return False, f"Could not stop PID {pid}: {exc}"
    user_pid_path(username).unlink(missing_ok=True)
    return True, f"Bot stopped (PID {pid})."
