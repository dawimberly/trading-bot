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
    # Prefer venv311 (working deps). Repo .venv may be Python 3.14 / incomplete.
    for candidate in (
        ROOT.parent / "venv311" / "Scripts" / "pythonw.exe",
        ROOT.parent / ".venv" / "Scripts" / "pythonw.exe",
        ROOT / ".venv" / "Scripts" / "pythonw.exe",
        Path(sys.executable).with_name("pythonw.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _stop_dashboards() -> None:
    ps1 = ROOT / "scripts" / "stop_dashboard.ps1"
    if not ps1.is_file():
        return
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
            cwd=str(ROOT),
            check=False,
            timeout=45,
        )
    except subprocess.TimeoutExpired:
        _log("[WARN] stop_dashboard.ps1 timed out after 45s — continuing")


def _force_clear_all_pids(username: str) -> None:
    """Unconditionally wipe all book PID files before the orphan sweep.

    The old _clean_stale_pids only removed PID files whose process was already
    dead.  Processes that are alive-but-orphaned kept their PID files, which
    caused stop_orphan_project_bots to treat them as 'managed' and skip them.
    Clearing everything here means the orphan sweep has an empty preserve-set
    and will kill every stray run_all / run_paper_bot process unconditionally.
    """
    from modules.portal_bot import book_pid_path
    from modules.trading_books import PRIMARY_PAPER_BOOK_ID

    for book_id in (PRIMARY_PAPER_BOOK_ID, "alpaca_live"):
        path = book_pid_path(username, book_id)
        if path.is_file():
            path.unlink(missing_ok=True)
            print(f"Cleared PID file for {book_id}", flush=True)


def _live_preserve_pids(username: str) -> set[int]:
    """PIDs to preserve during paper-only orphan sweep (live book + parent chain)."""
    from modules.portal_bot import _parent_pid, _process_cmdline, bot_pid

    preserve: set[int] = set()
    live = bot_pid(username, "alpaca_live")
    if live is None:
        return preserve
    preserve.add(live)
    walk = live
    for _ in range(32):
        parent = _parent_pid(walk)
        if parent is None:
            break
        cmd = _process_cmdline(parent) or ""
        if "run_all.py" not in cmd:
            break
        preserve.add(parent)
        walk = parent
    return preserve


def _clear_paper_pid_only(username: str) -> None:
    from modules.portal_bot import book_pid_path
    from modules.trading_books import PRIMARY_PAPER_BOOK_ID

    path = book_pid_path(username, PRIMARY_PAPER_BOOK_ID)
    if path.is_file():
        path.unlink(missing_ok=True)


def clean_restart_paper_only(username: str) -> tuple[bool, str]:
    """Kill orphans (preserve live), start paper bot only — no live restart or dashboard."""
    from modules.portal_bot import (
        bot_running,
        start_bot,
        stop_bot,
        stop_orphan_project_bots,
    )
    from modules.portal_paths import bind_project_root, has_alpaca_config

    bind_project_root(ROOT)

    from modules.trading_books import PRIMARY_PAPER_BOOK_ID

    if not has_alpaca_config(username, PRIMARY_PAPER_BOOK_ID):
        return False, f"{PRIMARY_PAPER_BOOK_ID}: Alpaca keys missing in portal — aborting (paper only)."

    _clear_paper_pid_only(username)

    if bot_running(username, PRIMARY_PAPER_BOOK_ID):
        _ok, stop_msg = stop_bot(username, PRIMARY_PAPER_BOOK_ID)
        if not _ok:
            return False, f"stop paper: {stop_msg}"
        time.sleep(1.0)

    preserve = _live_preserve_pids(username)
    stopped, orphan_msg = stop_orphan_project_bots(
        preserve_pids=preserve, username=username
    )
    if stopped:
        time.sleep(1.0)

    ok, msg = start_bot(username, PRIMARY_PAPER_BOOK_ID, skip_orphan_stop=True)
    detail = orphan_msg if orphan_msg else "Paper bot started."
    return ok, f"{msg} | {detail}" if ok else msg


def clean_restart_both_bots(username: str) -> tuple[bool, str]:
    """Force-clear PID files, then restart live + paper via portal_bot — no dashboard.

    Shared by ``owner_reset`` CLI and dashboard open/close/Restart Both so kill
    logic stays in one place. Clearing PID files first is required: otherwise
    ``stop_orphan_project_bots`` treats alive processes as managed and skips them.
    """
    from modules.portal_bot import restart_all_bots
    from modules.portal_paths import bind_project_root, has_alpaca_config
    from modules.trading_books import PRIMARY_PAPER_BOOK_ID

    bind_project_root(ROOT)
    _force_clear_all_pids(username)

    warnings: list[str] = []
    for book_id in (PRIMARY_PAPER_BOOK_ID, "alpaca_live"):
        if not has_alpaca_config(username, book_id):
            warnings.append(f"{book_id}: Alpaca keys missing in portal — skip")

    ok, msg = restart_all_bots(username)
    if warnings:
        return ok, " | ".join(warnings + [msg])
    return ok, msg


def stop_both_bots(username: str) -> tuple[bool, str]:
    """Gracefully stop portal live + primary paper books (positions left open)."""
    from modules.portal_bot import bot_running, stop_bot
    from modules.portal_paths import bind_project_root
    from modules.trading_books import PRIMARY_PAPER_BOOK_ID

    bind_project_root(ROOT)
    messages: list[str] = []
    ok_all = True
    any_stopped = False
    for book_id in (PRIMARY_PAPER_BOOK_ID, "alpaca_live"):
        if not bot_running(username, book_id):
            continue
        any_stopped = True
        ok, msg = stop_bot(username, book_id)
        messages.append(f"{book_id}: {msg}")
        ok_all = ok_all and ok
    if not any_stopped:
        return True, "No portal bots were running."
    return ok_all, "; ".join(messages)


def wait_for_paper_heartbeat(username: str, timeout_sec: int = 75) -> tuple[bool, str]:
    """Poll the paper book heartbeat until it is fresh (bot responding)."""
    from modules.portal_bot import book_heartbeat_path

    from modules.trading_books import PRIMARY_PAPER_BOOK_ID

    path = book_heartbeat_path(username, PRIMARY_PAPER_BOOK_ID)
    deadline = time.monotonic() + max(10, timeout_sec)
    last_age: float | None = None
    while time.monotonic() < deadline:
        age = _heartbeat_age_sec(path)
        if age is not None:
            last_age = age
            if age < 120:
                return True, f"heartbeat fresh ({age:.0f}s old)"
        time.sleep(3)
    if last_age is not None:
        return False, f"heartbeat still stale ({last_age:.0f}s old)"
    return False, "no heartbeat file yet (first cycle may still be warming up)"


def _heartbeat_age_sec(path: Path) -> float | None:
    if not path.is_file():
        return None
    try:
        import json
        from datetime import datetime

        data = json.loads(path.read_text(encoding="utf-8"))
        ts = data.get("timestamp")
        if not ts:
            return None
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)
        return max(0.0, (datetime.now() - parsed).total_seconds())
    except (OSError, ValueError, TypeError):
        return None


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


