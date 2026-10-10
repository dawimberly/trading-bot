"""
Keep Windows from sleeping while the exhaustive campaign is active.

Uses SetThreadExecutionState (ES_CONTINUOUS | ES_SYSTEM_REQUIRED).
Watchdog starts this detached when a campaign is running; exits when
campaign_state.json status is completed/halted or --max-hours elapses.

Freeze-safe: no trading side effects.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
STATE_PATH = RUNS / "campaign_state.json"
PID_PATH = RUNS / "keep_awake.pid"
LOG_PATH = RUNS / "keep_awake.log"

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040


def _log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _state_status() -> str:
    if not STATE_PATH.is_file():
        return ""
    try:
        return str(json.loads(STATE_PATH.read_text(encoding="utf-8")).get("status") or "")
    except Exception:
        return ""


def _campaign_alive() -> bool:
    """True if any campaign-related python is up (best-effort)."""
    try:
        import subprocess

        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-WindowStyle",
                "Hidden",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -match "
                "'run_campaign|compare-final|monte_carlo|walk_forward|"
                "full_strategy|chaotic_backtest|eval_strict|backtest_intraday|"
                "crypto_vol_v5|run_tod' } | "
                "Select-Object -First 1 -ExpandProperty ProcessId",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=30,
            creationflags=creationflags,
        )
        return bool((out or "").strip())
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-hours", type=float, default=36.0)
    ap.add_argument("--interval-sec", type=float, default=60.0)
    args = ap.parse_args()

    if sys.platform != "win32":
        _log("not Windows — noop")
        return 0

    PID_PATH.write_text(f"{__import__('os').getpid()}\n", encoding="utf-8")
    kernel32 = ctypes.windll.kernel32
    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
    deadline = time.time() + max(1.0, float(args.max_hours)) * 3600.0
    _log(f"keep-awake START max_hours={args.max_hours}")

    try:
        while time.time() < deadline:
            status = _state_status()
            if status in ("completed", "halted"):
                _log(f"stop: state={status}")
                break
            if status != "running" and not _campaign_alive():
                # brief grace: if neither state nor procs, exit
                time.sleep(5)
                if _state_status() not in ("running", "") and not _campaign_alive():
                    if _state_status() in ("completed", "halted"):
                        break
                if not _campaign_alive() and _state_status() not in ("running", ""):
                    _log(f"stop: no campaign (state={_state_status() or 'n/a'})")
                    break
            rc = kernel32.SetThreadExecutionState(flags)
            if not rc:
                _log("WARNING SetThreadExecutionState returned 0")
            time.sleep(max(15.0, float(args.interval_sec)))
    finally:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        try:
            PID_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        _log("keep-awake END")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
