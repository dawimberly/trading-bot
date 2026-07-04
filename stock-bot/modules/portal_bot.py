"""Start/stop run_all.py / run_paper_bot.py with per-user / per-book environment."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

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
from modules.runtime_paths import (
    BOT_EXE_NAME,
    find_bot_exe_pids,
    find_script_pids,
    resolve_bot_executable,
    resolve_bot_workdir,
)
from modules.trading_books import BOOKS, book_enabled

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


def _bot_subprocess_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Child bot env without inherited TLS paths from the monitor EXE."""
    env = dict(base or os.environ)
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        env.pop(var, None)
    return env


def user_bot_env(username: str, book_id: str = "alpaca_paper") -> dict[str, str]:
    migrate_user_to_books(username)
    env = _bot_subprocess_env()
    bd = book_dir(username, book_id)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONTRADING_ROOT"] = str(PROJECT_ROOT)
    env["PYTHONTRADING_ENV_FILE"] = str(ensure_book_env(username, book_id))
    env["PORTAL_MANAGED_BOT"] = "1"
    env["HEARTBEAT_FILE"] = str(book_heartbeat_path(username, book_id))
    env["PAPER_JOURNAL_CSV"] = str(book_journal_path(username, book_id))
    env["WISDOM_SCORECARD_FILE"] = str(bd / "wisdom_scorecard.json")
    env["WISDOM_JOURNAL_FILE"] = str(bd / "wisdom_journal.csv")
    spec = BOOKS.get(book_id) or {}
    prefs = read_user_env_prefs(username, book_id)
    paper = bool(prefs.get("paper", spec.get("default_paper", True)))
    allow_live = bool(prefs.get("allow_live", spec.get("allow_live_default", False)))
    env["PAPER_TRADING"] = "true" if paper else "false"
    env["ALLOW_LIVE_TRADING"] = "yes" if allow_live else "no"
    if spec.get("paper_chase") or (paper and book_id == "alpaca_paper"):
        env["PAPER_CHASE_MODE"] = "1"
        from config import apply_realistic_research_env

        env = apply_realistic_research_env(env)
    else:
        env.pop("PAPER_CHASE_MODE", None)
        if book_id == "alpaca_live":
            from config import clear_paper_research_env

            env = clear_paper_research_env(env)
    return env


def _child_pids(pid: int) -> list[int]:
    """Direct child process IDs (Windows WMI)."""
    if not _pid_alive(pid):
        return []
    try:
        if sys.platform == "win32":
            cmd = (
                f"Get-CimInstance Win32_Process -Filter \"ParentProcessId={pid}\" | "
                "Select-Object -ExpandProperty ProcessId"
            )
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", cmd],
                text=True,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return [int(x.strip()) for x in out.splitlines() if x.strip().isdigit()]
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, ValueError):
        return []
    return []


def _parent_pid(pid: int) -> int | None:
    try:
        if sys.platform == "win32":
            cmd = (
                f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").ParentProcessId"
            )
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", cmd],
                text=True,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).strip()
            return int(out) if out.isdigit() else None
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, ValueError):
        return None
    return None


def _is_descendant_of(pid: int, ancestor: int) -> bool:
    current = _parent_pid(pid)
    depth = 0
    while current is not None and depth < 32:
        if current == ancestor:
            return True
        current = _parent_pid(current)
        depth += 1
    return False


def _descendant_pids(root: int) -> set[int]:
    seen: set[int] = set()
    stack = [root]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(_child_pids(pid))
    seen.discard(root)
    return seen


def _portal_launched(cmd: str) -> bool:
    return " -u " in f" {cmd} "


def _paper_run_all_child(username: str) -> int | None:
    supervisor = bot_pid(username, "alpaca_paper")
    if supervisor is None:
        return None
    for pid in sorted(_descendant_pids(supervisor)):
        cmd = _process_cmdline(pid) or ""
        if "run_all.py" in cmd and not _portal_launched(cmd):
            return pid
    return None


