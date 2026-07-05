#!/usr/bin/env python3
"""Register Windows Task Scheduler job for nightly autonomous paper-bot startup.

Creates task: PythonTrading_Autonomous_Paper
  - Daily at 11:00 PM (23:00 local time)
  - Runs Start_Autonomous.bat with highest privileges
  - Runs whether the user is logged in or not (when password is supplied)

Run from stock-bot/:
  python scripts/setup_autonomous_task.py
  python scripts/setup_autonomous_task.py --logged-in-only   # no password prompt

Remove:
  Remove_Autonomous_Task.bat
  schtasks /Delete /TN "PythonTrading_Autonomous_Paper" /F
"""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASK_NAME = "PythonTrading_Autonomous_Paper"
BAT_PATH = ROOT / "Start_Autonomous.bat"
DAILY_TIME = "23:00"
LOG_PATH = ROOT / "logs" / "autostart_paper.log"


def _log(msg: str) -> None:
    print(msg, flush=True)


def _run(cmd: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def _windows_user() -> str:
    domain = (os.environ.get("USERDOMAIN") or "").strip()
    user = (os.environ.get("USERNAME") or "").strip()
    if domain and user and domain.upper() != user.upper():
        return f"{domain}\\{user}"
    return user or os.environ.get("COMPUTERNAME", "SYSTEM")


def _task_command() -> str:
    # Start_Autonomous.bat also cd's to %~dp0; cmd /c keeps quoting reliable in schtasks.
    return f'cmd.exe /c "{BAT_PATH}"'


def _delete_existing_task() -> None:
    proc = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    if proc.returncode == 0:
        _log(f"Removed existing task: {TASK_NAME}")


def _create_task(
    *,
    password: str | None,
    logged_in_only: bool,
    run_level: str = "HIGHEST",
) -> tuple[bool, str]:
    cmd = [
        "schtasks",
        "/Create",
        "/TN",
        TASK_NAME,
        "/TR",
        _task_command(),
        "/SC",
        "DAILY",
        "/ST",
        DAILY_TIME,
        "/RL",
        run_level,
        "/F",
    ]
    if logged_in_only:
        # No /RU — interactive task (only when logged in).
        pass
    else:
        cmd.extend(["/RU", _windows_user()])
        if password:
            cmd.extend(["/RP", password])
        # Omit /IT so the task is not limited to interactive sessions only.

    proc = _run(cmd)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode == 0:
        return True, out or "Task created."
    return False, out or f"schtasks failed (exit {proc.returncode})"


def _query_task() -> tuple[bool, str]:
    proc = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST"])
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, out


def _print_instructions(*, logged_in_only: bool) -> None:
    _log("")
    _log("=== PythonTrading Autonomous Paper — Task Scheduler Setup ===")
    _log(f"  Project root: {ROOT}")
    _log(f"  Batch file:   {BAT_PATH}")
    _log(f"  Task name:    {TASK_NAME}")
    _log(f"  Schedule:     Daily at {DAILY_TIME} (11:00 PM local time)")
    _log(f"  Privileges:   Highest")
    _log("")
    if logged_in_only:
        _log("  Mode: Run ONLY when you are logged in (--logged-in-only).")
    else:
        _log("  Mode: Run whether you are logged in or not.")
        _log("  You will be prompted for your Windows password once.")
        _log("  (Stored securely by Task Scheduler — required for overnight runs.)")
    _log("")
    _log("  The task launches Start_Autonomous.bat, which:")
    _log("    • Restarts the paper bot (live bot preserved)")
    _log("    • Waits for heartbeat + verifies insider integration")
    _log("    • Writes reports/premarket/YYYY-MM-DD.txt")
    _log("    • Sends Telegram summary at 9:00 AM ET")
    _log("")


def _print_success(*, logged_in_only: bool, run_level: str = "HIGHEST") -> None:
    _log("")
    _log("=== SUCCESS ===")
    _log(f"  Scheduled task '{TASK_NAME}' is registered.")
    _log(f"  Run level:    {run_level}")
    _log(f"  Next run: tonight at 11:00 PM (or tomorrow if past 11 PM).")
    _log("")
    _log("  Verify:")
    _log(f'    schtasks /Query /TN "{TASK_NAME}"')
    _log("")
    _log("  Manual test (recommended now):")
    _log(f"    Double-click: {BAT_PATH}")
    _log("    Or:           cmd /c Start_Autonomous.bat")
    _log("")
    _log("  Logs:")
    _log(f"    {LOG_PATH}")
    _log("")
    _log("  Remove task anytime:")
    _log("    Double-click Remove_Autonomous_Task.bat")
    _log(f'    Or: schtasks /Delete /TN "{TASK_NAME}" /F')
    _log("")
    if logged_in_only:
        _log("  NOTE: With --logged-in-only, the task will NOT run while logged off.")
        _log("  Re-run without that flag and enter your password for true overnight runs.")
    else:
        _log("  NOTE: PC must be on (or asleep with wake timers enabled) at 11:00 PM.")
    if run_level != "HIGHEST":
        _log("")
        _log("  For HIGHEST privileges, re-run as Administrator:")
        _log("    python scripts/setup_autonomous_task.py")
    _log("")


def main() -> int:
    if sys.platform != "win32":
        _log("[FAIL] This script requires Windows Task Scheduler.")
        return 1

    parser = argparse.ArgumentParser(
        description="Create nightly Task Scheduler job for Start_Autonomous.bat"
    )
    parser.add_argument(
        "--logged-in-only",
        action="store_true",
        help="Create task without stored password (runs only when logged in)",
    )
    parser.add_argument(
        "--password",
        default="",
        help="Windows password (avoid — prefer interactive prompt)",
    )
    args = parser.parse_args()

    _print_instructions(logged_in_only=args.logged_in_only)

    if not BAT_PATH.is_file():
        _log(f"[FAIL] Missing {BAT_PATH}")
        return 1

    schtasks = _run(["where", "schtasks"])
    if schtasks.returncode != 0:
        _log("[FAIL] schtasks not found — is Task Scheduler available?")
        return 1

    _delete_existing_task()

    password: str | None = None
    if not args.logged_in_only:
        password = args.password.strip() or None
        if password is None:
            _log("Enter your Windows password (input hidden; required for overnight runs):")
            _log("  Press Enter to skip — will fall back to logged-in-only mode.")
            entered = getpass.getpass("  Password: ")
            password = entered.strip() or None
        if password is None:
            _log("[INFO] No password — using logged-in-only mode.")
            args.logged_in_only = True

    ok, msg = _create_task(
        password=password, logged_in_only=args.logged_in_only, run_level="HIGHEST"
    )
    run_level_used = "HIGHEST"
    if not ok and "Access is denied" in msg:
        _log("[WARN] Highest privileges denied — retrying with LIMITED (no admin required)...")
        ok, msg = _create_task(
            password=password, logged_in_only=args.logged_in_only, run_level="LIMITED"
        )
        run_level_used = "LIMITED"

    if not ok and password and not args.logged_in_only:
        _log(f"[WARN] Task creation with password failed: {msg}")
        _log("[WARN] Retrying as logged-in-only (no stored password)...")
        ok, msg = _create_task(password=None, logged_in_only=True, run_level=run_level_used)
        args.logged_in_only = True

    if not ok and run_level_used == "HIGHEST":
        _log("[WARN] Retrying with LIMITED privileges...")
        ok, msg = _create_task(
            password=password if not args.logged_in_only else None,
            logged_in_only=args.logged_in_only,
            run_level="LIMITED",
        )
        run_level_used = "LIMITED"

    if not ok:
        _log(f"[FAIL] Could not create scheduled task: {msg}")
        _log("")
        _log("  Try running PowerShell or Command Prompt as Administrator, then re-run:")
        _log("    python scripts/setup_autonomous_task.py")
        return 1

    found, query_out = _query_task()
    if found:
        for line in query_out.splitlines():
            key = line.split(":", 1)[0].strip().lower()
            if key in (
                "taskname",
                "task to run",
                "start in",
                "status",
                "next run time",
                "schedule type",
                "start time",
                "run as user",
                "logon mode",
            ):
                _log(f"  {line.strip()}")

    _print_success(logged_in_only=args.logged_in_only, run_level=run_level_used)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
