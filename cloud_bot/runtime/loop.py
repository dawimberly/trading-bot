"""24/7 trading loop — runs parent run_all.py with cloud paths and profile."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from logging import Logger
from pathlib import Path

from cloud_bot.config.profile import apply_best_paper_profile
from cloud_bot.config.settings import CloudSettings


def _python_executable(repo_root: Path) -> str:
    venv = repo_root / ".venv" / "Scripts" / "python.exe"
    if venv.is_file():
        return str(venv)
    venv_unix = repo_root / ".venv" / "bin" / "python"
    if venv_unix.is_file():
        return str(venv_unix)
    return sys.executable


def run_forever(settings: CloudSettings, logger: Logger) -> int:
    """Spawn run_all.py subprocess with cloud env; restart on exit."""
    if settings.dry_run:
        logger.warning("CLOUD_BOT_DRY_RUN=true — trading loop not started")
        print("DRY RUN: set CLOUD_BOT_DRY_RUN=false to start run_all.py")
        return 0

    if not settings.run_all_script.is_file():
        logger.error("run_all.py not found: %s", settings.run_all_script)
        return 1

    settings.pid_file.parent.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)

    env = apply_best_paper_profile(
        overrides={
            "HEARTBEAT_FILE": str(settings.heartbeat_file),
            "PAPER_JOURNAL_CSV": str(settings.journal_csv),
            "PYTHONUNBUFFERED": "1",
        }
    )
    for key, val in env.items():
        os.environ[key] = val

    logger.info(
        "cloud bot environment | profile=%s | heartbeat=%s | journal=%s | dry_run=%s",
        settings.profile,
        settings.heartbeat_file,
        settings.journal_csv,
        settings.dry_run,
    )

    with settings.pid_file.open("w", encoding="utf-8") as pid_handle:
        pid_handle.write(str(os.getpid()))

    shutdown_requested = False
    proc: subprocess.Popen | None = None

    def _handle_signal(signum, frame):
        nonlocal shutdown_requested
        shutdown_requested = True
        logger.info("received signal=%s, stopping loop", signum)
        if proc is not None and proc.poll() is None:
            proc.terminate()

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)

    logger.info(
        "cloud bot environment | profile=%s | heartbeat=%s | journal=%s | dry_run=%s",
        settings.profile,
        settings.heartbeat_file,
        settings.journal_csv,
        settings.dry_run,
    )

    python = _python_executable(settings.repo_root)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    restart_delay = int(os.getenv("CLOUD_BOT_RESTART_SEC", "30"))
    max_failures = int(os.getenv("CLOUD_BOT_MAX_RESTARTS", "20"))
    consecutive_failures = 0

    logger.info(
        "starting cloud bot loop | python=%s | cwd=%s | heartbeat=%s",
        python,
        settings.repo_root,
        settings.heartbeat_file,
    )
    print("--- Cloud Bot 24/7 loop (best paper profile) ---")
    print(f"--- Heartbeat: {settings.heartbeat_file} ---")
    print(f"--- Journal:   {settings.journal_csv} ---")
    print(f"--- Logs:      {settings.log_dir / 'cloud_bot.log'} ---")

    while True:
        if shutdown_requested:
            logger.info("shutdown requested before next restart; exiting")
            break
        try:
            proc = subprocess.Popen(
                [python, str(settings.run_all_script)],
                cwd=str(settings.repo_root),
                env=env,
                creationflags=flags,
            )
            code = proc.wait()
            if code == 0:
                consecutive_failures = 0
                delay = restart_delay
            else:
                consecutive_failures += 1
                delay = min(
                    restart_delay * (2 ** min(consecutive_failures - 1, 6)),
                    600,
                )
            logger.warning(
                "run_all.py exited code=%s; restart in %ss (failures=%s/%s)",
                code,
                delay,
                consecutive_failures,
                max_failures,
            )
            if shutdown_requested:
                break
            if consecutive_failures >= max_failures:
                logger.error("max consecutive failures reached; stopping cloud loop")
                break
            time.sleep(delay)
        except KeyboardInterrupt:
            logger.info("shutdown requested")
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
            break
        except Exception as exc:
            consecutive_failures += 1
            delay = min(
                restart_delay * (2 ** min(consecutive_failures - 1, 6)),
                600,
            )
            logger.error("%s: %s\n%s", type(exc).__name__, exc, traceback.format_exc())
            if shutdown_requested:
                break
            if consecutive_failures >= max_failures:
                logger.error("max consecutive failures reached; stopping cloud loop")
                break
            time.sleep(delay)
    settings.pid_file.unlink(missing_ok=True)
    return 1 if consecutive_failures >= max_failures else 0
