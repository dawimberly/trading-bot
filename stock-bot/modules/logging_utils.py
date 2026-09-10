"""Central logging helpers for the main project."""

from __future__ import annotations

import logging
import sys
import time
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from modules.safe_io import ensure_stdio_streams

# Hard cap so Windows lock failures cannot grow a single log to tens of GB again.
DEFAULT_LOG_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
DEFAULT_LOG_BACKUP_COUNT = 3  # active + 3 backups ≈ 200 MB per log name


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


class _RetryTimedRotatingFileHandler(TimedRotatingFileHandler):
    """Midnight rotation with retries for Windows file-lock failures.

    Kept for tests / callers that still construct it directly. Production
    logging uses size-based ``_RetryRotatingFileHandler`` so a failed
    midnight rename cannot leave a multi-GB file growing forever.
    """

    _MAX_ROLLOVER_ATTEMPTS = 5
    _ROLLOVER_BACKOFF_BASE_SEC = 0.1

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("delay", True)
        super().__init__(*args, **kwargs)
        self._rollover_failure_logged = False

    @staticmethod
    def _is_retryable_rollover_error(exc: BaseException) -> bool:
        return _is_retryable_rollover_error(exc)

    def _defer_next_rollover(self) -> None:
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
                if not _is_retryable_rollover_error(exc):
                    raise
                last_exc = exc
                if attempt < self._MAX_ROLLOVER_ATTEMPTS - 1:
                    time.sleep(self._ROLLOVER_BACKOFF_BASE_SEC * (2**attempt))

        if last_exc is not None:
            self._log_rollover_failure_once(last_exc)
            self._defer_next_rollover()
            if self.stream is None:
                self.stream = self._open()