def _paper_allowed_descendants(supervisor: int) -> set[int]:
    """Only the paper supervisor's single run_all.py worker (not nested dup supervisors)."""
    allowed: set[int] = set()
    for pid in sorted(_descendant_pids(supervisor)):
        cmd = _process_cmdline(pid) or ""
        if "run_paper_bot.py" in cmd:
            continue
        if "run_all.py" in cmd:
            allowed.add(pid)
            break
    return allowed


def _managed_bot_pids(username: str) -> set[int]:
    """PIDs that may run for this user (supervisors, live chain, one paper run_all)."""
    allowed: set[int] = set()
    live = bot_pid(username, "alpaca_live")
    if live is not None:
        allowed.add(live)
        walk = live
        for _ in range(32):
            parent = _parent_pid(walk)
            if parent is None:
                break
            cmd = _process_cmdline(parent) or ""
            if "run_all.py" not in cmd:
                break
            allowed.add(parent)
            walk = parent
    paper = bot_pid(username, "alpaca_paper")
    if paper is not None:
        allowed.add(paper)
        allowed |= _paper_allowed_descendants(paper)
    return allowed


def _find_script_pids(script_name: str) -> list[int]:
    """PIDs for python processes running script_name."""
    return find_script_pids(script_name)


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


def _tracked_book_pids(username: str) -> set[int]:
    """Live PIDs from per-book pid files (paper + live may run together)."""
    pids: set[int] = set()
    for bid in BOOKS:
        pid = bot_pid(username, bid)
        if pid is not None:
            pids.add(pid)
    return pids


def trim_portal_duplicate_bots(username: str) -> tuple[int, str]:
    """Remove extra portal-launched supervisors and stray paper run_all workers."""
    live = bot_pid(username, "alpaca_live")
    paper = bot_pid(username, "alpaca_paper")
    stopped = 0
    notes: list[str] = []
    for pid in _find_script_pids("run_paper_bot.py"):
        if paper is not None and pid == paper:
            continue
        cmd = _process_cmdline(pid) or ""
        if not _portal_launched(cmd):
            continue
        ok, msg = _graceful_stop_pid(pid)
        if ok:
            stopped += 1
        else:
            notes.append(msg)
    paper_supervisor = paper
    for pid in _find_script_pids("run_all.py"):
        if live is not None and pid == live:
            continue
        if paper_supervisor is not None and _is_descendant_of(paper_supervisor, pid):
            parent = _parent_pid(pid)
            pcmd = _process_cmdline(parent) or "" if parent else ""
            if parent and "run_all.py" in pcmd:
                ok, msg = _graceful_stop_pid(pid)
                if ok:
                    stopped += 1
                else:
                    notes.append(msg)
            continue
        cmd = _process_cmdline(pid) or ""
        if _portal_launched(cmd):
            ok, msg = _graceful_stop_pid(pid)
            if ok:
                stopped += 1
            else:
                notes.append(msg)
    summary = f"Trimmed {stopped} duplicate bot process(es)." if stopped else "No duplicate bot processes."
    if notes:
        summary += " " + "; ".join(notes)
    return stopped, summary


def stop_orphan_project_bots(
    preserve_pids: set[int] | None = None,
    *,
    username: str | None = None,
) -> tuple[int, str]:
    """Stop stray run_paper_bot / run_all processes for this repo (no pid file)."""
    preserve = set(preserve_pids or set())
    if username:
        preserve |= _managed_bot_pids(username)
    stopped = 0
    notes: list[str] = []
    for script in ("run_paper_bot.py", "run_all.py"):
        pids = _find_script_pids(script)
        if pids:
            print(f"Found {len(pids)} {script} process(es)...", flush=True)
        for pid in pids:
            if pid in preserve:
                continue
            live_supervisor = bot_pid(username, "alpaca_live") if username else None
            paper_supervisor = bot_pid(username, "alpaca_paper") if username else None
            if live_supervisor is not None and _is_descendant_of(live_supervisor, pid):
                continue
            if paper_supervisor is not None and _is_descendant_of(paper_supervisor, pid):
                continue
            print(f"Stopping orphan {script} PID {pid}...", flush=True)
            ok, msg = _graceful_stop_pid(pid)
            if ok:
                stopped += 1
            else:
                notes.append(msg)
    for pid in find_bot_exe_pids():
        if pid in preserve:
            continue
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
    if _is_paper_book(username, book_id):
        return PROJECT_ROOT / "run_paper_bot.py"
    return PROJECT_ROOT / "run_all.py"


