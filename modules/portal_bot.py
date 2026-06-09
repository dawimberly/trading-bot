"""Start/stop run_all.py with per-user / per-book environment."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from modules.trading_books import BOOKS
from modules.portal_paths import (
    PROJECT_ROOT,
    book_bot_log_path,
    book_dir,
    book_env_path,
    book_heartbeat_path,
    book_journal_path,
    book_pid_path,
    migrate_user_to_books,
    read_user_env_prefs,
    user_bot_log_path,
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


def user_bot_env(username: str, book_id: str = "alpaca_paper") -> dict[str, str]:
    migrate_user_to_books(username)
    env = os.environ.copy()
    bd = book_dir(username, book_id)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONTRADING_ENV_FILE"] = str(book_env_path(username, book_id))
    env["HEARTBEAT_FILE"] = str(book_heartbeat_path(username, book_id))
    env["PAPER_JOURNAL_CSV"] = str(book_journal_path(username, book_id))
    env["WISDOM_SCORECARD_FILE"] = str(bd / "wisdom_scorecard.json")
    env["WISDOM_JOURNAL_FILE"] = str(bd / "wisdom_journal.csv")
    spec = BOOKS.get(book_id) or {}
    prefs = read_user_env_prefs(username, book_id)
    if spec.get("paper_chase") or (prefs.get("paper") and book_id == "alpaca_paper"):
        env["PAPER_CHASE_MODE"] = "1"
        env["PAPER_TRADING"] = "true"
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


def bot_pid(username: str, book_id: str = "alpaca_paper") -> int | None:
    path = book_pid_path(username, book_id)
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


def bot_running(username: str, book_id: str = "alpaca_paper") -> bool:
    return bot_pid(username, book_id) is not None


def read_bot_log_tail(username: str, book_id: str = "alpaca_paper", max_chars: int = 2000) -> str:
    path = book_bot_log_path(username, book_id)
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-max_chars:].strip()


def fund_slot_dir(slot: str) -> Path:
    """Isolated bot state for @root / legacy .env slots (not portal users)."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in slot.lower())
    path = PROJECT_ROOT / "data" / "fund" / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def start_bot_env(env_file: Path, slot: str, *, paper_chase: bool) -> tuple[bool, str]:
    """Start run_all.py with a specific .env file and isolated data/fund/<slot>/ logs."""
    env_file = Path(env_file).resolve()
    if not env_file.is_file():
        return False, f"Env file not found: {env_file}"
    text = env_file.read_text(encoding="utf-8")
    if "APCA_API_KEY_ID=" not in text or "APCA_API_SECRET_KEY=" not in text:
        return False, f"No Alpaca keys in {env_file}"

    slot_dir = fund_slot_dir(slot)
    pid_path = slot_dir / "bot.pid"
    if pid_path.is_file():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
            if _pid_alive(pid):
                return False, f"Bot slot '{slot}' already running (PID {pid})."
        except ValueError:
            pass
        pid_path.unlink(missing_ok=True)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONTRADING_ENV_FILE"] = str(env_file)
    env["HEARTBEAT_FILE"] = str(slot_dir / "bot_heartbeat.json")
    env["PAPER_JOURNAL_CSV"] = str(slot_dir / "paper_journal.csv")
    env["WISDOM_SCORECARD_FILE"] = str(slot_dir / "wisdom_scorecard.json")
    env["WISDOM_JOURNAL_FILE"] = str(slot_dir / "wisdom_journal.csv")
    if paper_chase:
        env["PAPER_CHASE_MODE"] = "1"
        env["PAPER_TRADING"] = "true"

    log_path = slot_dir / "bot.log"
    log_file = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    log_file.write(f"\n--- bot start {slot} {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    log_file.flush()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [_python(), "-u", str(PROJECT_ROOT / "run_all.py")],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )
    pid_path.write_text(str(proc.pid), encoding="utf-8")
    time.sleep(2)
    if proc.poll() is not None:
        log_file.flush()
        log_file.close()
        pid_path.unlink(missing_ok=True)
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        return False, f"Bot slot '{slot}' exited immediately (code {proc.returncode}).\n\n{tail}"
    mode = "paper chase" if paper_chase else "live"
    return True, f"[{slot}] {mode} started (PID {proc.pid}). Data: {slot_dir}"


def stop_bot_env(slot: str) -> tuple[bool, str]:
    pid_path = fund_slot_dir(slot) / "bot.pid"
    if not pid_path.is_file():
        return False, f"No bot running for slot '{slot}'."
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except ValueError:
        return False, f"Invalid PID file for slot '{slot}'."
    if not _pid_alive(pid):
        pid_path.unlink(missing_ok=True)
        return False, f"No bot running for slot '{slot}'."
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
    pid_path.unlink(missing_ok=True)
    return True, f"Stopped slot '{slot}' (PID {pid})."


def bot_env_running(slot: str) -> bool:
    pid_path = fund_slot_dir(slot) / "bot.pid"
    if not pid_path.is_file():
        return False
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except ValueError:
        return False
    if _pid_alive(pid):
        return True
    pid_path.unlink(missing_ok=True)
    return False


def start_bot(username: str, book_id: str = "alpaca_paper") -> tuple[bool, str]:
    if bot_running(username, book_id):
        return False, f"Bot is already running for {book_id}."
    run_all = PROJECT_ROOT / "run_all.py"
    if not run_all.is_file():
        return False, "run_all.py not found in project root."
    log_path = book_bot_log_path(username, book_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    log_file.write(f"\n--- bot start {book_id} {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    log_file.flush()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [_python(), "-u", str(run_all)],
        cwd=str(PROJECT_ROOT),
        env=user_bot_env(username, book_id),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )
    book_pid_path(username, book_id).write_text(str(proc.pid), encoding="utf-8")
    time.sleep(2)
    if proc.poll() is not None:
        log_file.flush()
        log_file.close()
        book_pid_path(username, book_id).unlink(missing_ok=True)
        tail = read_bot_log_tail(username, book_id)
        detail = f"\n\n{tail}" if tail else ""
        return False, f"Bot exited immediately (code {proc.returncode}).{detail}"
    return True, f"Bot started for {book_id} (PID {proc.pid}). First heartbeat may take up to 60s."


def stop_bot(username: str, book_id: str = "alpaca_paper") -> tuple[bool, str]:
    pid = bot_pid(username, book_id)
    if pid is None:
        return False, f"No bot running for {book_id}."
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
    book_pid_path(username, book_id).unlink(missing_ok=True)
    return True, f"Bot stopped for {book_id} (PID {pid})."
