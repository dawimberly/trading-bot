#!/usr/bin/env python3
"""Register Windows Task Scheduler job for the overnight research pack.

Daily 08:00 local → Research_Pack.bat (paper overnight pack).
Does not modify run_paper_bot.py or strategy code.

Run from stock-bot/:
  python scripts/setup_research_pack_task.py
  python scripts/setup_research_pack_task.py --logged-in-only
  python scripts/setup_research_pack_task.py --time 08:00
  python scripts/setup_research_pack_task.py --print-only

Remove:
  Remove_Research_Pack_Task.bat
  schtasks /Delete /TN "PythonTrading_OvernightResearchPack" /F
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BAT_PATH = ROOT / "Research_Pack.bat"
TASK_NAME = "PythonTrading_OvernightResearchPack"
DEFAULT_TIME = "08:00"


def _log(msg: str) -> None:
    print(msg, flush=True)


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _windows_user() -> str:
    domain = (os.environ.get("USERDOMAIN") or "").strip()
    user = (os.environ.get("USERNAME") or "").strip()
    if domain and user and domain.upper() != user.upper():
        return f"{domain}\\{user}"
    return user or os.environ.get("COMPUTERNAME", "SYSTEM")


def _manual_schtasks(tr: str, task_time: str) -> str:
    return (
        f'schtasks /Create /TN "{TASK_NAME}" '
        f'/TR "{tr}" /SC DAILY /ST {task_time} /RL LIMITED /F'
    )


def install_task(*, logged_in_only: bool, task_time: str, print_only: bool) -> int:
    if sys.platform != "win32":
        _log("[FAIL] Task Scheduler requires Windows.")
        return 1
    if not BAT_PATH.is_file():
        _log(f"[FAIL] Missing {BAT_PATH}")
        return 1

    # cmd /c with quoted bat; working directory is set via bat's own cd /d "%~dp0"
    tr = f'cmd.exe /c "cd /d {ROOT} && {BAT_PATH}"'
    manual = _manual_schtasks(tr, task_time)

    _log("")
    _log("=== Overnight Research Pack — Task Scheduler Setup ===")
    _log(f"  Project root: {ROOT}")
    _log(f"  Batch file:   {BAT_PATH}")
    _log(f"  Task name:    {TASK_NAME}")
    _log(f"  Schedule:     Daily at {task_time} (local time)")
    _log(f"  Prefer:       LIMITED rights (logged-in-only by default)")
    _log("")
    _log("Manual schtasks create (copy/paste if needed):")
    _log(f"  {manual}")
    _log("")
    _log("Admin note: use 'Run whether user is logged on or not' only if you")
    _log("  need headless runs — that usually requires admin + stored password.")
    _log("  This installer defaults to LIMITED + logged-in-only.")
    _log("")

    if print_only:
        _log("[OK] --print-only: no task created.")
        return 0

    _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])

    cmd = [
        "schtasks",
        "/Create",
        "/TN",
        TASK_NAME,
        "/TR",
        tr,
        "/SC",
        "DAILY",
        "/ST",
        task_time,
        "/RL",
        "LIMITED",
        "/F",
    ]
    if not logged_in_only:
        cmd.extend(["/RU", _windows_user()])

    proc = _run(cmd)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0 and "Access is denied" in out and not logged_in_only:
        _log("[WARN] Create with /RU failed — retrying logged-in-only...")
        logged_in_only = True
        cmd2 = [
            "schtasks",
            "/Create",
            "/TN",
            TASK_NAME,
            "/TR",
            tr,
            "/SC",
            "DAILY",
            "/ST",
            task_time,
            "/RL",
            "LIMITED",
            "/F",
        ]
        proc = _run(cmd2)
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()

    if proc.returncode != 0:
        _log(f"[FAIL] schtasks create failed: {out or proc.returncode}")
        _log("  Try running the terminal as Administrator, or use the manual command above.")
        return 1

    _log(f"[OK] Task registered: {TASK_NAME}")
    if out:
        _log(f"  {out}")
    _log(f'  Verify: schtasks /Query /TN "{TASK_NAME}"')
    _log(f'  Remove: schtasks /Delete /TN "{TASK_NAME}" /F')
    _log("  Or:     Remove_Research_Pack_Task.bat")
    _log("")
    _log("Env reminder: OVERNIGHT_PACK_ENABLED=true for brief Telegram (Section 1+6).")
    _log("Full report: reports/research/YYYY-MM-DD.md")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install daily 08:00 overnight research pack Task Scheduler job"
    )
    parser.add_argument(
        "--logged-in-only",
        action="store_true",
        default=True,
        help="Run only when logged in (default; no stored password)",
    )
    parser.add_argument(
        "--whether-logged-on",
        action="store_true",
        help="Attempt /RU current user (may need admin); prefer logged-in-only",
    )
    parser.add_argument(
        "--time",
        default=DEFAULT_TIME,
        help=f"Daily local start time HH:MM (default {DEFAULT_TIME})",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Print schtasks command only; do not register",
    )
    args = parser.parse_args()
    logged_in_only = not args.whether_logged_on
    return install_task(
        logged_in_only=logged_in_only,
        task_time=args.time.strip(),
        print_only=args.print_only,
    )


if __name__ == "__main__":
    raise SystemExit(main())
