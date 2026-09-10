"""Tests for logging_utils rollover retry handler."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest import mock

from modules.logging_utils import (
    _RetryRotatingFileHandler,
    _RetryTimedRotatingFileHandler,
    log_event,
    setup_logging,
)


def test_retryable_rollover_error_windows_permission():
    assert _RetryTimedRotatingFileHandler._is_retryable_rollover_error(PermissionError())
    assert _RetryRotatingFileHandler._is_retryable_rollover_error(PermissionError())


def test_retryable_rollover_error_winerror_32():
    exc = OSError("in use")
    exc.winerror = 32  # type: ignore[attr-defined]
    assert _RetryTimedRotatingFileHandler._is_retryable_rollover_error(exc)
    assert _RetryRotatingFileHandler._is_retryable_rollover_error(exc)


def test_non_retryable_oserror():
    exc = OSError("disk full")
    exc.errno = 28
    assert not _RetryTimedRotatingFileHandler._is_retryable_rollover_error(exc)
    assert not _RetryRotatingFileHandler._is_retryable_rollover_error(exc)


def test_doRollover_retries_on_permission_error(tmp_path: Path):
    log_file = tmp_path / "events.log"
    handler = _RetryTimedRotatingFileHandler(
        log_file,
        when="S",
        interval=1,
        backupCount=1,
        encoding="utf-8",
        delay=True,
    )
    handler.emit(logging.LogRecord("t", logging.INFO, "", 0, "line", (), None))

    calls = {"n": 0}

    def flaky_rollover(self):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("locked")

    with mock.patch.object(
        logging.handlers.TimedRotatingFileHandler,
        "doRollover",
        flaky_rollover,
    ):
        handler.doRollover()

    assert calls["n"] == 3


def test_doRollover_defers_after_exhausted_retries(tmp_path: Path):
    log_file = tmp_path / "events.log"
    handler = _RetryTimedRotatingFileHandler(
        log_file,
        when="S",
        interval=1,
        backupCount=1,
        encoding="utf-8",
        delay=True,
    )
    handler.emit(logging.LogRecord("t", logging.INFO, "", 0, "line", (), None))
    before = handler.rolloverAt

    with mock.patch.object(
        logging.handlers.TimedRotatingFileHandler,
        "doRollover",
        side_effect=PermissionError("locked"),
    ):
        handler.doRollover()

    assert handler.rolloverAt >= before
    handler.emit(logging.LogRecord("t", logging.INFO, "", 0, "still logging", (), None))


def test_size_handler_emergency_truncate_on_failed_rollover(tmp_path: Path):
    log_file = tmp_path / "run_all.log"
    handler = _RetryRotatingFileHandler(
        log_file,
        maxBytes=1024,
        backupCount=1,
        encoding="utf-8",
        delay=True,
    )
    # Pretend the file already blew past 2x maxBytes while locked.
    log_file.write_bytes(b"x" * 5000)
    with mock.patch.object(
        logging.handlers.RotatingFileHandler,
        "doRollover",
        side_effect=PermissionError("locked"),
    ):
        handler.doRollover()
    assert log_file.stat().st_size == 0


def test_setup_logging_creates_size_handlers(tmp_path: Path):
    root = setup_logging(log_dir=tmp_path, backup_days=3, max_bytes=1024 * 1024, backup_count=2)
    assert any(isinstance(h, _RetryRotatingFileHandler) for h in root.handlers)
    events = logging.getLogger("events")
    assert any(isinstance(h, _RetryRotatingFileHandler) for h in events.handlers)
    log_event("setup_smoke")
    for h in root.handlers + events.handlers:
        if isinstance(h, _RetryRotatingFileHandler):
            assert h.delay is True
            assert h.maxBytes == 1024 * 1024
            assert h.backupCount == 2
