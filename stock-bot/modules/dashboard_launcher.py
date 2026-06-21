"""Launch dashboard_app.py / PythonTradingMonitor.exe from run_all (non-blocking)."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import config
from modules.runtime_paths import (
    DASHBOARD_SCRIPT,
    MONITOR_EXE_NAME,
    resolve_dashboard_executable,
    resolve_dashboard_script,
    dashboard_process_running,
    resolve_runtime_root,
)

logger = logging.getLogger(__name__)


def _resolve_pythonw(root: Path) -> str | None:
    """Return pythonw for dashboard_app.py; never return the frozen bot EXE."""
    for candidate in (
        root / ".venv" / "Scripts" / "pythonw.exe",
        root.parent / ".venv" / "Scripts" / "pythonw.exe",
    ):
        if candidate.is_file():
            return str(candidate)
    for name in ("pythonw.exe", "pythonw", "python.exe", "python"):
        found = shutil.which(name)
        if found and Path(found).resolve() != Path(sys.executable).resolve():
            return found
    if getattr(sys, "frozen", False):
        return None
    return sys.executable


def _dashboard_env(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONTRADING_ROOT"] = str(root)
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        env.pop(var, None)
    return env


def _append_launch_log(root: Path, message: str) -> None:
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with (log_dir / "dashboard_auto_launch.log").open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")


def _spawn_detached(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    log_file.write(
        f"\n--- dashboard auto-launch {datetime.now().isoformat(timespec='seconds')} ---\n"
    )
    log_file.write(f"cwd={cwd}\nargv={' '.join(argv)}\n")
    log_file.flush()
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=flags,
        close_fds=sys.platform != "win32",
    )


def try_launch_dashboard(*, force: bool = False) -> tuple[bool, str]:
    """
    Start the desktop monitor in a separate process.
    Does not pass --launch-bot (the bot is already running).
    """
    if not force and not config.AUTO_LAUNCH_DASHBOARD:
        return False, "AUTO_LAUNCH_DASHBOARD is disabled"

    root = resolve_runtime_root()
    if dashboard_process_running(root):
        msg = "Dashboard already running — skipped auto-launch"
        logger.info(msg)
        _append_launch_log(root, msg)
        return True, msg

    env = _dashboard_env(root)
    log_path = root / "logs" / "dashboard_auto_launch.log"

    monitor = resolve_dashboard_executable(root)
    if monitor is not None:
        try:
            proc = _spawn_detached(
                [str(monitor)],
                cwd=root,
                env=env,
                log_path=log_path,
            )
        except OSError as exc:
            msg = f"Failed to launch {monitor.name}: {exc}"
            logger.warning(msg)
            _append_launch_log(root, msg)
            return False, msg
        time.sleep(1.5)
        if proc.poll() is not None:
            msg = f"{monitor.name} exited immediately (code {proc.returncode})"
            logger.warning(msg)
            _append_launch_log(root, msg)
            return False, msg
        msg = f"Launched {monitor.name} (PID {proc.pid})"
        logger.info(msg)
        _append_launch_log(root, msg)
        return True, msg

    script = resolve_dashboard_script(root)
    if script is None:
        msg = (
            f"{DASHBOARD_SCRIPT} not found and no {MONITOR_EXE_NAME}; "
            "build the monitor with build_dashboard.bat or run from source"
        )
        logger.warning(msg)
        _append_launch_log(root, msg)
        return False, msg

    pyw = _resolve_pythonw(root)
    if pyw is None:
        msg = (
            f"No {MONITOR_EXE_NAME} and no pythonw found for {DASHBOARD_SCRIPT}; "
            "install Python/venv or run build_dashboard.bat"
        )
        logger.warning(msg)
        _append_launch_log(root, msg)
        return False, msg
    try:
        proc = _spawn_detached(
            [pyw, str(script)],
            cwd=root,
            env=env,
            log_path=log_path,
        )
    except OSError as exc:
        msg = f"Failed to launch {DASHBOARD_SCRIPT}: {exc}"
        logger.warning(msg)
        _append_launch_log(root, msg)
        return False, msg
    time.sleep(1.5)
    if proc.poll() is not None:
        msg = f"{DASHBOARD_SCRIPT} exited immediately (code {proc.returncode})"
        logger.warning(msg)
        _append_launch_log(root, msg)
        return False, msg
    msg = f"Launched {DASHBOARD_SCRIPT} via {Path(pyw).name} (PID {proc.pid})"
    logger.info(msg)
    _append_launch_log(root, msg)
    return True, msg


def maybe_launch_dashboard() -> None:
    """Fire-and-forget dashboard launch; never raises to caller."""
    try:
        ok, msg = try_launch_dashboard()
        if ok:
            print(f"--- Dashboard: {msg} ---")
        elif config.AUTO_LAUNCH_DASHBOARD:
            print(f"[WARN] Dashboard auto-launch: {msg}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Dashboard auto-launch failed (bot continues): %s", exc)
        print(f"[WARN] Dashboard auto-launch failed (bot continues): {exc}")
