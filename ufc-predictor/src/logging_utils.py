"""Central logging helpers — rotating file + console (ported from trading bot)."""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

import config


def setup_logging(
    *,
    verbose: bool = False,
    log_dir: Path | str | None = None,
    log_name: str = "ufc_bot.log",
) -> logging.Logger:
    """Configure root logger with console + optional daily-rotating file (7 days)."""
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    out_dir = Path(log_dir) if log_dir else config.LOG_DIR
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        fh = TimedRotatingFileHandler(
            out_dir / log_name,
            when="midnight",
            interval=1,
            backupCount=6,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        root.exception("Failed to create log file handler at %s", out_dir)

    logging.getLogger("optuna").setLevel(logging.WARNING)
    root.debug("logging initialized (level=%s)", logging.getLevelName(level))
    return root


def log_event(name: str, /, **data: Any) -> None:
    """Emit a structured event to the events logger."""
    parts = " ".join(f"{k}={v}" for k, v in data.items())
    logging.getLogger("events").info("%s %s", name, parts)
