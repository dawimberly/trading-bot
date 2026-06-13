"""Structured logging for 24/7 cloud deployment."""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def setup_logging(log_dir: Path, *, name: str = "cloud_bot") -> logging.Logger:
    """Console + daily-rotating file log (14 days)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "cloud_bot.log"

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=13,
        encoding="utf-8",
        utc=False,
    )
    fh.suffix = "%Y-%m-%d"
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger.info("logging initialized -> %s (daily rotation, 14 days)", log_file)
    return logger


def log_structured(logger: logging.Logger, event: str, /, **fields) -> None:
    """Emit a single-line structured event for grep/journald parsing."""
    if not fields:
        logger.info("event=%s", event)
        return
    parts = " ".join(f"{k}={v!r}" for k, v in sorted(fields.items()))
    logger.info("event=%s %s", event, parts)
