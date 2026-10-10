"""
Campaign watchdog — keeps the exhaustive research campaign alive.

Every run (via Task Scheduler every 5 minutes):
  1. If campaign heartbeat fresh OR run_campaign/compare-final alive -> OK
  2. If state says completed -> OK (noop)
  3. If dead/stale and not completed -> detach-restart with --resume

Freeze-safe: research only. Never touches paper/live .env.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
RUNS.mkdir(parents=True, exist_ok=True)


def _find_stock_bot_root() -> Path:
    for cand in (HERE.parents[i] for i in range(1, 8)):
        if (cand / "backtester.py").is_file() and (cand / "modules").is_dir():
            return cand
    raise RuntimeError(f"Could not locate stock-bot root from {HERE}")


ROOT = _find_stock_bot_root()

STATE_PATH = RUNS / "campaign_state.json"
HEARTBEAT_PATH = RUNS / "campaign_heartbeat.json"
PID_PATH = RUNS / "campaign.pid"
WATCH_LOG = RUNS / "watchdog.log"
STATUS_MD = RUNS / "STATUS.md"

STALE_HEARTBEAT_SEC = 180  # 3 minutes without beat => suspicious
PY = None
for cand in (
    ROOT.parent / "venv311" / "Scripts" / "python.exe",
    ROOT.parent / ".venv" / "Scripts" / "python.exe",
    ROOT / ".venv" / "Scripts" / "python.exe",
):
    if cand.is_file():
        PY = str(cand)
        break
if PY is None:
    PY = sys.executable


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _log(msg: str) -> None:
    line = f"[{_utc()}] {msg}"
    print(line, flush=True)
    with open(WATCH_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x00100000, 0, int(pid))
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _ps_match(pattern: str) -> list[int]:
    """Match CommandLine regex against python.exe and pythonw.exe (Windows)."""
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    # Include pythonw: keep_awake is spawned via pythonw. A python.exe-only
    # filter always returned [] and Task Scheduler flooded duplicate keep_awakes.
    ps_cmd = (
        "Get-CimInstance Win32_Process -Filter "
        "\"Name = 'python.exe' OR Name = 'pythonw.exe'\" | "
        f"Where-Object {{ $_.CommandLine -match '{pattern}' }} | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-WindowStyle",
                "Hidden",
                "-Command",
                ps_cmd,
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=45,
            creationflags=creationflags,
        )
    except Exception:
        return []
    pids: list[int] = []
    for line in (out or "").splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def campaign_processes() -> dict[str, list[int]]:
    return {
        "run_campaign": _ps_match("run_campaign\\.py"),
        "compare_final": _ps_match("backtester\\.py.*compare-final"),
        "monte_carlo": _ps_match("monte_carlo_backtest"),
        "phase_workers": _ps_match(
            "eval_strict_vs_full|monte_carlo_backtest|walk_forward|"
            "full_strategy_experiment|chaotic_backtest|backtest_intraday|"
            "backtest_crypto_vol_v5|run_tod_analysis"
        ),
    }


def campaign_processes_reliable(*, attempts: int = 3, pause_sec: float = 2.0) -> dict[str, list[int]]:
    """Retry PS queries — a single timeout looked like 'no processes' and stacked MC workers."""
    best: dict[str, list[int]] = {
        "run_campaign": [],
        "compare_final": [],
        "monte_carlo": [],
        "phase_workers": [],
    }
    for i in range(max(1, attempts)):
        snap = campaign_processes()
        for key in best:
            if len(snap.get(key) or []) > len(best[key]):
                best[key] = list(snap.get(key) or [])
        if any(best.values()):
            return snap if any(snap.values()) else best
        if i + 1 < attempts:
            time.sleep(pause_sec)
    return best


def _process_roots(pids: list[int]) -> list[int]:
    """Collapse venv launcher→child pairs to independent root PIDs."""
    if not pids:
        return []
    pid_set = set(pids)
    parents = _process_parents(pids)
    roots = [p for p in pids if parents.get(p) not in pid_set]
    return sorted(set(roots)) or sorted(pids)[-1:]


def _process_cmdline(pid: int) -> str:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-WindowStyle",
                "Hidden",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=20,
            creationflags=creationflags,
        )
        return (out or "").strip()
    except Exception:
        return ""


def _kill_pid_tree(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            capture_output=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )
    except Exception:
        pass


def cull_duplicate_workers(procs: dict[str, list[int]]) -> list[int]:
    """
    Keep one run_campaign root and one monte_carlo root per export-dir.
    Returns PIDs that were killed.
    """
    killed: list[int] = []

    rc_roots = _process_roots(procs.get("run_campaign") or [])
    if len(rc_roots) > 1:
        victims = sorted(rc_roots)[:-1]
        for pid in victims:
            _kill_pid_tree(pid)
            killed.append(pid)
        _log(f"Culled duplicate run_campaign roots={victims}; kept={rc_roots[-1]}")

    mc_pids = procs.get("monte_carlo") or []
    mc_roots = _process_roots(mc_pids)
    if len(mc_roots) <= 1:
        return killed

    # Group MC roots by --export-dir path; keep highest PID (newest) per dir.
    by_export: dict[str, list[int]] = {}
    for pid in mc_roots:
        cmd = _process_cmdline(pid)
        export = ""
        if "--export-dir" in cmd:
            parts = cmd.split("--export-dir")
            tail = parts[-1].strip()
            export = tail.split()[0].strip().strip('"').strip("'") if tail else ""
        by_export.setdefault(export or "__unknown__", []).append(pid)

    for export_key, roots in by_export.items():
        if len(roots) <= 1:
            continue
        victims = sorted(roots)[:-1]
        for pid in victims:
            _kill_pid_tree(pid)
            killed.append(pid)
        _log(
            f"Culled duplicate monte_carlo roots={victims} "
            f"export={export_key!r}; kept={roots[-1]}"
        )
    return killed


def heartbeat_age_sec() -> float | None:
    hb = _read_json(HEARTBEAT_PATH)
    if not hb:
        return None
    epoch = hb.get("epoch")
    if isinstance(epoch, (int, float)):
        return max(0.0, time.time() - float(epoch))
    # fallback to mtime
    try:
        return max(0.0, time.time() - HEARTBEAT_PATH.stat().st_mtime)
    except Exception:
        return None


def write_status(*, healthy: bool, action: str, detail: str) -> None:
    state = _read_json(STATE_PATH)
    procs = campaign_processes()
    age = heartbeat_age_sec()
    lines = [
        "# Exhaustive campaign STATUS",
        "",
        f"- Checked: {_utc()}",
        f"- Healthy: **{healthy}**",
        f"- Watchdog action: {action}",
        f"- Detail: {detail}",
        f"- State status: {state.get('status', 'n/a')}",
        f"- Phase: {state.get('phase', 'n/a')} ({state.get('phase_name', '')})",
        f"- Last completed: {state.get('last_completed_phase', 'n/a')}",
        f"- Heartbeat age sec: {age if age is not None else 'n/a'}",
        f"- run_campaign PIDs: {procs['run_campaign']}",
        f"- compare-final PIDs: {procs['compare_final']}",
        f"- monte_carlo PIDs: {procs.get('monte_carlo', [])}",
        f"- other phase PIDs: {procs['phase_workers']}",
        "",
        "Freeze unchanged — research only.",
        "",
    ]
    STATUS_MD.write_text("\n".join(lines), encoding="utf-8")
    _atomic = RUNS / "campaign_watch_status.json"
    _atomic.write_text(
        json.dumps(
            {
                "utc": _utc(),
                "healthy": healthy,
                "action": action,
                "detail": detail,
                "state": state,
                "heartbeat_age_sec": age,
                "processes": procs,
            },
            indent=2,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )


def adopt_legacy_run(procs: dict[str, list[int]]) -> None:
    """If an older campaign is alive without heartbeat, write adoption state."""
    state = _read_json(STATE_PATH)
    if state.get("status") == "completed":
        return
    pids = procs["run_campaign"] or procs["compare_final"]
    if not pids:
        return
    leader = pids[0]
    payload = {
        "status": "running",
        "adopted_utc": _utc(),
        "orchestrator_pid": leader if procs["run_campaign"] else None,
        "child_pid": procs["compare_final"][0] if procs["compare_final"] else None,
        "phase": state.get("phase") or 1,
        "phase_name": state.get("phase_name") or "compare-final (adopted live process)",
        "note": "Adopted pre-hardening process; waiting for exit then resume with durable runner",
    }
    STATE_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    # Synthetic heartbeat so we don't false-restart while live
    HEARTBEAT_PATH.write_text(
        json.dumps(
            {
                "utc": _utc(),
                "epoch": time.time(),
                "pid": leader,
                "adopted": True,
                "phase": payload["phase"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if procs["run_campaign"]:
        PID_PATH.write_text(f"{procs['run_campaign'][0]}\n", encoding="utf-8")


def start_campaign_detached() -> int:
    out = RUNS / "campaign_detached.log"
    err = RUNS / "campaign_detached.err"
    script = HERE / "run_campaign.py"
    state = _read_json(STATE_PATH)
    cmd = [PY, "-u", "-B", str(script), "--resume"]
    if str(state.get("profile") or "") == "fast" or state.get("skip_heavy"):
        cmd.append("--fast")
    # Append markers
    with open(out, "a", encoding="utf-8") as f:
        f.write(f"\n# watchdog restart {_utc()} cmd={cmd}\n")
    creationflags = 0
    if sys.platform == "win32":
        # Hidden: no flashing console on desktop when watchdog respawns work.
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    with open(out, "a", encoding="utf-8") as out_f, open(err, "a", encoding="utf-8") as err_f:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=out_f,
            stderr=err_f,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )
    _log(f"Started detached campaign pid={proc.pid} cmd={cmd}")
    return int(proc.pid)


def _process_parents(pids: list[int]) -> dict[int, int]:
    """Map pid -> ParentProcessId for the given PIDs (Windows best-effort)."""
    if not pids or sys.platform != "win32":
        return {}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    id_list = ",".join(str(p) for p in pids)
    ps_cmd = (
        f"$ids = @({id_list}); "
        "Get-CimInstance Win32_Process -Filter "
        "\"Name = 'python.exe' OR Name = 'pythonw.exe'\" | "
        "Where-Object { $ids -contains $_.ProcessId } | "
        "ForEach-Object { '{0}={1}' -f $_.ProcessId, $_.ParentProcessId }"
    )
    try:
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-WindowStyle",
                "Hidden",
                "-Command",
                ps_cmd,
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=45,
            creationflags=creationflags,
        )
    except Exception:
        return {}
    parents: dict[int, int] = {}
    for line in (out or "").splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        a, b = line.split("=", 1)
        if a.isdigit() and b.isdigit():
            parents[int(a)] = int(b)
    return parents


def _keep_awake_roots(pids: list[int]) -> list[int]:
    """Collapse venv pythonw launcher→child pairs to independent root PIDs."""
    if not pids:
        return []
    pid_set = set(pids)
    parents = _process_parents(pids)
    roots = [p for p in pids if parents.get(p) not in pid_set]
    return roots or sorted(pids)[-1:]


def ensure_keep_awake() -> None:
    """Detach keep_awake.py if not already running (prevents S3 sleep)."""
    pids = _ps_match("keep_awake\\.py")
    roots = _keep_awake_roots(pids)
    if len(roots) > 1:
        # True duplicates only (not venv parent/child). Keep highest PID root.
        victims = sorted(roots)[:-1]
        for extra in victims:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(extra), "/T", "/F"],
                    check=False,
                    capture_output=True,
                    timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                )
            except Exception:
                pass
        _log(f"Culled duplicate keep_awake roots={victims}; kept={sorted(roots)[-1]}")
        return
    if roots:
        return
    script = HERE / "keep_awake.py"
    if not script.is_file():
        _log("keep_awake.py missing — skip")
        return
    out = RUNS / "keep_awake_stdout.log"
    # Prefer pythonw on Windows so keep_awake never opens a console window.
    exe = PY
    if sys.platform == "win32":
        pyw = Path(PY).with_name("pythonw.exe")
        if pyw.is_file():
            exe = str(pyw)
    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    with open(out, "a", encoding="utf-8") as out_f:
        out_f.write(f"\n# watchdog spawn keep_awake {_utc()}\n")
        proc = subprocess.Popen(
            [exe, "-u", "-B", str(script), "--max-hours", "36"],
            cwd=str(ROOT),
            stdout=out_f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )
    _log(f"Started keep_awake pid={proc.pid} exe={exe}")


def ensure_ac_never_sleep() -> None:
    """Best-effort: AC standby/hibernate timeout = never (0)."""
    if sys.platform != "win32":
        return
    for args in (
        ["powercfg", "/change", "standby-timeout-ac", "0"],
        ["powercfg", "/change", "hibernate-timeout-ac", "0"],
        ["powercfg", "/change", "standby-timeout-dc", "0"],
        ["powercfg", "/change", "hibernate-timeout-dc", "0"],
    ):
        try:
            subprocess.run(args, check=False, capture_output=True, timeout=15)
        except Exception:
            pass


def main() -> int:
    ensure_ac_never_sleep()
    pause_flag = RUNS / "WATCHDOG_PAUSED"
    if pause_flag.is_file():
        write_status(
            healthy=True,
            action="paused",
            detail="WATCHDOG_PAUSED present — not monitoring/restarting",
        )
        _log("PAUSED — WATCHDOG_PAUSED flag set")
        return 0

    procs = campaign_processes_reliable()
    state = _read_json(STATE_PATH)
    status = str(state.get("status") or "")

    if status == "completed":
        write_status(healthy=True, action="noop", detail="Campaign already completed")
        _log("OK completed")
        return 0

    alive = bool(procs["run_campaign"] or procs["compare_final"] or procs["phase_workers"])
    age = heartbeat_age_sec()

    if alive:
        culled = cull_duplicate_workers(procs)
        if culled:
            procs = campaign_processes_reliable(attempts=2, pause_sec=1.0)
        ensure_keep_awake()
        # Keep heartbeat fresh for legacy runs that don't write one
        if age is None or age > 60:
            adopt_legacy_run(procs)
            age = heartbeat_age_sec()
        write_status(
            healthy=True,
            action="monitor",
            detail=f"Live processes present; heartbeat_age={age}; keep_awake ensured",
        )
        _log(f"OK live procs={procs} heartbeat_age={age}")
        return 0

    # No live processes
    if status == "halted":
        write_status(
            healthy=False,
            action="halted_needs_human",
            detail="State=halted after critical failure — not auto-restarting. Inspect logs.",
        )
        _log("HALTED — human review required")
        return 2

    # Re-check with retries — false empty PS results stacked duplicate MC workers.
    procs2 = campaign_processes_reliable(attempts=3, pause_sec=2.0)
    if procs2["run_campaign"] or procs2["compare_final"] or procs2["phase_workers"]:
        cull_duplicate_workers(procs2)
        ensure_keep_awake()
        adopt_legacy_run(procs2)
        write_status(
            healthy=True,
            action="adopt_no_spawn",
            detail=f"Re-check found live procs before restart; adopted {procs2}",
        )
        _log(f"ADOPT (no spawn) procs={procs2}")
        return 0

    # Dead mid-run or never started with state
    detail = f"No campaign processes; status={status or 'missing'}; restarting --resume"
    _log(detail)
    try:
        ensure_keep_awake()
        pid = start_campaign_detached()
        write_status(healthy=True, action="restarted", detail=f"Started pid={pid}")
        return 0
    except Exception as exc:
        write_status(healthy=False, action="restart_failed", detail=str(exc))
        _log(f"restart failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
