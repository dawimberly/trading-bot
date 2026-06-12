"""Central logging helpers for the main project."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler
from typing import Any


def setup_logging(log_dir: Path | str | None = None, *, level: int = logging.INFO) -> logging.Logger:
    """Configure root logger. If log_dir provided, add a daily-rotating file handler there (keep 7 days).

    Returns the root logger instance.
    """
    root = logging.getLogger()
    root.setLevel(level)
    # Clear existing handlers to avoid duplicate logs when reloading
    root.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    if log_dir:
        try:
            p = Path(log_dir)
            p.mkdir(parents=True, exist_ok=True)
            # Daily rotation: rotate at midnight, keep 7 days (backupCount=6 means 7 total: current + 6 backups)
            fh = TimedRotatingFileHandler(
                p / "run_all.log",
                when="midnight",
                interval=1,
                backupCount=6,
                encoding="utf-8",
                utc=False,
            )
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except Exception:
            root.exception("Failed to create log file handler at %s", log_dir)

    root.info("logging initialized")
    return root


def log_event(name: str, /, **data: Any) -> None:
    """Emit a simple structured event to the `events` logger.

    Example: log_event("order_submitted", symbol="AAPL", side="buy", notional=250)
    """
    logger = logging.getLogger("events")
    logger.info(name, extra={"event": name, **data})
