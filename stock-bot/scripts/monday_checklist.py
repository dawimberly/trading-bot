#!/usr/bin/env python3
"""Monday pre-market checklist - automate verify, reset, heartbeat, Telegram, health.

Run from stock-bot/:
  python scripts/monday_checklist.py
  python scripts/monday_checklist.py --quick --skip-reset
  python scripts/monday_checklist.py --install-task
  python scripts/monday_checklist.py --install-task --logged-in-only

Double-click:
  Monday_Checklist.bat

Exit codes:
  0 - all checks PASS or WARN (no FAIL)
  1 - one or more FAIL (or task install failed)

Emergency (manual):
  python scripts/recover_bot.py
  Start_Autonomous.bat
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("PYTHONTRADING_ROOT", str(ROOT))

Status = Literal["PASS", "WARN", "FAIL"]

TASK_NAME = "PythonTrading_Monday_Checklist"
BAT_PATH = ROOT / "Monday_Checklist.bat"
# Premarket Monday local time (adjust via --task-time)
DEFAULT_TASK_TIME = "08:00"
LOG_PATH = ROOT / "logs" / "monday_checklist.log"

_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"
_CYAN = "\033[96m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"
_USE_COLOR = True


@dataclass
class Check:
    name: str
    status: Status
    detail: str = ""


def _c(text: str, code: str) -> str:
    if not _USE_COLOR:
        return text
    return f"{code}{text}{_RESET}"


def _icon(status: Status) -> str:
    if status == "PASS":
        return _c("[PASS]", _GREEN)
    if status == "WARN":
        return _c("[WARN]", _YELLOW)
    return _c("[FAIL]", _RED)


def _log(msg: str) -> None:
    print(msg, flush=True)


def _section(title: str) -> None:
    _log("")
    _log(_c("=" * 72, _BOLD))
    _log(_c(f"  {title}", _BOLD + _CYAN))
    _log(_c("=" * 72, _BOLD))


def _record(checks: list[Check], name: str, status: Status, detail: str = "") -> None:
    checks.append(Check(name=name, status=status, detail=detail))
    line = f"  {_icon(status)}  {name}"
    if detail:
        line = f"{line:<48} {detail}"
    _log(line)


def _python() -> str:
    for candidate in (
        ROOT.parent / ".venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "Scripts" / "python.exe",
        Path(sys.executable),
    ):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _run_py(
    script_rel: str,
    *args: str,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [_python(), "-u", str(ROOT / script_rel), *args]
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=run_env,
    )


def _default_username() -> str:
    prefs = ROOT / "data" / "portal" / "desktop_prefs.json"
    if prefs.is_file():
        try:
            data = json.loads(prefs.read_text(encoding="utf-8"))
            name = str(data.get("last_username") or "").strip().lower()
            if name:
                return name
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return (os.getenv("PORTAL_USERNAME") or "dawimberly").strip().lower()


def _heartbeat_age_sec(path: Path) -> float | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ts = data.get("timestamp")
        if not ts:
            age = time.time() - path.stat().st_mtime
            return max(0.0, age)
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)
        return max(0.0, (datetime.now() - parsed).total_seconds())
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        try:
            return max(0.0, time.time() - path.stat().st_mtime)
        except OSError:
            return None


def _load_heartbeat(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _wait_heartbeat(path: Path, *, timeout_sec: int = 90, fresh_sec: float = 180) -> tuple[bool, str]:
    deadline = time.monotonic() + max(10, timeout_sec)
    last_age: float | None = None
    while time.monotonic() < deadline:
        age = _heartbeat_age_sec(path)
        if age is not None:
            last_age = age
            if age < fresh_sec:
                return True, f"fresh ({age:.0f}s old)"
        time.sleep(3)
    if last_age is not None:
        return False, f"stale ({last_age:.0f}s old)"
    return False, "no heartbeat file yet"


# ---------------------------------------------------------------------------
# Checklist steps
# ---------------------------------------------------------------------------


def step_verify(checks: list[Check], *, quick: bool) -> None:
    _section("1/7  Full verify - paper + live FINAL LOCK")
    extra = ["--quick"] if quick else []

    _log(_c("  -> Paper: scripts/full_system_verify.py", _DIM))
    paper = _run_py("scripts/full_system_verify.py", *extra)
    if paper.returncode == 0:
        _record(checks, "Paper full_system_verify", "PASS", "exit 0 (PASS/WARN OK)")
    else:
        _record(checks, "Paper full_system_verify", "FAIL", f"exit {paper.returncode}")

    _log(_c("  -> Live: scripts/full_system_verify.py --live", _DIM))
    live = _run_py("scripts/full_system_verify.py", "--live", *extra)
    if live.returncode == 0:
        _record(checks, "Live full_system_verify --live", "PASS", "exit 0 (PASS/WARN OK)")
    else:
        _record(checks, "Live full_system_verify --live", "FAIL", f"exit {live.returncode}")

    # Lock_v15 stamp (lightweight - full verify already covered paper above).
    # To re-apply locks: run Lock_v15.bat separately.
    lock_file = ROOT / "data" / "realistic_research_v15.lock.json"
    try:
        import config

        ver = str(getattr(config, "REALISTIC_RESEARCH_VERSION", ""))
        if ver == "1.5.4":
            _record(checks, "Lock_v15 version", "PASS", f"REALISTIC_RESEARCH_VERSION={ver}")
        else:
            _record(checks, "Lock_v15 version", "FAIL", f"expected 1.5.4 got {ver}")
        if lock_file.is_file():
            try:
                stamp = json.loads(lock_file.read_text(encoding="utf-8"))
                locked_at = str(stamp.get("locked_at") or stamp.get("version") or "")[:40]
                _record(checks, "Lock_v15 stamp", "PASS", locked_at or str(lock_file.name))
            except (OSError, json.JSONDecodeError, TypeError):
                _record(checks, "Lock_v15 stamp", "WARN", "lock file unreadable")
        else:
            _record(
                checks,
                "Lock_v15 stamp",
                "WARN",
                "missing - run Lock_v15.bat once to write stamp",
            )
    except Exception as exc:
        _record(checks, "Lock_v15", "WARN", str(exc)[:70])


def step_owner_reset(checks: list[Check], *, skip: bool, username: str) -> None:
    _section("2/7  owner_reset (live + paper + dashboard)")
    if skip:
        _record(checks, "owner_reset.py", "WARN", "skipped (--skip-reset)")
        return

    _log(_c(f"  -> python scripts/owner_reset.py  (user={username})", _DIM))
    proc = _run_py("scripts/owner_reset.py", "--username", username)
    if proc.returncode == 0:
        _record(checks, "owner_reset.py", "PASS", "live + paper restart requested")
    else:
        _record(checks, "owner_reset.py", "FAIL", f"exit {proc.returncode}")


def step_books_responding(
    checks: list[Check],
    *,
    username: str,
    wait_sec: int,
    skip_wait: bool,
) -> None:
    _section("3/7  Both books RESPONDING + FINAL LOCK banners")
    import config
    from modules.portal_bot import book_heartbeat_path, bot_running
    from modules.portal_paths import bind_project_root

    bind_project_root(ROOT)

    # FINAL LOCK banners (config - always available)
    try:
        paper_hl = config.format_realistic_research_headline()
        live_hl = config.format_live_conservative_headline()
        cross = config.format_paper_live_profile_line()
        if "FINAL LOCK" in (paper_hl or "").upper():
            _record(checks, "Paper FINAL LOCK banner", "PASS", paper_hl[:70])
        else:
            _record(checks, "Paper FINAL LOCK banner", "FAIL", paper_hl[:70] or "missing")
        if "FINAL LOCK" in (live_hl or "").upper():
            _record(checks, "Live FINAL LOCK banner", "PASS", live_hl[:70])
        else:
            _record(checks, "Live FINAL LOCK banner", "FAIL", live_hl[:70] or "missing")
        _log(_c(f"  Paper: {paper_hl[:90]}", _DIM))
        _log(_c(f"  Live:  {live_hl[:90]}", _DIM))
        _log(_c(f"  Dual:  {cross[:90]}", _DIM))
    except Exception as exc:
        _record(checks, "FINAL LOCK banners", "FAIL", str(exc)[:80])

    for book_id, label in (("alpaca_paper", "Paper"), ("alpaca_live", "Live")):
        path = book_heartbeat_path(username, book_id)
        running = bot_running(username, book_id)
        if not running:
            _record(checks, f"{label} process", "FAIL", "not running - start via owner_reset")
            age = _heartbeat_age_sec(path)
            if age is not None:
                _record(
                    checks,
                    f"{label} heartbeat",
                    "WARN",
                    f"age={age:.0f}s path={path.name} (process down)",
                )
            else:
                _record(checks, f"{label} heartbeat", "FAIL", f"missing {path}")
            continue

        _record(checks, f"{label} process", "PASS", "running")
        if skip_wait:
            age = _heartbeat_age_sec(path)
            if age is not None and age < 1800:
                _record(checks, f"{label} RESPONDING", "PASS", f"heartbeat fresh ({age:.0f}s)")
            elif age is not None and age < 5400:
                _record(checks, f"{label} RESPONDING", "WARN", f"heartbeat age={age:.0f}s")
            else:
                _record(
                    checks,
                    f"{label} RESPONDING",
                    "FAIL",
                    f"stale/missing age={age}" if age is not None else "no heartbeat",
                )
        else:
            _log(_c(f"  Waiting up to {wait_sec}s for {label} heartbeat...", _DIM))
            ok, detail = _wait_heartbeat(path, timeout_sec=wait_sec)
            if ok:
                _record(checks, f"{label} RESPONDING", "PASS", detail)
            else:
                _record(checks, f"{label} RESPONDING", "FAIL", detail)


def step_telegram_status(checks: list[Check], *, username: str, send: bool) -> None:
    _section("4/7  Telegram /status (paper + live)")
    import config
    from modules import alerts
    from modules.portal_bot import book_heartbeat_path
    from modules.portal_paths import bind_project_root
    from modules.telegram_commands import format_status_command

    bind_project_root(ROOT)

    def _equity_regime(book_id: str) -> tuple[float | None, float | None, str]:
        hb = _load_heartbeat(book_heartbeat_path(username, book_id))
        equity = cash = None
        regime = ""
        if hb:
            try:
                equity = float(hb["equity"]) if hb.get("equity") is not None else None
            except (TypeError, ValueError):
                equity = None
            try:
                cash = float(hb["cash"]) if hb.get("cash") is not None else None
            except (TypeError, ValueError):
                cash = None
            regime = str(hb.get("regime") or "")
        return equity, cash, regime

    tg = config.get_telegram_config()
    if tg:
        _record(checks, "Telegram config", "PASS", "token + chat_id present")
    else:
        _record(checks, "Telegram config", "WARN", "TELEGRAM_* not set - print only")

    messages: list[tuple[str, str]] = []
    orig_paper = bool(getattr(config, "PAPER_TRADING", True))
    for paper, book_id, label in (
        (True, "alpaca_paper", "Paper"),
        (False, "alpaca_live", "Live"),
    ):
        equity, cash, regime = _equity_regime(book_id)
        try:
            config.PAPER_TRADING = paper
            text = format_status_command(equity=equity, cash=cash, regime=regime or "n/a")
        except Exception as exc:
            _record(checks, f"{label} /status text", "FAIL", str(exc)[:70])
            continue
        finally:
            config.PAPER_TRADING = orig_paper

        if text and text.strip():
            first = text.split("\n")[0][:60]
            _record(checks, f"{label} /status text", "PASS", first)
            messages.append((label, text))
            _log(_c(f"  --- {label} /status ---", _DIM))
            for line in text.splitlines()[:12]:
                _log(f"    {line}")
            if text.count("\n") > 12:
                _log("    ...")
        else:
            _record(checks, f"{label} /status text", "FAIL", "empty reply")

    if send and messages and tg:
        combined = (
            "Monday Checklist - /status\n\n"
            + "\n\n".join(f"=== {label} ===\n{body}" for label, body in messages)
        )
        ok = alerts.send_telegram(combined[:4000])
        if ok:
            _record(checks, "Telegram send", "PASS", "paper+live status sent")
        else:
            _record(checks, "Telegram send", "WARN", "send failed - printed above")
    elif send and not tg:
        _record(checks, "Telegram send", "WARN", "skipped - no Telegram config")
    else:
        _record(checks, "Telegram send", "PASS", "print-only (--no-telegram-send)")


def step_paper_health_strategy(checks: list[Check]) -> None:
    _section("5/7  Paper Health >=90 + Strategy Performance")
    try:
        from modules.bot_health import (
            calculate_health_score,
            format_health_line,
            gather_health_context,
        )

        ctx = gather_health_context({"regime": "RHYME_C"})
        health = calculate_health_score(**ctx)
        score = int(health.get("score") or 0)
        grade = str(health.get("grade") or "?")
        line = format_health_line(health) or f"{score}/100 ({grade})"
        if score >= 90:
            _record(checks, "Paper Health >=90", "PASS", line.strip()[:70])
        elif score >= 70:
            _record(checks, "Paper Health >=90", "WARN", f"{line.strip()[:60]} (target 90+)")
        else:
            _record(checks, "Paper Health >=90", "FAIL", f"{line.strip()[:60]} (target 90+)")
    except Exception as exc:
        _record(checks, "Paper Health >=90", "FAIL", str(exc)[:80])

    try:
        from modules.strategy_performance import get_strategy_ratings
        import config

        ratings = get_strategy_ratings(days=30)
        ranked = ratings.get("ranked") or []
        db = Path(getattr(config, "STRATEGY_METRICS_DB", "data/strategy_metrics.db"))
        if not db.is_absolute():
            db = ROOT / db
        mtime_detail = ""
        if db.is_file():
            age_h = (time.time() - db.stat().st_mtime) / 3600.0
            mtime_detail = f"db age={age_h:.1f}h"
            if age_h > 72:
                _record(
                    checks,
                    "Strategy Performance updating",
                    "WARN",
                    f"{mtime_detail} - may be stale",
                )
            else:
                if ranked:
                    top = ranked[0]
                    _record(
                        checks,
                        "Strategy Performance updating",
                        "PASS",
                        f"{top.get('label', '?')}: {top.get('rating')} "
                        f"({top.get('trade_count')} trades) {mtime_detail}",
                    )
                else:
                    _record(
                        checks,
                        "Strategy Performance updating",
                        "WARN",
                        f"no closed trades yet - {mtime_detail}",
                    )
        else:
            _record(
                checks,
                "Strategy Performance updating",
                "WARN",
                f"metrics DB missing ({db.name}) - will create after trades",
            )
    except Exception as exc:
        _record(checks, "Strategy Performance updating", "FAIL", str(exc)[:80])


def step_during_market_reminder(checks: list[Check]) -> None:
    _section("6/7  During market (manual reminder)")
    _log("  Keep phone Telegram alerts ON during RTH.")
    _log("  Watch dashboard Overview for STALE heartbeats / last_cycle_error.")
    _log("  Do not flip live GARCH / ARIMA / HMM-primary mid-session.")
    _record(
        checks,
        "During-market alerts",
        "PASS",
        "reminder printed - human: leave Telegram alerts on",
    )


def step_friday_weekly_path(checks: list[Check]) -> None:
    _section("7/7  After close (Friday) - weekly path intact")
    import config

    weekly_mod = ROOT / "modules" / "weekly_telegram_summary.py"
    weekly_script = ROOT / "scripts" / "weekly_telegram_summary.py"
    if weekly_mod.is_file() and weekly_script.is_file():
        _record(checks, "Weekly Telegram modules", "PASS", "modules + scripts present")
    else:
        _record(
            checks,
            "Weekly Telegram modules",
            "FAIL",
            f"missing mod={weekly_mod.is_file()} script={weekly_script.is_file()}",
        )
        return

    try:
        from modules.weekly_telegram_summary import (
            send_weekly_telegram_summary,
            weekly_telegram_due,
        )

        assert callable(weekly_telegram_due) and callable(send_weekly_telegram_summary)
        enabled = bool(config.telegram_weekly_summary_enabled())
        when = getattr(config, "TELEGRAM_WEEKLY_SUMMARY_TIME", "16:30")
        if enabled:
            _record(
                checks,
                "Friday weekly path",
                "PASS",
                f"enabled - Fridays after {when} ET (run_all hooks)",
            )
        else:
            _record(
                checks,
                "Friday weekly path",
                "WARN",
                "telegram_weekly_summary_enabled() is False - paper default is ON",
            )
        # Confirm wiring still referenced from run_all (do not execute send).
        run_all = (ROOT / "run_all.py").read_text(encoding="utf-8", errors="ignore")
        if "send_weekly_telegram_summary" in run_all or "weekly_telegram_summary" in run_all:
            _record(checks, "run_all weekly hook", "PASS", "weekly_telegram_summary referenced")
        else:
            _record(checks, "run_all weekly hook", "FAIL", "weekly hook missing from run_all.py")
    except Exception as exc:
        _record(checks, "Friday weekly path", "FAIL", str(exc)[:80])


def print_emergency_help() -> None:
    _log("")
    _log(_c("Emergency recovery", _BOLD))
    _log("  python scripts/recover_bot.py     # paper safe reset + RESPONDING wait")
    _log("  Start_Autonomous.bat              # overnight paper + 9 AM Telegram")
    _log("  python scripts/owner_reset.py     # full live + paper + dashboard")
    _log("")


def print_summary(checks: list[Check]) -> Status:
    pass_n = sum(1 for c in checks if c.status == "PASS")
    warn_n = sum(1 for c in checks if c.status == "WARN")
    fail_n = sum(1 for c in checks if c.status == "FAIL")
    _log("")
    _log(_c("=" * 72, _BOLD))
    _log(_c("  MONDAY CHECKLIST SUMMARY", _BOLD))
    _log(_c("=" * 72, _BOLD))
    for c in checks:
        detail = f"  {c.detail}" if c.detail else ""
        _log(f"  {_icon(c.status)}  {c.name}{detail}")
    _log(_c("-" * 72, _DIM))
    if fail_n:
        overall: Status = "FAIL"
        verdict = f"{fail_n} FAIL - fix before market open"
    elif warn_n:
        overall = "WARN"
        verdict = f"{warn_n} WARN - operational with caveats"
    else:
        overall = "PASS"
        verdict = "All checks PASS - ready for Monday"
    _log(
        f"  Overall: {_icon(overall)}  "
        f"({pass_n} pass / {warn_n} warn / {fail_n} fail)  {verdict}"
    )
    _log(_c("=" * 72, _BOLD))
    return overall


# ---------------------------------------------------------------------------
# Task Scheduler (--install-task)
# ---------------------------------------------------------------------------


def _run_cmd(cmd: list[str]) -> subprocess.CompletedProcess[str]:
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


def install_task(*, logged_in_only: bool, password: str | None, task_time: str) -> int:
    if sys.platform != "win32":
        _log("[FAIL] Task Scheduler requires Windows.")
        return 1
    if not BAT_PATH.is_file():
        _log(f"[FAIL] Missing {BAT_PATH}")
        return 1

    _log("")
    _log("=== Monday Checklist - Task Scheduler Setup ===")
    _log(f"  Project root: {ROOT}")
    _log(f"  Batch file:   {BAT_PATH}")
    _log(f"  Task name:    {TASK_NAME}")
    _log(f"  Schedule:     Weekly Monday at {task_time} (local time)")
    _log("")

    _run_cmd(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])

    tr = f'cmd.exe /c "{BAT_PATH}"'
    cmd = [
        "schtasks",
        "/Create",
        "/TN",
        TASK_NAME,
        "/TR",
        tr,
        "/SC",
        "WEEKLY",
        "/D",
        "MON",
        "/ST",
        task_time,
        "/RL",
        "HIGHEST",
        "/F",
    ]
    if not logged_in_only:
        cmd.extend(["/RU", _windows_user()])
        if password:
            cmd.extend(["/RP", password])

    proc = _run_cmd(cmd)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    run_level = "HIGHEST"
    if proc.returncode != 0 and "Access is denied" in out:
        _log("[WARN] Highest privileges denied - retrying LIMITED...")
        cmd[cmd.index("HIGHEST")] = "LIMITED"
        proc = _run_cmd(cmd)
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        run_level = "LIMITED"

    if proc.returncode != 0 and password and not logged_in_only:
        _log(f"[WARN] Password mode failed: {out}")
        _log("[WARN] Retrying logged-in-only...")
        logged_in_only = True
        cmd2 = [
            "schtasks",
            "/Create",
            "/TN",
            TASK_NAME,
            "/TR",
            tr,
            "/SC",
            "WEEKLY",
            "/D",
            "MON",
            "/ST",
            task_time,
            "/RL",
            run_level,
            "/F",
        ]
        proc = _run_cmd(cmd2)
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()

    if proc.returncode != 0:
        _log(f"[FAIL] Could not create task: {out}")
        return 1

    _log(f"[OK] Task '{TASK_NAME}' registered (run level {run_level}).")
    _log(f'  Verify: schtasks /Query /TN "{TASK_NAME}"')
    _log(f'  Remove: schtasks /Delete /TN "{TASK_NAME}" /F')
    _log(f"  Logs:   {LOG_PATH}")
    if logged_in_only:
        _log("  NOTE: logged-in-only - will not run while logged off.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Monday pre-market checklist (verify, reset, heartbeats, Telegram, health)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Emergency:\n"
            "  python scripts/recover_bot.py\n"
            "  Start_Autonomous.bat\n"
            "\n"
            "Examples:\n"
            "  python scripts/monday_checklist.py\n"
            "  python scripts/monday_checklist.py --quick --skip-reset\n"
            "  python scripts/monday_checklist.py --install-task --logged-in-only\n"
        ),
    )
    parser.add_argument("--quick", action="store_true", help="Faster verify (skip slow scanners)")
    parser.add_argument(
        "--skip-reset",
        action="store_true",
        help="Skip owner_reset.py (check running bots only)",
    )
    parser.add_argument(
        "--skip-wait",
        action="store_true",
        help="Do not wait for fresh heartbeats (use current age)",
    )
    parser.add_argument(
        "--wait-sec",
        type=int,
        default=90,
        help="Seconds to wait for fresh heartbeats after reset (default 90)",
    )
    parser.add_argument(
        "--no-telegram-send",
        action="store_true",
        help="Print /status only; do not send Telegram",
    )
    parser.add_argument("--username", default="", help="Portal username (default: last dashboard user)")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument(
        "--install-task",
        action="store_true",
        help="Register Monday premarket Task Scheduler job and exit",
    )
    parser.add_argument(
        "--logged-in-only",
        action="store_true",
        help="With --install-task: no stored password (runs only when logged in)",
    )
    parser.add_argument(
        "--password",
        default="",
        help="Windows password for --install-task (prefer interactive prompt)",
    )
    parser.add_argument(
        "--task-time",
        default=DEFAULT_TASK_TIME,
        help=f"Monday local start time for --install-task (default {DEFAULT_TASK_TIME})",
    )
    args = parser.parse_args()

    global _USE_COLOR
    if args.no_color or not sys.stdout.isatty():
        _USE_COLOR = False

    if args.install_task:
        password: str | None = None
        logged_in_only = bool(args.logged_in_only)
        if not logged_in_only:
            password = args.password.strip() or None
            if password is None:
                _log("Enter Windows password (hidden) for Task Scheduler, or Enter to skip:")
                entered = getpass.getpass("  Password: ")
                password = entered.strip() or None
            if password is None:
                _log("[INFO] No password - logged-in-only mode.")
                logged_in_only = True
        return install_task(
            logged_in_only=logged_in_only,
            password=password,
            task_time=args.task_time.strip() or DEFAULT_TASK_TIME,
        )

    username = (args.username or _default_username()).strip().lower()
    checks: list[Check] = []

    _log(_c("=" * 72, _BOLD))
    _log(_c("  PythonTrading - MONDAY CHECKLIST", _BOLD + _CYAN))
    _log(_c("=" * 72, _BOLD))
    _log(f"  Root:     {ROOT}")
    _log(f"  Python:   {_python()}")
    _log(f"  User:     {username}")
    _log(f"  Started:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        step_verify(checks, quick=bool(args.quick))
        step_owner_reset(checks, skip=bool(args.skip_reset), username=username)
        # After reset, allow a short settle before polling heartbeats
        if not args.skip_reset and not args.skip_wait:
            _log(_c("  Settling 15s after reset before heartbeat poll...", _DIM))
            time.sleep(15)
        step_books_responding(
            checks,
            username=username,
            wait_sec=int(args.wait_sec),
            skip_wait=bool(args.skip_wait) or bool(args.skip_reset),
        )
        step_telegram_status(
            checks,
            username=username,
            send=not bool(args.no_telegram_send),
        )
        step_paper_health_strategy(checks)
        step_during_market_reminder(checks)
        step_friday_weekly_path(checks)
    except KeyboardInterrupt:
        _log("\n[WARN] Interrupted by user")
        _record(checks, "Checklist", "FAIL", "interrupted")
    except Exception as exc:
        _log(f"\n[FAIL] Unhandled error: {exc}")
        _record(checks, "Checklist", "FAIL", str(exc)[:80])

    overall = print_summary(checks)
    print_emergency_help()
    return 1 if overall == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
