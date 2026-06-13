"""24/7 trading loop — runs parent run_all.py with cloud paths and profile."""

from __future__ import annotations

import os
import random
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
from cloud_bot.runtime.logging_setup import log_structured


def _python_executable(repo_root: Path) -> str:
    venv = repo_root / ".venv" / "Scripts" / "python.exe"
    if venv.is_file():
        return str(venv)
    venv_unix = repo_root / ".venv" / "bin" / "python"
    if venv_unix.is_file():
        return str(venv_unix)
    return sys.executable


def _graceful_stop(proc: subprocess.Popen | None, logger: Logger, *, timeout: int = 15) -> None:
    if proc is None or proc.poll() is not None:
        return
    log_structured(logger, "run_all_terminate", pid=proc.pid)
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
        log_structured(logger, "run_all_exit_clean", pid=proc.pid)
    except subprocess.TimeoutExpired:
        logger.warning("run_all.py did not exit in %ss; killing pid=%s", timeout, proc.pid)
        proc.kill()
        proc.wait(timeout=5)
        log_structured(logger, "run_all_killed", pid=proc.pid)


def _backoff_delay(base_sec: int, failure_count: int, *, cap_sec: int = 600) -> float:
    """Exponential backoff with small jitter (cap at cap_sec)."""
    if failure_count <= 0:
        return float(base_sec)
    exp = min(base_sec * (2 ** min(failure_count - 1, 6)), cap_sec)
    jitter = random.uniform(0, min(5.0, exp * 0.1))
    return exp + jitter


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def run_forever(
    settings: CloudSettings,
    logger: Logger,
    *,
    runtime_env: dict[str, str] | None = None,
) -> int:
    """Spawn run_all.py subprocess with cloud env; restart on exit with backoff."""
    if settings.dry_run:
        logger.warning("CLOUD_BOT_DRY_RUN=true — trading loop not started")
        print("DRY RUN: set CLOUD_BOT_DRY_RUN=false or use --run without --dry-run")
        return 0

    if not settings.run_all_script.is_file():
        logger.error("run_all.py not found: %s", settings.run_all_script)
        return 1

    settings.pid_file.parent.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)

    if settings.pid_file.exists():
        try:
            existing = int(settings.pid_file.read_text(encoding="utf-8").strip())
            if _pid_alive(existing) and existing != os.getpid():
                logger.error(
                    "Cloud bot already running (pid=%s). Use --stop first.",
                    existing,
                )
                return 1
        except ValueError:
            settings.pid_file.unlink(missing_ok=True)

    env = dict(runtime_env) if runtime_env else apply_best_paper_profile(
        overrides={
            "HEARTBEAT_FILE": str(settings.heartbeat_file),
            "PAPER_JOURNAL_CSV": str(settings.journal_csv),
            "STAT_ARB_BOOK_FILE": str(settings.data_dir / "stat_arb_open_book.json"),
            "PYTHONUNBUFFERED": "1",
        }
    )
    for key, val in env.items():
        os.environ[key] = val

    with settings.pid_file.open("w", encoding="utf-8") as pid_handle:
        pid_handle.write(str(os.getpid()))

    shutdown_requested = False
    proc: subprocess.Popen | None = None

    def _handle_signal(signum, _frame):
        nonlocal shutdown_requested
        shutdown_requested = True
        log_structured(logger, "signal_received", signum=signum)
        _graceful_stop(proc, logger)

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)

    python = _python_executable(settings.repo_root)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    restart_delay = int(os.getenv("CLOUD_BOT_RESTART_SEC", "30"))
    max_failures = int(os.getenv("CLOUD_BOT_MAX_RESTARTS", "20"))
    consecutive_failures = 0
    subprocess_log = settings.log_dir / "run_all_subprocess.log"

    log_structured(
        logger,
        "loop_start",
        python=python,
        cwd=str(settings.repo_root),
        heartbeat=str(settings.heartbeat_file),
        max_restarts=max_failures,
        base_restart_sec=restart_delay,
    )

    while True:
        if shutdown_requested:
            log_structured(logger, "loop_shutdown", reason="signal")
            break
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            with subprocess_log.open("a", encoding="utf-8") as sub_log:
                sub_log.write(f"\n--- run_all start {started_at} ---\n")
                sub_log.flush()
                proc = subprocess.Popen(
                    [python, str(settings.run_all_script)],
                    cwd=str(settings.repo_root),
                    env=env,
                    stdout=sub_log,
                    stderr=subprocess.STDOUT,
                    creationflags=flags,
                )
            log_structured(logger, "run_all_spawned", pid=proc.pid)
            code = proc.wait()
            if code == 0:
                consecutive_failures = 0
                delay = float(restart_delay)
                log_structured(logger, "run_all_exit_ok", pid=proc.pid, exit_code=code)
            else:
                consecutive_failures += 1
                delay = _backoff_delay(restart_delay, consecutive_failures)
                log_structured(
                    logger,
                    "run_all_exit_error",
                    pid=proc.pid,
                    exit_code=code,
                    failures=consecutive_failures,
                    max_failures=max_failures,
                    restart_in_sec=round(delay, 1),
                )
            if shutdown_requested:
                break
            if consecutive_failures >= max_failures:
                logger.error(
                    "Max consecutive failures (%s) reached; stopping supervisor",
                    max_failures,
                )
                log_structured(logger, "loop_max_restarts", failures=consecutive_failures)
                break
            if code != 0 or consecutive_failures == 0:
                logger.info("Restarting run_all.py in %.0fs", delay)
            time.sleep(delay)
        except KeyboardInterrupt:
            log_structured(logger, "loop_shutdown", reason="keyboard_interrupt")
            shutdown_requested = True
            _graceful_stop(proc, logger)
            break
        except Exception as exc:
            consecutive_failures += 1
            delay = _backoff_delay(restart_delay, consecutive_failures)
            logger.error("%s: %s\n%s", type(exc).__name__, exc, traceback.format_exc())
            log_structured(
                logger,
                "loop_exception",
                error=type(exc).__name__,
                failures=consecutive_failures,
                restart_in_sec=round(delay, 1),
            )
            if shutdown_requested:
                break
            if consecutive_failures >= max_failures:
                log_structured(logger, "loop_max_restarts", failures=consecutive_failures)
                break
            time.sleep(delay)

    settings.pid_file.unlink(missing_ok=True)
    return 1 if consecutive_failures >= max_failures else 0
