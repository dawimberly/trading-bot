"""Keep Windows from idle-sleeping while trading bots run.

Uses SetThreadExecutionState (ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE).
Does not block Start-menu Sleep / Shutdown / lid close. No trading side effects.
"""

from __future__ import annotations

import atexit
import logging
import sys
import time
from typing import Optional

logger = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040

_ARMED = False
_LAST_PULSE = 0.0
_MIN_PULSE_SEC = 30.0


def _kernel32():
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        return ctypes.windll.kernel32
    except Exception:
        return None


def arm(*, reason: str = "trading bot") -> bool:
    """Request continuous system-required state. Idempotent."""
    global _ARMED
    k32 = _kernel32()
    if k32 is None:
        return False
    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
    try:
        ok = bool(k32.SetThreadExecutionState(flags))
    except Exception as exc:
        logger.debug("system_awake arm failed: %s", exc)
        return False
    if ok and not _ARMED:
        _ARMED = True
        atexit.register(release)
        logger.info("system keep-awake ON (%s)", reason)
        print(f"--- System keep-awake: ON ({reason}) ---", flush=True)
        try:
            from modules.trade_journal import log_ops_event

            log_ops_event("keep_awake_on", notes=reason)
        except Exception:
            pass
    return ok


def pulse(*, min_interval_sec: float = _MIN_PULSE_SEC) -> bool:
    """Re-assert wake lock periodically (some Windows builds need refresh)."""
    global _LAST_PULSE
    if not _ARMED and not arm():
        return False
    now = time.monotonic()
    if now - _LAST_PULSE < max(5.0, float(min_interval_sec)):
        return True
    k32 = _kernel32()
    if k32 is None:
        return False
    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
    try:
        ok = bool(k32.SetThreadExecutionState(flags))
    except Exception:
        return False
    if ok:
        _LAST_PULSE = now
    return ok


def release() -> None:
    """Clear the wake request so idle sleep can resume after bots exit."""
    global _ARMED, _LAST_PULSE
    if not _ARMED:
        return
    k32 = _kernel32()
    if k32 is not None:
        try:
            k32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception:
            pass
    _ARMED = False
    _LAST_PULSE = 0.0
    logger.info("system keep-awake OFF")
