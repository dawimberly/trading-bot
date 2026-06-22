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


def _pythonw() -> str:    for candidate in (
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


def _clean_stale_pids(username: str) -> None:
    from modules.portal_bot import book_pid_path, bot_pid

    for book_id in ("alpaca_paper", "alpaca_live"):
        path = book_pid_path(username, book_id)
        if path.is_file() and bot_pid(username, book_id) is None:
            path.unlink(missing_ok=True)
            print(f"Cleared stale PID file for {book_id}", flush=True)


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
    from modules.portal_bot import restart_all_bots, stop_orphan_project_bots
    from modules.portal_paths import bind_project_root, has_alpaca_config

    bind_project_root(ROOT)
    _clean_stale_pids(username)

    _log("Scanning for stray bot processes (may take up to 30 seconds)...")
    stopped, orphan_msg = stop_orphan_project_bots()
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
