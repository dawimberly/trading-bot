"""Stop stray bots/dashboards and restart both Alpaca books for the portal user.

Run from stock-bot/:
  python scripts/owner_reset.py
  python scripts/owner_reset.py --no-dashboard
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("PYTHONTRADING_ROOT", str(ROOT))


def _log(msg: str) -> None:
    print(msg, flush=True)


def _pythonw() -> str:
    for candidate in (
        ROOT / ".venv" / "Scripts" / "pythonw.exe",
        ROOT.parent / ".venv" / "Scripts" / "pythonw.exe",
        Path(sys.executable).with_name("pythonw.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _stop_dashboards() -> None:
    ps1 = ROOT / "scripts" / "stop_dashboard.ps1"
    if ps1.is_file():
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
            cwd=str(ROOT),
            check=False,
        )


def _force_clear_all_pids(username: str) -> None:
    """Unconditionally wipe all book PID files before the orphan sweep.

    The old _clean_stale_pids only removed PID files whose process was already
    dead.  Processes that are alive-but-orphaned kept their PID files, which
    caused stop_orphan_project_bots to treat them as 'managed' and skip them.
    Clearing everything here means the orphan sweep has an empty preserve-set
    and will kill every stray run_all / run_paper_bot process unconditionally.
    """
    from modules.portal_bot import book_pid_path

    for book_id in ("alpaca_paper", "alpaca_live"):
        path = book_pid_path(username, book_id)
        if path.is_file():
            path.unlink(missing_ok=True)
            print(f"Cleared PID file for {book_id}", flush=True)


def _default_username() -> str:
    prefs = ROOT / "data" / "portal" / "desktop_prefs.json"
    if prefs.is_file():
        try:
            import json

            data = json.loads(prefs.read_text(encoding="utf-8"))
            name = str(data.get("last_username") or "").strip().lower()
            if name:
                return name
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return "dawimberly"


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset owner Alpaca bots + monitor")
    parser.add_argument(
        "--username",
        default=os.getenv("PORTAL_USERNAME") or _default_username(),
    )
    parser.add_argument("--no-dashboard", action="store_true", help="Skip monitor relaunch")
    args = parser.parse_args()
    username = args.username.strip().lower()

    print(f"=== PythonTrading reset ({username}) ===", flush=True)
    print(f"Project root: {ROOT}", flush=True)

    _log("Stopping old dashboard windows (if any)...")
    _stop_dashboards()
    time.sleep(0.5)

    _log("Loading portal bot manager...")
    from modules.portal_bot import (
        restart_all_bots,
        stop_bot,
        stop_orphan_project_bots,
        bot_running,
    )
    from modules.portal_paths import bind_project_root, has_alpaca_config

    bind_project_root(ROOT)

    # Force-clear all PID files so the orphan sweep has no preserved PIDs
    # and will kill every stray bot process unconditionally.
    _force_clear_all_pids(username)

    _log("Stopping any running portal bots...")
    for book_id in ("alpaca_paper", "alpaca_live"):
        if bot_running(username, book_id):
            _ok, stop_msg = stop_bot(username, book_id)
            _log(stop_msg)

    _log("Scanning for stray bot processes (please wait, can take 30-60 seconds)...")
    stopped, orphan_msg = stop_orphan_project_bots(username=username)
    _log(orphan_msg if stopped else f"{orphan_msg} Continuing.")
    if stopped:
        time.sleep(1.0)

    for book_id in ("alpaca_paper", "alpaca_live"):
        if not has_alpaca_config(username, book_id):
            _log(f"[WARN] {book_id}: Alpaca keys missing in portal — skip")

    _log("Starting Live + Paper bots...")
    ok, msg = restart_all_bots(username)
    _log(msg if ok else f"[ERROR] {msg}")

    if not args.no_dashboard:
        _stop_dashboards()
        time.sleep(1.5)  # increased from 0.5 — give Windows time to release handles
        _log("Opening dashboard (pythonw, no console)...")
        env = os.environ.copy()
        env["PYTHONTRADING_ROOT"] = str(ROOT)
        pyw = _pythonw()
        script = ROOT / "dashboard_app.py"
        subprocess.Popen(
            [pyw, str(script)],
            cwd=str(ROOT),
            env=env,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        _log(f"Monitor launched via {Path(pyw).name} - sign in as {username}")

    _log("Done. Wait ~60s for fresh heartbeats, then check the Overview tab.")
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main())