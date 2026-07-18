"""Central logging helpers for the main project."""

from __future__ import annotations

import logging
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from modules.safe_io import ensure_stdio_streams


class _RetryTimedRotatingFileHandler(TimedRotatingFileHandler):
    """Midnight rotation with retries for Windows file-lock failures.

    On Windows, ``TimedRotatingFileHandler.doRollover()`` renames the active log
    file while it may still be open elsewhere (same process stream flush timing,
    antivirus scanners, ``Get-Content -Wait``, another Python worker). That
    raises ``PermissionError`` / WinError 32 (sharing violation) and can spam
    tracebacks on every subsequent emit.

    ``delay=True`` defers opening the file until the first record. Rollover
    retries use exponential backoff; if rotation still fails, we defer the next
    attempt and keep writing to the current file (safe on Linux/macOS too).
    """

    _MAX_ROLLOVER_ATTEMPTS = 5
    _ROLLOVER_BACKOFF_BASE_SEC = 0.1

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("delay", True)
        super().__init__(*args, **kwargs)
        self._rollover_failure_logged = False

    @staticmethod
    def _is_retryable_rollover_error(exc: BaseException) -> bool:
        if isinstance(exc, PermissionError):
            return True
        if isinstance(exc, OSError):
            winerror = getattr(exc, "winerror", None)
            if sys.platform == "win32" and winerror in (5, 32):
                # 5 = access denied, 32 = sharing violation (file in use)
                return True
            if exc.errno in (13, 16):
                # EACCES / EBUSY on Unix
                return True
        return False

    def _defer_next_rollover(self) -> None:
        """Push rolloverAt forward so a failed rotation is not retried every emit."""
        current_time = int(time.time())
        next_at = self.computeRollover(current_time)
        while next_at <= current_time:
            next_at += self.interval
        self.rolloverAt = next_at

    def _log_rollover_failure_once(self, exc: BaseException) -> None:
        if self._rollover_failure_logged:
            return
        self._rollover_failure_logged = True
        try:
            sys.stderr.write(
                f"WARNING: log rollover skipped for {self.baseFilename!r} "
                f"after {self._MAX_ROLLOVER_ATTEMPTS} attempts: {exc}\n"
            )
            sys.stderr.flush()
        except OSError:
            pass

    def doRollover(self) -> None:
        last_exc: BaseException | None = None
        for attempt in range(self._MAX_ROLLOVER_ATTEMPTS):
            try:
                super().doRollover()
                self._rollover_failure_logged = False
                return
            except (PermissionError, OSError) as exc:
                if not self._is_retryable_rollover_error(exc):
                    raise
                last_exc = exc
                if attempt < self._MAX_ROLLOVER_ATTEMPTS - 1:
                    time.sleep(self._ROLLOVER_BACKOFF_BASE_SEC * (2**attempt))

        if last_exc is not None:
            self._log_rollover_failure_once(last_exc)
            self._defer_next_rollover()
            if self.stream is None:
                self.stream = self._open()


def _add_daily_handler(
    root: logging.Logger,
    log_path: Path,
    fmt: logging.Formatter,
    *,
    backup_days: int = 7,
) -> None:
    """Attach a midnight-rotating file handler (keeps backup_days of history)."""
    fh = _RetryTimedRotatingFileHandler(
        log_path,
        when="midnight",
        interval=1,
        backupCount=max(0, backup_days - 1),
        encoding="utf-8",
        utc=False,
        delay=True,
    )
    fh.suffix = "%Y-%m-%d"
    fh.setFormatter(fmt)
    root.addHandler(fh)


def setup_logging(
    log_dir: Path | str | None = None,
    *,
    level: int = logging.INFO,
    backup_days: int = 7,
) -> logging.Logger:
    """Configure root logger with stdout + optional daily-rotating file logs.

    When log_dir is set, writes:
      - run_all.log  (all loggers)
      - events.log   (structured events via log_event)
    """
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ensure_stdio_streams()
    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    if log_dir:
        try:
            p = Path(log_dir)
            p.mkdir(parents=True, exist_ok=True)
            _add_daily_handler(root, p / "run_all.log", fmt, backup_days=backup_days)
            events_logger = logging.getLogger("events")
            events_logger.setLevel(level)
            events_logger.propagate = False
            events_logger.handlers.clear()
            _add_daily_handler(events_logger, p / "events.log", fmt, backup_days=backup_days)
        except Exception:
            root.exception("Failed to create log file handlers at %s", log_dir)

    root.info("logging initialized (daily rotation, %s days)", backup_days)
    return root


def setup_project_logging(
    *,
    level: int = logging.INFO,
    backup_days: int = 7,
) -> logging.Logger:
    """Project default: stdout + logs/run_all.log and logs/events.log."""
    return setup_logging(log_dir=Path("logs"), level=level, backup_days=backup_days)


def log_event(name: str, /, **data: Any) -> None:
    """Emit a simple structured event to the `events` logger.

    Example: log_event("order_submitted", symbol="AAPL", side="buy", notional=250)
    """
    logger = logging.getLogger("events")
    if data:
        parts = " ".join(f"{k}={v!r}" for k, v in sorted(data.items()))
        logger.info("event=%s %s", name, parts)
    else:
        logger.info("event=%s", name)


def log_subsystem_warning(
    subsystem: str,
    message: str,
    exc: BaseException | None = None,
) -> None:
    """Non-fatal subsystem warning with optional exception + structured event."""
    log = logging.getLogger(subsystem)
    if exc is not None:
        log.warning("%s: %s", message, exc, exc_info=True)
        log_event(f"{subsystem}_warn", message=message, error=str(exc))
    else:
        log.warning(message)
        log_event(f"{subsystem}_warn", message=message)


def log_subsystem_error(
    subsystem: str,
    message: str,
    exc: BaseException | None = None,
) -> None:
    """Subsystem error with optional exception + structured event."""
    log = logging.getLogger(subsystem)
    if exc is not None:
        log.error("%s: %s", message, exc, exc_info=True)
        log_event(f"{subsystem}_error", message=message, error=str(exc))
    else:
        log.error(message)
        log_event(f"{subsystem}_error", message=message)
