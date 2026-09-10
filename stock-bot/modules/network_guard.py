"""Light network recovery for trading supervisors (Windows).

If outbound connectivity is lost long enough, try a gentle Wi-Fi reconnect /
adapter bounce. No-ops when Ethernet (or any NIC) already has Internet.
Does not place orders or touch Alpaca keys.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from typing import Optional

logger = logging.getLogger(__name__)

_LAST_OK = 0.0
_LAST_CHECK = 0.0
_LAST_REPAIR = 0.0
_DOWN_SINCE: Optional[float] = None
_ARMED_LOGGED = False

# Defaults: check every 60s; repair after 90s down; at most one repair / 10 min.
_CHECK_SEC = float(os.getenv("NETWORK_GUARD_CHECK_SEC", "60") or 60)
_DOWN_SEC = float(os.getenv("NETWORK_GUARD_DOWN_SEC", "90") or 90)
_REPAIR_COOLDOWN_SEC = float(os.getenv("NETWORK_GUARD_REPAIR_COOLDOWN_SEC", "600") or 600)
_PROBE_HOST = (os.getenv("NETWORK_GUARD_PROBE_HOST") or "1.1.1.1").strip()
_ENABLED = os.getenv("NETWORK_GUARD_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)


def _run(cmd: list[str], *, timeout: float = 25.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
    )


def connectivity_ok() -> bool:
    """True if we can reach the probe host (ICMP) or Alpaca (TCP 443)."""
    if sys.platform == "win32":
        try:
            cp = _run(["ping", "-n", "1", "-w", "2000", _PROBE_HOST], timeout=8.0)
            if cp.returncode == 0:
                return True
        except Exception:
            pass
    # TCP fallback — Alpaca is what the bot actually needs (ICMP often blocked).
    try:
        import socket

        with socket.create_connection(("api.alpaca.markets", 443), timeout=4.0):
            return True
    except Exception:
        return False


def _wifi_profiles() -> list[str]:
    if sys.platform != "win32" or not shutil.which("netsh"):
        return []
    try:
        cp = _run(["netsh", "wlan", "show", "profiles"], timeout=15.0)
    except Exception:
        return []
    names: list[str] = []
    for line in (cp.stdout or "").splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        if "profile" in key.lower() and "all user" in key.lower():
            name = val.strip()
            if name:
                names.append(name)
    return names


def _wifi_interface_name() -> Optional[str]:
    if sys.platform != "win32" or not shutil.which("netsh"):
        return None
    try:
        cp = _run(["netsh", "wlan", "show", "interfaces"], timeout=15.0)
    except Exception:
        return None
    for line in (cp.stdout or "").splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        if key.strip().lower() == "name":
            name = val.strip()
            if name:
                return name
    return None


def _try_wifi_reconnect() -> str:
    """Best-effort Wi-Fi reconnect. Returns a short status string."""
    if sys.platform != "win32":
        return "skip_non_windows"
    iface = _wifi_interface_name()
    profiles = _wifi_profiles()
    if not iface:
        return "no_wifi_iface"
    if not profiles:
        return "no_wifi_profile"
    # Prefer currently-known first profile (usually the home SSID).
    ssid = profiles[0]
    try:
        cp = _run(
            ["netsh", "wlan", "connect", f"name={ssid}", f"interface={iface}"],
            timeout=30.0,
        )
        if cp.returncode == 0:
            return f"wlan_connect:{ssid}"
        # Bounce adapter via PowerShell (needs admin sometimes — soft-fail OK).
        bounce = _run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Disable-NetAdapter -Name '{iface}' -Confirm:$false; "
                f"Start-Sleep -Seconds 3; "
                f"Enable-NetAdapter -Name '{iface}' -Confirm:$false; "
                f"Start-Sleep -Seconds 5; "
                f"netsh wlan connect name=\"{ssid}\" interface=\"{iface}\"",
            ],
            timeout=60.0,
        )
        if bounce.returncode == 0:
            return f"wifi_bounce:{ssid}"
        return f"wifi_fail:{cp.returncode}"
    except Exception as exc:
        return f"wifi_exc:{exc}"


def pulse(*, force: bool = False) -> Optional[str]:
    """Periodic check from paper/live supervisors. Returns action label or None."""
    global _LAST_OK, _LAST_CHECK, _LAST_REPAIR, _DOWN_SINCE, _ARMED_LOGGED

    if not _ENABLED:
        return None
    if sys.platform != "win32":
        return None

    now = time.monotonic()
    if not force and (now - _LAST_CHECK) < max(15.0, _CHECK_SEC):
        return None
    _LAST_CHECK = now

    def _ops(kind: str, **fields: object) -> None:
        try:
            from modules.trade_journal import log_ops_event

            log_ops_event(kind, **fields)
        except Exception:
            pass

    if not _ARMED_LOGGED:
        _ARMED_LOGGED = True
        msg = (
            f"--- Network guard: ON (probe {_PROBE_HOST}, "
            f"repair after {_DOWN_SEC:.0f}s down) ---"
        )
        print(msg, flush=True)
        logger.info("network_guard armed probe=%s down_sec=%s", _PROBE_HOST, _DOWN_SEC)
        _ops("network_guard_on", probe=_PROBE_HOST, down_sec=int(_DOWN_SEC))

    if connectivity_ok():
        was_down = _DOWN_SINCE is not None
        _LAST_OK = now
        _DOWN_SINCE = None
        if was_down:
            print("--- Network guard: connectivity RESTORED ---", flush=True)
            logger.info("network_guard restored")
            _ops("network_restored")
        return None

    if _DOWN_SINCE is None:
        _DOWN_SINCE = now
        print("--- Network guard: connectivity LOST ---", flush=True)
        logger.warning("network_guard connectivity lost")
        _ops("network_lost")
        return "lost"

    down_for = now - _DOWN_SINCE
    if down_for < _DOWN_SEC:
        return "down"
    if (now - _LAST_REPAIR) < _REPAIR_COOLDOWN_SEC:
        return "cooldown"

    _LAST_REPAIR = now
    action = _try_wifi_reconnect()
    print(f"--- Network guard: repair attempt ({action}) ---", flush=True)
    logger.warning("network_guard repair %s (down_for=%.0fs)", action, down_for)
    _ops("network_repair", notes=action, down_sec=int(down_for))
    time.sleep(5.0)
    if connectivity_ok():
        _LAST_OK = time.monotonic()
        _DOWN_SINCE = None
        print("--- Network guard: connectivity RESTORED ---", flush=True)
        logger.info("network_guard restored after %s", action)
        _ops("network_restored", notes=action)
        return f"restored:{action}"
    return f"still_down:{action}"
