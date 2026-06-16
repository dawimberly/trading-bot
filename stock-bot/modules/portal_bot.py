"""Start/stop run_all.py / run_paper_bot.py with per-user / per-book environment."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from modules.trading_books import BOOKS
from modules.portal_paths import (
    PROJECT_ROOT,
    book_bot_log_path,
    book_dir,
    book_heartbeat_path,
    book_journal_path,
    book_pid_path,
    ensure_book_env,
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
    """Interpreter for run_all.py (venv or PATH python when dashboard is frozen)."""
    for venv in (
        PROJECT_ROOT / ".venv" / "Scripts" / "python.exe",
        PROJECT_ROOT.parent / ".venv" / "Scripts" / "python.exe",
    ):
        if venv.is_file():
            return str(venv)
    if getattr(sys, "frozen", False):
        import shutil

        for name in ("python", "python3", "python.exe"):
            found = shutil.which(name)
            if found:
                return found
    return sys.executable


def user_bot_env(username: str, book_id: str = "alpaca_paper") -> dict[str, str]:
    migrate_user_to_books(username)
    env = os.environ.copy()
    bd = book_dir(username, book_id)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONTRADING_ROOT"] = str(PROJECT_ROOT)
    env["PYTHONTRADING_ENV_FILE"] = str(ensure_book_env(username, book_id))
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


def _find_script_pids(script_name: str) -> list[int]:
    """PIDs for python processes running script_name."""
    pids: list[int] = []
    try:
        if sys.platform == "win32":
            cmd = (
                f"Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                f"Where-Object {{ $_.CommandLine -like '*{script_name}*' }} | "
                "Select-Object -ExpandProperty ProcessId"
            )
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", cmd],
                text=True,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            pids = [int(x.strip()) for x in out.splitlines() if x.strip().isdigit()]
        else:
            out = subprocess.check_output(["pgrep", "-f", script_name], text=True)
            pids = [int(x) for x in out.split() if x.strip().isdigit()]
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, ValueError):
        pass
    return pids


def _process_cmdline(pid: int) -> str | None:
    try:
        if sys.platform == "win32":
            cmd = (
                f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine"
            )
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", cmd],
                text=True,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return out.strip() or None
        with open(f"/proc/{pid}/cmdline", encoding="utf-8", errors="replace") as f:
            return f.read().replace("\0", " ").strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, ValueError):
        return None


def _graceful_stop_pid(pid: int, *, wait_sec: float = 6.0) -> tuple[bool, str]:
    """Try graceful shutdown, then force-kill if needed."""
    if not _pid_alive(pid):
        return True, f"PID {pid} already stopped."
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid)],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            os.kill(pid, signal.SIGTERM)
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            if not _pid_alive(pid):
                return True, f"Stopped PID {pid}."
            time.sleep(0.25)
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                check=True,
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            os.kill(pid, signal.SIGKILL)
        return True, f"Force-stopped PID {pid}."
    except subprocess.CalledProcessError as exc:
        return False, f"Could not stop PID {pid}: {exc}"
    except OSError as exc:
        return False, f"Could not stop PID {pid}: {exc}"


def stop_orphan_project_bots() -> tuple[int, str]:
    """Stop stray run_paper_bot / run_all processes for this repo (no pid file)."""
    stopped = 0
    notes: list[str] = []
    for script in ("run_paper_bot.py", "run_all.py"):
        for pid in _find_script_pids(script):
            ok, msg = _graceful_stop_pid(pid)
            if ok:
                stopped += 1
            else:
                notes.append(msg)
    summary = f"Stopped {stopped} orphan bot process(es)." if stopped else "No orphan bot processes."
    if notes:
        summary += " " + "; ".join(notes)
    return stopped, summary


def _is_paper_book(username: str, book_id: str) -> bool:
    spec = BOOKS.get(book_id) or {}
    prefs = read_user_env_prefs(username, book_id)
    return bool(spec.get("paper_chase") or prefs.get("paper"))


def _bot_entry_script(username: str, book_id: str) -> Path:
    """Paper chase books use run_paper_bot supervisor; live uses run_all."""
    pid = bot_pid(username, book_id)
    if pid:
        cmd = _process_cmdline(pid) or ""
        if "run_paper_bot" in cmd:
            return PROJECT_ROOT / "run_paper_bot.py"
    if _find_script_pids("run_paper_bot.py"):
        return PROJECT_ROOT / "run_paper_bot.py"
    if _is_paper_book(username, book_id):
        return PROJECT_ROOT / "run_paper_bot.py"
    return PROJECT_ROOT / "run_all.py"


def bot_status_label(username: str, book_id: str = "alpaca_paper") -> str:
    """Short status for dashboard header: Running (PID n) · script · mode."""
    pid = bot_pid(username, book_id)
    if pid is None:
        orphans = _find_script_pids("run_all.py") + _find_script_pids("run_paper_bot.py")
        if orphans:
            pid = orphans[0]
        else:
            return "Bot: Stopped"
    cmd = _process_cmdline(pid) or ""
    script = "run_paper_bot" if "run_paper_bot" in cmd else "run_all"
    mode = "paper" if _is_paper_book(username, book_id) else "live"
    return f"Bot: Running · PID {pid} · {script} ({mode})"


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

    stop_orphan_project_bots()
    time.sleep(1.0)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONTRADING_ROOT"] = str(PROJECT_ROOT)
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
    orphans_stopped, orphan_msg = stop_orphan_project_bots()
    if orphans_stopped:
        time.sleep(1.0)
    bot_script = _bot_entry_script(username, book_id)
    if not bot_script.is_file():
        return False, f"{bot_script.name} not found in project root."
    log_path = book_bot_log_path(username, book_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    log_file.write(
        f"\n--- bot start {book_id} {bot_script.name} "
        f"{time.strftime('%Y-%m-%d %H:%M:%S')} ---\n"
    )
    log_file.flush()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [_python(), "-u", str(bot_script)],
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
        hint = ""
        if "401" in tail or "not authorized" in tail.lower():
            hint = (
                "\n\nAlpaca returned 401 — check API keys in the portal menu "
                "(paper vs live keys must match PAPER_TRADING in your book .env)."
            )
        orphan_note = f"\n{orphan_msg}" if orphans_stopped else ""
        return False, f"Bot exited immediately (code {proc.returncode}).{hint}{orphan_note}{detail}"
    mode = "paper" if _is_paper_book(username, book_id) else "live"
    orphan_note = f" ({orphan_msg})" if orphans_stopped else ""
    return True, (
        f"{mode} bot started via {bot_script.name} (PID {proc.pid}){orphan_note}. "
        "First heartbeat may take up to 60s."
    )


def stop_bot(username: str, book_id: str = "alpaca_paper") -> tuple[bool, str]:
    pid = bot_pid(username, book_id)
    if pid is None:
        return False, f"No bot running for {book_id}."
    ok, msg = _graceful_stop_pid(pid)
    book_pid_path(username, book_id).unlink(missing_ok=True)
    if not ok:
        return False, msg
    # run_paper_bot.py supervises a child run_all.py — clean up stragglers
    stop_orphan_project_bots()
    return True, f"Bot stopped for {book_id} ({msg})"


def restart_bot(username: str, book_id: str = "alpaca_paper") -> tuple[bool, str]:
    """Gracefully stop then start the book bot (or start if not running)."""
    mode = "paper" if _is_paper_book(username, book_id) else "live"
    was_running = bot_running(username, book_id)
    if was_running:
        ok, stop_msg = stop_bot(username, book_id)
        if not ok:
            return False, stop_msg
    else:
        stop_msg = "Bot was not running."
        stop_orphan_project_bots()
    time.sleep(1.5)
    ok, start_msg = start_bot(username, book_id)
    if not ok:
        return False, start_msg
    prefix = "Bot restarted successfully" if was_running else "Bot started successfully"
    return True, f"{prefix} ({mode} mode).\n{stop_msg}\n{start_msg}"
