"""Central logging helpers for the main project."""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any


def _add_daily_handler(
    root: logging.Logger,
    log_path: Path,
    fmt: logging.Formatter,
    *,
    backup_days: int = 7,
) -> None:
    """Attach a midnight-rotating file handler (keeps backup_days of history)."""
    fh = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        interval=1,
        backupCount=max(0, backup_days - 1),
        encoding="utf-8",
        utc=False,
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