class _RetryRotatingFileHandler(RotatingFileHandler):
    """Size-capped rotation with Windows lock retries + emergency truncate.

    If rename-based rollover keeps failing (file locked), truncate the active
    file when it exceeds 2× maxBytes so the disk cannot fill again.
    """

    _MAX_ROLLOVER_ATTEMPTS = 5
    _ROLLOVER_BACKOFF_BASE_SEC = 0.1

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("delay", True)
        super().__init__(*args, **kwargs)
        self._rollover_failure_logged = False
        self._truncate_failure_logged = False

    @staticmethod
    def _is_retryable_rollover_error(exc: BaseException) -> bool:
        return _is_retryable_rollover_error(exc)

    def _log_rollover_failure_once(self, exc: BaseException) -> None:
        if self._rollover_failure_logged:
            return
        self._rollover_failure_logged = True
        try:
            sys.stderr.write(
                f"WARNING: size log rollover skipped for {self.baseFilename!r} "
                f"after {self._MAX_ROLLOVER_ATTEMPTS} attempts: {exc}\n"
            )
            sys.stderr.flush()
        except OSError:
            pass

    def _emergency_truncate(self) -> None:
        """Last resort when Windows holds the file open through rollover."""
        path = Path(self.baseFilename)
        try:
            size = path.stat().st_size if path.is_file() else 0
        except OSError:
            size = 0
        if size < max(self.maxBytes * 2, self.maxBytes + 1):
            return
        try:
            if self.stream:
                try:
                    self.stream.close()
                except OSError:
                    pass
                self.stream = None
            with open(self.baseFilename, "w", encoding=self.encoding or "utf-8"):
                pass
            self.stream = self._open()
            try:
                sys.stderr.write(
                    f"WARNING: truncated oversized log {self.baseFilename!r} "
                    f"({size} bytes) after failed rollover\n"
                )
                sys.stderr.flush()
            except OSError:
                pass
        except OSError as exc:
            if not self._truncate_failure_logged:
                self._truncate_failure_logged = True
                try:
                    sys.stderr.write(
                        f"WARNING: could not truncate {self.baseFilename!r}: {exc}\n"
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
                if not _is_retryable_rollover_error(exc):
                    raise
                last_exc = exc
                if attempt < self._MAX_ROLLOVER_ATTEMPTS - 1:
                    time.sleep(self._ROLLOVER_BACKOFF_BASE_SEC * (2**attempt))

        if last_exc is not None:
            self._log_rollover_failure_once(last_exc)
            self._emergency_truncate()
            if self.stream is None:
                self.stream = self._open()


class _YfinanceNoiseFilter(logging.Filter):
    """Downgrade yfinance rate-limit/delisted noise and throttle repeats."""

    _last_logged: dict[str, float] = {}
    _interval_sec = 300.0

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.name.startswith("yfinance"):
            return True
        msg = record.getMessage().lower()
        noisy = (
            "rate limit" in msg
            or "possibly delisted" in msg
            or "no data found" in msg
            or "failed download" in msg
        )
        if not noisy:
            return True
        if record.levelno >= logging.ERROR:
            record.levelno = logging.INFO
            record.levelname = "INFO"
        key = record.name + ":" + msg[:96]
        now = time.monotonic()
        last = self._last_logged.get(key)
        if last is not None and (now - last) < self._interval_sec:
            return False
        self._last_logged[key] = now
        return True


def _add_rotating_handler(
    root: logging.Logger,
    log_path: Path,
    fmt: logging.Formatter,
    *,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> None:
    """Attach a size-capped rotating file handler."""
    fh = _RetryRotatingFileHandler(
        log_path,
        maxBytes=max(1024 * 1024, int(max_bytes)),
        backupCount=max(1, int(backup_count)),
        encoding="utf-8",
        delay=True,
    )
    fh.setFormatter(fmt)
    fh.addFilter(_YfinanceNoiseFilter())
    root.addHandler(fh)


def setup_logging(
    log_dir: Path | str | None = None,
    *,
    level: int = logging.INFO,
    backup_days: int = 7,
    run_log_names: list[str] | None = None,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> logging.Logger:
    """Configure root logger with stdout + size-capped rotating file logs.

    ``backup_days`` is accepted for call-site compatibility but unused; rotation
    is size-based so Windows file locks cannot grow a single log without bound.

    When log_dir is set, writes:
      - run_all.log (and/or book-specific run_all_live.log / run_all_paper.log)
      - events.log   (structured events via log_event)
    """
    del backup_days  # size-based rotation replaces daily retention
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
        sh.addFilter(_YfinanceNoiseFilter())
        root.addHandler(sh)

    for yf_logger in ("yfinance", "yfinance.scrapers", "yfinance.scrapers.quote"):
        logging.getLogger(yf_logger).setLevel(logging.WARNING)

    if log_dir:
        try:
            p = Path(log_dir)
            p.mkdir(parents=True, exist_ok=True)
            names = list(run_log_names) if run_log_names else ["run_all.log"]
            # Always keep combined fallback for backward compatibility.
            if "run_all.log" not in names:
                names.append("run_all.log")
            seen: set[str] = set()
            for name in names:
                key = str(name).strip() or "run_all.log"
                if key in seen:
                    continue
                seen.add(key)
                _add_rotating_handler(
                    root,
                    p / key,
                    fmt,
                    max_bytes=max_bytes,
                    backup_count=backup_count,
                )
            events_logger = logging.getLogger("events")
            events_logger.setLevel(level)
            events_logger.propagate = False
            events_logger.handlers.clear()
            _add_rotating_handler(
                events_logger,
                p / "events.log",
                fmt,
                max_bytes=max_bytes,
                backup_count=backup_count,
            )
        except Exception:
            root.exception("Failed to create log file handlers at %s", log_dir)

    root.info(
        "logging initialized (size rotation, max %s MB x %s backups)",
        max(1, int(max_bytes) // (1024 * 1024)),
        max(1, int(backup_count)),
    )
    return root


def setup_project_logging(
    *,
    level: int = logging.INFO,
    backup_days: int = 7,
    book: str | None = None,
) -> logging.Logger:
    """Project default: stdout + book log + combined logs/run_all.log + events.log.

    book:
      - ``\"live\"`` → logs/run_all_live.log (+ run_all.log)
      - ``\"paper\"`` → logs/run_all_paper.log (+ run_all.log)
      - None → logs/run_all.log only
    """
    book_key = (book or "").strip().lower()
    if book_key == "live":
        names = ["run_all_live.log", "run_all.log"]
    elif book_key == "paper":
        names = ["run_all_paper.log", "run_all.log"]
    else:
        names = ["run_all.log"]
    return setup_logging(
        log_dir=Path("logs"),
        level=level,
        backup_days=backup_days,
        run_log_names=names,
    )


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