def _bot_launch_command(bot_script: Path) -> tuple[list[str], Path]:
    """Argv + cwd for portal-managed bots — always Python source (not the frozen EXE).

    The Weinstein EXE is for standalone/manual use only; portal books need per-book
    .env and heartbeat paths that are reliable only via python run_all/run_paper_bot.
    """
    return [_python(), "-u", str(bot_script)], PROJECT_ROOT


def bot_status_label(username: str, book_id: str = "alpaca_paper") -> str:
    """Short status for dashboard header: Running (PID n) · script · mode."""
    pid = bot_pid(username, book_id)
    if pid is None:
        return "Bot: Stopped"
    cmd = _process_cmdline(pid) or ""
    if BOT_EXE_NAME.replace(".exe", "") in cmd or BOT_EXE_NAME in cmd:
        script = BOT_EXE_NAME
    elif "run_paper_bot" in cmd:
        script = "run_paper_bot"
    else:
        script = "run_all"
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


def _write_startup_heartbeat(username: str, book_id: str) -> None:
    """Fresh timestamp so status/dashboard do not show stale heartbeat after restart."""
    try:
        from modules.safe_io import write_json_atomic

        path = book_heartbeat_path(username, book_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        write_json_atomic(
            path,
            {
                "timestamp": now,
                "status": "starting",
                "bot_restarted_at": now,
                "book_id": book_id,
                "paper": _is_paper_book(username, book_id),
            },
        )
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning(
            "startup heartbeat write failed for %s: %s", book_id, exc
        )


def _book_has_keys(username: str, book_id: str) -> bool:
    try:
        from modules.portal_paths import has_alpaca_config

        return has_alpaca_config(username, book_id)
    except Exception:
        return False


def restart_all_bots(username: str) -> tuple[bool, str]:
    """Gracefully restart every configured book that has API keys."""
    messages: list[str] = []
    ok_all = True
    restarted_any = False

    # Stop every book first, then one orphan sweep — avoids killing live run_all
    # while starting paper (both use run_all.py on disk).
    for book_id in BOOKS:
        if not book_enabled(book_id) or not _book_has_keys(username, book_id):
            continue
        if bot_running(username, book_id):
            stop_bot(username, book_id)
    stopped, orphan_msg = stop_orphan_project_bots(username=username)
    if stopped:
        time.sleep(1.0)

    for book_id in BOOKS:
        if not book_enabled(book_id) or not _book_has_keys(username, book_id):
            continue
        restarted_any = True
        ok, msg = start_bot(username, book_id, skip_orphan_stop=True)
        prefix = "Bot restarted successfully" if ok else "Bot failed to start"
        mode = "paper" if _is_paper_book(username, book_id) else "live"
        messages.append(f"--- {book_id} ---\n{prefix} ({mode} mode).\n{msg}")
        ok_all = ok_all and ok
    if not restarted_any:
        return False, "No books with API keys found to restart."
    if stopped:
        messages.insert(0, f"(Pre-start cleanup: {orphan_msg})")
    prefix = "All bots restarted" if ok_all else "Restart finished with errors"
    return ok_all, f"{prefix}:\n\n" + "\n\n".join(messages)


def bot_running(username: str, book_id: str = "alpaca_paper") -> bool:
    return bot_pid(username, book_id) is not None


def read_bot_log_tail(username: str, book_id: str = "alpaca_paper", max_chars: int = 2000) -> str:
    path = book_bot_log_path(username, book_id)
    if not path.is_file():
        return ""
    try:
        size = path.stat().st_size
        read_bytes = min(size, max(max_chars * 4, 8192))
        with path.open("rb") as handle:
            if size > read_bytes:
                handle.seek(size - read_bytes)
            chunk = handle.read().decode("utf-8", errors="replace")
        return chunk[-max_chars:].strip()
    except OSError:
        return ""


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

    env = _bot_subprocess_env()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONTRADING_ROOT"] = str(PROJECT_ROOT)
    env["PYTHONTRADING_ENV_FILE"] = str(env_file)
    env["HEARTBEAT_FILE"] = str(slot_dir / "bot_heartbeat.json")
    env["PAPER_JOURNAL_CSV"] = str(slot_dir / "paper_journal.csv")
    env["WISDOM_SCORECARD_FILE"] = str(slot_dir / "wisdom_scorecard.json")
    env["WISDOM_JOURNAL_FILE"] = str(slot_dir / "wisdom_journal.csv")
    if paper_chase:
        from config import apply_realistic_research_env

        env = apply_realistic_research_env(env)
        env["PAPER_CHASE_MODE"] = "1"
        env["PAPER_TRADING"] = "true"

    log_path = slot_dir / "bot.log"
    log_file = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    log_file.write(f"\n--- bot start {slot} {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    log_file.flush()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    launch_argv, launch_cwd = _bot_launch_command(PROJECT_ROOT / "run_all.py")
    proc = subprocess.Popen(
        launch_argv,
        cwd=str(launch_cwd),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )
    pid_path.write_text(str(proc.pid), encoding="utf-8")
    try:
        from modules.safe_io import write_json_atomic

        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        write_json_atomic(
            slot_dir / "bot_heartbeat.json",
            {
                "timestamp": now,
                "status": "starting",
                "bot_restarted_at": now,
                "slot": slot,
            },
        )
    except Exception:
        pass
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


def start_bot(username: str, book_id: str = "alpaca_paper", *, skip_orphan_stop: bool = False) -> tuple[bool, str]:
    if bot_running(username, book_id):
        return False, f"Bot is already running for {book_id}."
    orphan_msg = ""
    if not skip_orphan_stop:
        preserve = _tracked_book_pids(username)
        orphans_stopped, orphan_msg = stop_orphan_project_bots(preserve_pids=preserve)
        if orphans_stopped:
            time.sleep(1.0)
    bot_script = _bot_entry_script(username, book_id)
    launch_argv, launch_cwd = _bot_launch_command(bot_script)
    if bot_script.name != "run_all.py" and not bot_script.is_file():
        return False, f"{bot_script.name} not found in project root."
    if bot_script.name == "run_all.py" and launch_argv[0].endswith(BOT_EXE_NAME):
        if not Path(launch_argv[0]).is_file():
            return False, f"{BOT_EXE_NAME} not found beside dist/ runtime data."
    elif not bot_script.is_file():
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
        launch_argv,
        cwd=str(launch_cwd),
        env=user_bot_env(username, book_id),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )
    book_pid_path(username, book_id).write_text(str(proc.pid), encoding="utf-8")
    _write_startup_heartbeat(username, book_id)
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
        orphan_note = f"\n{orphan_msg}" if orphan_msg else ""
        return False, f"Bot exited immediately (code {proc.returncode}).{hint}{orphan_note}{detail}"
    mode = "paper" if _is_paper_book(username, book_id) else "live"
    orphan_note = f" ({orphan_msg})" if orphan_msg else ""
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
    stop_orphan_project_bots(preserve_pids=_tracked_book_pids(username), username=username)
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
    ok, start_msg = start_bot(username, book_id)
    if not ok:
        return False, start_msg
    prefix = "Bot restarted successfully" if was_running else "Bot started successfully"
    return True, f"{prefix} ({mode} mode).\n{stop_msg}\n{start_msg}"
