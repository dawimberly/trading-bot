"""Weekday session check: restart dead live, Medium SoT, and Lab.

The scheduled task runs this every 30 minutes from 9:35 through 16:05 ET.

Never uses restart_bot() (that orphan-killer can stop a healthy sibling).
Does not change strategy or .env.
A running Phase 4 Medium paper supervisor is left alone.

Lab is always in the check. If a Lab start exits again (the paper supervisor
gives up after three startup crashes), the book is left down and a hold file
blocks the next morning from repeating that loop.

  python scripts/ops/morning_bot_health.py
  python scripts/ops/morning_bot_health.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_ET = ZoneInfo("America/New_York")
REQUIRED = ("alpaca_live", "alpaca_paper_v2")
LAB = "alpaca_paper"
STALE_AFTER = timedelta(minutes=8)
# Paper supervisor backs off 3s + 6s + 9s, then exits. 75s covers that loop.
LAB_SETTLE_SEC = 75.0
LOG_NAME = "morning_bot_health.log"
HOLD_NAME = "morning_bot_health_hold.json"


def _username() -> str:
    return (os.getenv("PORTAL_USERNAME") or "dawimberly").strip().lower() or "dawimberly"


def _is_session_weekday(now: datetime) -> bool:
    return now.weekday() < 5


def _managed_bot_cmd(cmd: str) -> bool:
    low = (cmd or "").lower()
    return any(
        token in low
        for token in ("run_all.py", "run_paper_bot.py", "weinstein-trading-bot", "lab_next.paper_owner")
    )


def _log_path() -> Path:
    override = (os.getenv("MORNING_BOT_HEALTH_LOG") or "").strip()
    if override:
        return Path(override)
    return ROOT / "data" / LOG_NAME


def _hold_path() -> Path:
    override = (os.getenv("MORNING_BOT_HEALTH_HOLD") or "").strip()
    if override:
        return Path(override)
    return ROOT / "data" / HOLD_NAME


def _read_hold() -> dict:
    path = _hold_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def lab_hold_text() -> str | None:
    row = _read_hold().get(LAB)
    if not isinstance(row, dict):
        return None
    reason = str(row.get("reason") or "startup crash").strip() or "startup crash"
    at = str(row.get("at") or "").strip()
    if at:
        return f"{reason} at {at}"
    return reason


def set_lab_hold(reason: str, now: datetime) -> None:
    data = _read_hold()
    data[LAB] = {"at": now.isoformat(timespec="seconds"), "reason": reason}
    path = _hold_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def clear_lab_hold() -> None:
    data = _read_hold()
    if LAB not in data:
        return
    data.pop(LAB, None)
    path = _hold_path()
    if data:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def _heartbeat_age(path: Path, now: datetime) -> timedelta | None:
    if not path.is_file():
        return None
    # File mtime is timezone-correct. JSON timestamps in this repo are naive local.
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=now.tzinfo)
    return now - mtime


def _phase4_paper_running(username: str) -> bool:
    """True when Medium paper is the Phase 4 worktree supervisor.

    Morning health must not replace that process with the main-tree bot.
    A dead Phase 4 process still follows the normal restart path.
    """
    from modules.portal_bot import _process_cmdline, bot_pid

    pid = bot_pid(username, "alpaca_paper_v2")
    if pid is None:
        return False
    cmd = (_process_cmdline(pid) or "").lower()
    return "pythontrading-phase4" in cmd and "run_paper_bot.py" in cmd


def _book_ok(username: str, book_id: str, now: datetime) -> tuple[bool, str]:
    from modules.portal_bot import _process_cmdline, bot_pid
    from modules.portal_paths import book_heartbeat_path

    pid = bot_pid(username, book_id)
    if pid is None:
        return False, "no live process"
    cmd = _process_cmdline(pid) or ""
    if cmd and not _managed_bot_cmd(cmd):
        return False, f"PID {pid} is not a bot ({cmd[:80]})"
    age = _heartbeat_age(book_heartbeat_path(username, book_id), now)
    if age is None:
        return False, f"PID {pid} but heartbeat missing/unreadable"
    if age > STALE_AFTER:
        return False, f"PID {pid} heartbeat stale {int(age.total_seconds())}s"
    return True, f"PID {pid} heartbeat {int(age.total_seconds())}s"


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _lab_decision(username: str) -> tuple[dict, int | None]:
    """Dead Lab Next restarts as Lab Next. A reused pid is not stopped."""
    import importlib.util

    from modules.portal_bot import _pid_alive, _process_cmdline
    from modules.portal_paths import book_pid_path

    route_path = Path(r"C:\Users\Owner\PythonTradingDeployments\lab_next\lab_next\owner_route.py")
    if not route_path.is_file():
        return {"action": "blocked", "stop": False, "start": None}, None
    spec = importlib.util.spec_from_file_location("lab_next_owner_route", route_path)
    if spec is None or spec.loader is None:
        return {"action": "blocked", "stop": False, "start": None}, None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pid_path = book_pid_path(username, "alpaca_paper")
    pid = _read_pid(pid_path) if pid_path.is_file() else None
    alive = bool(pid and _pid_alive(pid))
    command = _process_cmdline(pid) if alive and pid else ""
    others: set[int] = set()
    for book_id in ("alpaca_live", "alpaca_paper_v2"):
        other = book_pid_path(username, book_id)
        value = _read_pid(other) if other.is_file() else None
        if value:
            others.add(value)
    marker = pid_path.parent / "lab_next_owner.json"
    decision = module.lab_replacement_action(
        book="alpaca_paper",
        pid=pid,
        pid_alive=alive,
        command=command or "",
        marker_present=marker.is_file(),
        other_book_pids=others,
    )
    return decision, pid


def _fix_book(username: str, book_id: str) -> tuple[bool, str]:
    from modules.portal_bot import (
        _graceful_stop_pid,
        _process_cmdline,
        bot_pid,
        start_bot,
    )
    from modules.portal_paths import book_pid_path

    pid = bot_pid(username, book_id)
    if pid is not None:
        cmd = _process_cmdline(pid) or ""
        if _managed_bot_cmd(cmd) or not cmd:
            ok_stop, stop_msg = _graceful_stop_pid(pid)
            book_pid_path(username, book_id).unlink(missing_ok=True)
            if not ok_stop:
                return False, stop_msg
        else:
            book_pid_path(username, book_id).unlink(missing_ok=True)
    ok, msg = start_bot(username, book_id, skip_orphan_stop=True)
    return ok, msg


def _process_stayed_up(
    username: str,
    book_id: str,
    *,
    seconds: float = LAB_SETTLE_SEC,
    poll: float = 5.0,
) -> bool:
    """True when the pid is still a live bot after ``seconds``."""
    from modules.portal_bot import bot_pid

    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        if bot_pid(username, book_id) is None:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(poll, remaining))


def _telegram(text: str, log) -> None:
    try:
        from modules.alerts import send_telegram

        send_telegram(text)
    except Exception as exc:
        log(f"telegram skip: {exc}")


def _leave_lab_down(now: datetime, reason: str, log) -> None:
    set_lab_hold(reason, now)
    log(f"{LAB}: HOLD — {reason}. Left down until {_hold_path().name} is cleared.")
    _telegram(
        "Morning bot health: Lab (alpaca_paper) startup failed and was left down. "
        f"Clear data/{HOLD_NAME} after the startup crash is fixed.",
        log,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Morning live/Medium/Lab bot health")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--force-weekday",
        action="store_true",
        help="Run even on Sat/Sun (for tests)",
    )
    args = parser.parse_args(argv)

    now = datetime.now(_ET)
    log_path = _log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(line: str) -> None:
        stamp = now.strftime("%Y-%m-%d %H:%M:%S ET")
        text = f"{stamp} {line}"
        print(text, flush=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")

    if not args.force_weekday and not _is_session_weekday(now):
        log("skip: weekend")
        return 0

    username = _username()
    books = list(REQUIRED) + [LAB]

    restarted: list[str] = []
    failed: list[str] = []
    for book_id in books:
        if book_id == "alpaca_paper_v2" and _phase4_paper_running(username):
            log(f"{book_id}: Phase 4 trial is running — not restarting")
            continue
        if book_id == LAB:
            decision, pid = _lab_decision(username)
            if decision["action"] == "keep":
                clear_lab_hold()
                log(f"{LAB}: owner running PID {pid}; not duplicating")
                continue
            if decision["action"] == "blocked":
                log(f"{LAB}: BLOCKED — Lab Next route is unavailable. Old Lab was not started.")
                failed.append(book_id)
                continue
            if decision["stop"] and pid:
                from modules.portal_bot import _graceful_stop_pid

                _graceful_stop_pid(pid)
            elif pid:
                log(f"{LAB}: not stopping pid {pid}")
            from modules.portal_paths import book_pid_path

            book_pid_path(username, book_id).unlink(missing_ok=True)
            if args.dry_run:
                log(f"{LAB}: dry-run would start Lab Next")
                continue
            from modules.portal_bot import start_bot

            fixed, msg = start_bot(username, book_id, skip_orphan_stop=True)
            log(f"{LAB}: {'started' if fixed else 'FAILED'} — {msg}")
            if not fixed or not _process_stayed_up(username, book_id):
                failed.append(book_id)
                _leave_lab_down(now, "startup failed", log)
            else:
                restarted.append(book_id)
            continue
        ok, detail = _book_ok(username, book_id, now)
        if ok:
            if book_id == LAB:
                clear_lab_hold()
            log(f"{book_id}: OK — {detail}")
            continue
        if book_id == LAB:
            held = lab_hold_text()
            if held:
                log(f"{LAB}: HOLD — {held}. Not restarting.")
                continue
        log(f"{book_id}: NEED FIX — {detail}")
        if args.dry_run:
            continue
        fixed, msg = _fix_book(username, book_id)
        log(f"{book_id}: {'started' if fixed else 'FAILED'} — {msg}")
        if not fixed:
            failed.append(book_id)
            if book_id == LAB:
                _leave_lab_down(now, "startup failed", log)
            continue
        if book_id == LAB and not _process_stayed_up(username, book_id):
            log(f"{LAB}: process exited after start")
            failed.append(book_id)
            _leave_lab_down(now, "started then exited", log)
            continue
        restarted.append(book_id)

    if restarted:
        _telegram(
            "Morning bot health: restarted " + ", ".join(restarted),
            log,
        )

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
