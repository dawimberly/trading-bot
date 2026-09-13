"""Cycle watchdog — force-exit a stuck run_all loop so the supervisor restarts it.

The paper supervisor (``run_paper_bot.py``) restarts ``run_all.py`` when it exits
with ``EXIT_WATCHDOG``, giving graceful auto-recovery from a hung cycle (e.g. a
network call with no timeout). A fresh loop starts within seconds, so Telegram
commands and the heartbeat stay responsive.

Usage (run_all.py):
    wd = CycleWatchdog(timeout_sec=90)
    wd.start()
    while True:
        wd.begin_cycle("main")
        try:
            main()
        finally:
            wd.end_cycle()
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

# Distinct exit code so the supervisor can tell a watchdog restart apart from a
# clean exit (0) or an auth/fatal failure (1).
EXIT_WATCHDOG = 42


def watchdog_enabled(default: bool = True) -> bool:
    raw = os.getenv("HEARTBEAT_WATCHDOG_ENABLED")
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def watchdog_timeout_sec(default: float = 90.0) -> float:
    raw = os.getenv("HEARTBEAT_WATCHDOG_TIMEOUT_SEC")
    if not raw:
        # Full research / live cycles routinely exceed 90–300s (data refresh,
        # universe, thinking). Too-low timeouts cause exit-42 thrash with no
        # completed cycle — paper had a supervisor so it "survived"; live did not.
        paperish = (
            os.getenv("PAPER_TRADING", "").strip().lower() in ("1", "true", "yes", "on")
            or os.getenv("PAPER_CHASE_MODE", "").strip().lower() in ("1", "true", "yes", "on")
        )
        return 900.0 if paperish else 600.0
    try:
        return max(30.0, float(raw))
    except ValueError:
        return default


class CycleWatchdog:
    """Background thread that force-exits the process when a cycle overruns.

    Only an *active* cycle is timed; the idle sleep between cycles is not, so a
    normal long ``time.sleep`` between crypto-only cycles never trips it.
    """

    def __init__(
        self,
        timeout_sec: float = 90.0,
        poll_sec: float = 5.0,
        on_timeout: Callable[[str, float], None] | None = None,
    ) -> None:
        self.timeout_sec = max(30.0, float(timeout_sec))
        self.poll_sec = max(1.0, float(poll_sec))
        self._on_timeout = on_timeout or self._default_on_timeout
        self._lock = threading.Lock()
        self._deadline: float | None = None
        self._label = ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="cycle-watchdog", daemon=True
        )
        self._thread.start()
        logger.info(
            "Cycle watchdog armed (timeout %.0fs, poll %.0fs)",
            self.timeout_sec,
            self.poll_sec,
        )

    def begin_cycle(self, label: str = "main") -> None:
        with self._lock:
            self._deadline = time.monotonic() + self.timeout_sec
            self._label = label

    def end_cycle(self) -> None:
        with self._lock:
            self._deadline = None
            self._label = ""

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.poll_sec):
            with self._lock:
                deadline = self._deadline
                label = self._label
            if deadline is not None and time.monotonic() > deadline:
                # on_timeout normally os._exit()s; if a custom callback returns,
                # stop watching so we don't fire repeatedly.
                self._on_timeout(label, self.timeout_sec)
                return

    @staticmethod
    def _default_on_timeout(label: str, timeout_sec: float) -> None:
        logger.critical(
            "Watchdog: cycle '%s' stuck >%.0fs — forcing restart (exit %d)",
            label or "main",
            timeout_sec,
            EXIT_WATCHDOG,
        )
        try:
            logging.shutdown()
        finally:
            # os._exit skips atexit/finally in the hung thread — the only reliable
            # way to abandon a wedged cycle. The supervisor restarts us.
            os._exit(EXIT_WATCHDOG)
