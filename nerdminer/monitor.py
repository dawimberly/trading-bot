"""NerdMiner v2 serial monitor — parse COM output, persist state, optional alerts."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modules.safe_io import write_json_atomic
from nerdminer import config

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
STATS_RE = re.compile(
    r"\|\s*([\d.]+)\s*MH/s\s*\|\s*(\d+)/(\d+)\s*\|\s*([\d.]+T?)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+K?)\s*\|\s*(\d+)\s*\|\s*([\d.]+kB)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|"
)
VERSION_RE = re.compile(r"v([\d.]+)")
POOL_JOB_RE = re.compile(r"Job\s*\[(\d+)\]\s*from\s*\[([^\]]+)\]")
SHARE_ACCEPT_RE = re.compile(r"#(\d+)\s+share\s+accepted,\s*(\d+)ms", re.I)
SHARE_REJECT_RE = re.compile(r"#(\d+)\s+share\s+rejected", re.I)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text).strip()


def parse_line(line: str) -> dict[str, Any]:
    """Return partial state updates parsed from one serial line."""
    clean = strip_ansi(line)
    if not clean:
        return {}

    if "hash rate" in clean and "share(R/A)" in clean:
        return {}

    version_match = VERSION_RE.search(clean)
    if version_match and "----" in clean:
        return {"firmware": version_match.group(1)}

    stats_match = STATS_RE.search(clean)
    if stats_match:
        rejected = int(stats_match.group(2))
        accepted = int(stats_match.group(3))
        total = rejected + accepted
        reject_pct = (rejected / total * 100.0) if total else 0.0
        rssi_raw = stats_match.group(11).strip()
        rssi_dbm = None
        if rssi_raw.endswith("dBm"):
            try:
                rssi_dbm = int(rssi_raw.replace("dBm", ""))
            except ValueError:
                rssi_dbm = None
        temp_raw = stats_match.group(10).strip()
        return {
            "hash_rate_mhs": float(stats_match.group(1)),
            "shares_rejected": rejected,
            "shares_accepted": accepted,
            "reject_pct": round(reject_pct, 2),
            "net_diff": stats_match.group(4).strip(),
            "pool_diff": float(stats_match.group(5)),
            "last_diff": float(stats_match.group(6)),
            "best_diff": stats_match.group(7).strip(),
            "hits": int(stats_match.group(8)),
            "free_heap_kb": float(stats_match.group(9).replace("kB", "")),
            "temp_c": None if temp_raw.upper() == "N/A" else temp_raw,
            "rssi_dbm": rssi_dbm,
        }

    pool_match = POOL_JOB_RE.search(clean)
    if pool_match:
        return {
            "pool_job_id": pool_match.group(1),
            "pool": pool_match.group(2),
        }

    accept_match = SHARE_ACCEPT_RE.search(clean)
    if accept_match:
        return {
            "last_share_id": int(accept_match.group(1)),
            "last_share_ms": int(accept_match.group(2)),
            "last_share_status": "accepted",
        }

    if SHARE_REJECT_RE.search(clean):
        return {"last_share_status": "rejected"}

    return {}


def merge_state(state: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(state)
    merged.update(patch)
    return merged


def assess_health(state: dict[str, Any], *, stale_seconds: float) -> tuple[str, list[str]]:
    """Return (status, warnings) where status is ok | warning | offline."""
    warnings: list[str] = []
    updated_at = state.get("updated_at")
    if not updated_at:
        return "offline", ["No monitor data yet"]

    try:
        age = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        ).total_seconds()
    except ValueError:
        age = stale_seconds + 1

    if age > stale_seconds:
        return "offline", [f"No serial update for {int(age)}s — is the monitor running?"]

    hash_rate = float(state.get("hash_rate_mhs") or 0)
    if hash_rate <= 0:
        warnings.append("Hash rate is zero")
    elif hash_rate < 0.5:
        warnings.append(f"Hash rate low ({hash_rate:.3f} MH/s)")

    reject_pct = float(state.get("reject_pct") or 0)
    if reject_pct > 5:
        warnings.append(f"Reject rate elevated ({reject_pct:.1f}%)")

    rssi = state.get("rssi_dbm")
    if isinstance(rssi, int):
        if rssi <= -80:
            warnings.append(f"WiFi weak ({rssi} dBm) — move closer to router")
        elif rssi <= -75:
            warnings.append(f"WiFi marginal ({rssi} dBm)")

    share_ms = state.get("last_share_ms")
    if isinstance(share_ms, int) and share_ms > 250:
        warnings.append(f"High pool latency ({share_ms} ms)")

    if state.get("last_error"):
        warnings.append(str(state["last_error"]))

    status = "warning" if warnings else "ok"
    return status, warnings


def load_state(path: str | Path | None = None) -> dict[str, Any] | None:
    target = Path(path or config.STATE_FILE)
    if not target.is_file():
        return None
    try:
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def save_state(state: dict[str, Any], path: str | Path | None = None) -> None:
    target = str(path or config.STATE_FILE)
    try:
        write_json_atomic(target, state)
    except OSError:
        with open(target, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)


def append_history(snapshot: dict[str, Any], path: str | Path | None = None) -> None:
    target = Path(path or config.HISTORY_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, default=str) + "\n")


def detect_port(preferred: str | None = None) -> str | None:
    try:
        import serial.tools.list_ports
    except ImportError:
        return preferred

    ports = list(serial.tools.list_ports.comports())
    if preferred:
        if any(p.device == preferred for p in ports):
            return preferred

    for port in ports:
        desc = f"{port.description} {port.manufacturer or ''}".lower()
        if "ch340" in desc or "usb-serial" in desc or "wch" in desc:
            return port.device
    return preferred or (ports[0].device if ports else None)


def read_lines(
    port: str,
    *,
    baud: int | None = None,
    seconds: float = 8.0,
) -> list[str]:
    import serial

    ser = serial.Serial(port, baud or config.BAUD, timeout=1)
    try:
        time.sleep(0.2)
        ser.reset_input_buffer()
        lines: list[str] = []
        start = time.time()
        while time.time() - start < seconds:
            raw = ser.readline()
            if not raw:
                continue
            lines.append(strip_ansi(raw.decode("utf-8", errors="replace").rstrip()))
        return lines
    finally:
        ser.close()


def snapshot_from_serial(
    port: str | None = None,
    *,
    baud: int | None = None,
    seconds: float = 8.0,
) -> dict[str, Any]:
    """One-shot read: open COM port, parse lines, return state dict."""
    resolved = detect_port(port or config.SERIAL_PORT)
    if not resolved:
        raise RuntimeError("No serial port found (set NERDMINER_SERIAL_PORT)")

    state: dict[str, Any] = {
        "port": resolved,
        "baud": baud or config.BAUD,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        for line in read_lines(resolved, baud=baud, seconds=seconds):
            state = merge_state(state, parse_line(line))
        state["status"], state["warnings"] = assess_health(
            state, stale_seconds=config.STALE_SECONDS
        )
        state.pop("last_error", None)
    except Exception as exc:  # noqa: BLE001 — surface serial failures in state
        state["last_error"] = str(exc)
        state["status"] = "offline"
        state["warnings"] = [str(exc)]
    return state


def run_monitor(
    *,
    port: str | None = None,
    baud: int | None = None,
    poll_seconds: float | None = None,
    state_path: str | Path | None = None,
    history_path: str | Path | None = None,
    alert_on_offline: bool = True,
) -> None:
    """Blocking loop: read serial, write state/history, optional Telegram alerts."""
    import serial

    resolved = detect_port(port or config.SERIAL_PORT)
    if not resolved:
        raise RuntimeError("No serial port found (set NERDMINER_SERIAL_PORT)")

    poll = poll_seconds if poll_seconds is not None else config.POLL_SECONDS
    state_file = str(state_path or config.STATE_FILE)
    history_file = str(history_path or config.HISTORY_FILE)

    state: dict[str, Any] = load_state(state_file) or {}
    state.update({"port": resolved, "baud": baud or config.BAUD})
    last_alert_key: str | None = state.get("last_alert_key")
    last_history_at = 0.0

    ser = serial.Serial(resolved, baud or config.BAUD, timeout=1)
    try:
        time.sleep(0.3)
        ser.reset_input_buffer()
        while True:
            try:
                raw = ser.readline()
                if raw:
                    line = strip_ansi(raw.decode("utf-8", errors="replace").rstrip())
                    patch = parse_line(line)
                    if patch:
                        state = merge_state(state, patch)
                        state["updated_at"] = datetime.now(timezone.utc).isoformat()
                        state["port"] = resolved
                        status, warnings = assess_health(
                            state, stale_seconds=config.STALE_SECONDS
                        )
                        state["status"] = status
                        state["warnings"] = warnings
                        state.pop("last_error", None)
                        try:
                            save_state(state, state_file)
                        except OSError as exc:
                            state["last_error"] = f"state write failed: {exc}"

                        now = time.time()
                        if now - last_history_at >= poll and state.get("hash_rate_mhs") is not None:
                            append_history(
                                {
                                    "ts": state["updated_at"],
                                    "hash_rate_mhs": state.get("hash_rate_mhs"),
                                    "shares_accepted": state.get("shares_accepted"),
                                    "shares_rejected": state.get("shares_rejected"),
                                    "reject_pct": state.get("reject_pct"),
                                    "rssi_dbm": state.get("rssi_dbm"),
                                    "best_diff": state.get("best_diff"),
                                },
                                history_file,
                            )
                            last_history_at = now

                        if alert_on_offline and status != "ok":
                            alert_key = "|".join([status, *warnings])
                            if alert_key != last_alert_key:
                                _maybe_alert(state, status, warnings)
                                last_alert_key = alert_key
                                state["last_alert_key"] = alert_key
                                save_state(state, state_file)
                else:
                    time.sleep(0.05)
            except serial.SerialException as exc:
                state["last_error"] = str(exc)
                state["status"] = "offline"
                state["warnings"] = [str(exc)]
                state["updated_at"] = datetime.now(timezone.utc).isoformat()
                save_state(state, state_file)
                time.sleep(2.0)
    finally:
        ser.close()


def _maybe_alert(state: dict[str, Any], status: str, warnings: list[str]) -> None:
    if not config.ALERTS_ENABLED:
        return
    try:
        from modules.alerts import alerts_configured, send_telegram
    except ImportError:
        return
    if not alerts_configured():
        return

    hr = state.get("hash_rate_mhs")
    pool = state.get("pool") or "—"
    lines = [
        f"NerdMiner {status.upper()}",
        f"Port: {state.get('port', '—')}",
        f"Hash: {hr} MH/s" if hr is not None else "Hash: —",
        f"Pool: {pool}",
    ]
    if warnings:
        lines.append("Warnings: " + "; ".join(warnings))
    send_telegram("\n".join(lines))


def load_history(limit: int = 500, path: str | Path | None = None) -> list[dict[str, Any]]:
    target = Path(path or config.HISTORY_FILE)
    if not target.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with open(target, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows[-limit:]