def _run_paper_only(username: str, *, verify: bool) -> int:
    """Force-restart only the paper bot with strong orphan killing (live preserved)."""
    from modules.portal_paths import bind_project_root

    bind_project_root(ROOT)

    _log("Paper-only force restart (live bot preserved)...")
    ok, msg = clean_restart_paper_only(username)
    _log(msg if ok else f"[ERROR] {msg}")
    if not ok:
        _log(f"Bot restart FAILED — check portal Alpaca keys for {PRIMARY_PAPER_BOOK_ID}.")
        return 1

    if verify:
        _log("Verifying paper bot is responding (waiting for fresh heartbeat)...")
        fresh, detail = wait_for_paper_heartbeat(username)
        _log(f"Heartbeat: {detail}")
        if fresh:
            _log("Bot restarted successfully — paper bot is RESPONDING.")
            return 0
        _log(
            "Bot restarted, but heartbeat not confirmed yet. Give it ~60s, "
            "then check the dashboard Overview tab."
        )
        return 0

    _log("Bot restarted successfully (paper only). Wait ~60s for a fresh heartbeat.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset owner Alpaca bots + monitor")
    parser.add_argument(
        "--username",
        default=os.getenv("PORTAL_USERNAME") or _default_username(),
    )
    parser.add_argument("--no-dashboard", action="store_true", help="Skip monitor relaunch")
    parser.add_argument(
        "--paper-only",
        action="store_true",
        help="Force-restart only the paper bot (live bot preserved)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Wait for a fresh paper heartbeat and confirm the bot is responding",
    )
    args = parser.parse_args()
    username = args.username.strip().lower()

    print(f"=== PythonTrading reset ({username}) ===", flush=True)
    print(f"Project root: {ROOT}", flush=True)

    if args.paper_only:
        return _run_paper_only(username, verify=args.verify)

    _log("Stopping old dashboard windows (if any)...")
    _stop_dashboards()
    time.sleep(0.5)

    _log("Loading portal bot manager...")
    from modules.portal_paths import bind_project_root

    bind_project_root(ROOT)

    _log("Scanning for stray bot processes + restarting Live + Paper...")
    ok, msg = clean_restart_both_bots(username)
    _log(msg if ok else f"[ERROR] {msg}")
    if ok:
        _log("Bot restarted successfully (live + paper).")

    if not args.no_dashboard:
        _stop_dashboards()
        time.sleep(1.5)  # increased from 0.5 — give Windows time to release handles
        _log("Opening dashboard (pythonw, no console)...")
        env = os.environ.copy()
        env["PYTHONTRADING_ROOT"] = str(ROOT)
        # Bots already restarted above — skip dashboard-on-open dual reset.
        env["DASHBOARD_RESTART_BOTS_ON_OPEN"] = "false"
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