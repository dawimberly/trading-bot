"""
Exhaustive campaign orchestrator — durable, freeze-safe, no paper/live wiring.

Designed for multi-hour / overnight unattended runs (retirement-fund research bar):
  - PID + single-instance lock
  - Heartbeat + state JSON (watchdog can resume)
  - Retries on failure; halt on Phase 1 failure after retries
  - Strict cwd = stock-bot root
  - Detach-friendly (no Cursor shell dependency)

Usage (from stock-bot/):
  python -u -B scripts/research/exhaustive_campaign/run_campaign.py --from-phase 1
  python -u -B scripts/research/exhaustive_campaign/run_campaign.py --resume
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

def _find_stock_bot_root() -> Path:
    here = Path(__file__).resolve().parent
    for cand in (here, *here.parents):
        if (cand / "backtester.py").is_file() and (cand / "modules").is_dir():
            return cand
    raise RuntimeError(f"Could not locate stock-bot root from {here}")


ROOT = _find_stock_bot_root()
RUNS = Path(__file__).resolve().parent / "runs"
RUNS.mkdir(parents=True, exist_ok=True)

PY = sys.executable
MASTER_LOG = RUNS / "campaign_master.log"
STATE_PATH = RUNS / "campaign_state.json"
HEARTBEAT_PATH = RUNS / "campaign_heartbeat.json"
PID_PATH = RUNS / "campaign.pid"
LOCK_PATH = RUNS / "campaign.lock"

# Phase 1 success markers (must appear before advancing)
PHASE1_SUCCESS_MARKERS = (
    "Report saved:",
    "=== 365D ===",
    "--- FINAL PAPER BOT COMPARISON",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _log(msg: str) -> None:
    line = f"[{_utc_now()}] {msg}"
    print(line, flush=True)
    with open(MASTER_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Unique tmp + replace retries — fixed *.json.tmp names collide on Windows (WinError 5)."""
    data = json.dumps(payload, indent=2, default=str) + "\n"
    last_err: Exception | None = None
    for attempt in range(8):
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        try:
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, path)
            return
        except OSError as exc:
            last_err = exc
            try:
                if tmp.is_file():
                    tmp.unlink()
            except OSError:
                pass
            # WinError 5 / sharing violation under concurrent writers
            time.sleep(0.05 * (attempt + 1))
    raise PermissionError(f"atomic write failed for {path}: {last_err}")


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
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except SystemError:
        return False
    # Windows: os.kill(pid, 0) may not exist the same way — also try OpenProcess via ctypes
    if sys.platform == "win32":
        try:
            import ctypes

            SYNCHRONIZE = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, 0, int(pid))
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    return True


def _cmdline_has_campaign(pid: int) -> bool:
    """Best-effort: confirm PID is our campaign (avoids stale PID reuse)."""
    creationflags = 0
    if sys.platform == "win32":
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
        return "run_campaign.py" in (out or "")
    except Exception:
        return False


def _orphan_research_pids() -> list[int]:
    """PIDs for compare-final / heavy phase workers that must not stack."""
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-WindowStyle",
                "Hidden",
                "-Command",
                "Get-CimInstance Win32_Process -Filter "
                "\"Name = 'python.exe' OR Name = 'pythonw.exe'\" | "
                "Where-Object { $_.CommandLine -match "
                "'backtester\\.py.*compare-final|monte_carlo_backtest|walk_forward|"
                "full_strategy_experiment|chaotic_backtest|eval_strict_vs_full' } | "
                "Select-Object -ExpandProperty ProcessId",
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


def acquire_lock() -> None:
    """Refuse to start a second campaign if one is healthy."""
    if PID_PATH.is_file():
        try:
            old = int(PID_PATH.read_text(encoding="utf-8").strip().splitlines()[0])
        except Exception:
            old = 0
        if old and _pid_alive(old) and (old == os.getpid() or _cmdline_has_campaign(old)):
            raise SystemExit(
                f"Another campaign is running (pid={old}). "
                f"Watchdog will supervise it; refuse duplicate start."
            )
    orphans = _orphan_research_pids()
    if orphans:
        raise SystemExit(
            f"Refuse start: orphan research PIDs still alive {orphans}. "
            "Kill them before launching another campaign."
        )
    PID_PATH.write_text(f"{os.getpid()}\n", encoding="utf-8")
    LOCK_PATH.write_text(
        json.dumps({"pid": os.getpid(), "started_utc": _utc_now()}, indent=2) + "\n",
        encoding="utf-8",
    )


def release_lock() -> None:
    try:
        if PID_PATH.is_file():
            cur = PID_PATH.read_text(encoding="utf-8").strip().splitlines()[0]
            if cur == str(os.getpid()):
                PID_PATH.unlink(missing_ok=True)
                LOCK_PATH.unlink(missing_ok=True)
    except Exception:
        pass


class Heartbeat:
    def __init__(self, interval_sec: float = 20.0) -> None:
        self.interval = interval_sec
        self._stop = threading.Event()
        self._extra: dict[str, Any] = {}
        self._thread = threading.Thread(target=self._loop, name="campaign-heartbeat", daemon=True)

    def set_extra(self, **kwargs: Any) -> None:
        self._extra.update(kwargs)

    def start(self) -> None:
        self._thread.start()
        self.beat()

    def stop(self) -> None:
        self._stop.set()
        self.beat(final=True)

    def beat(self, *, final: bool = False) -> None:
        payload = {
            "utc": _utc_now(),
            "epoch": time.time(),
            "pid": os.getpid(),
            "final": final,
            **self._extra,
        }
        try:
            _atomic_write_json(HEARTBEAT_PATH, payload)
        except Exception as exc:
            _log(f"heartbeat write failed: {exc}")

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.beat()


def write_state(update: dict[str, Any]) -> None:
    state = _read_json(STATE_PATH)
    state.update(update)
    state["updated_utc"] = _utc_now()
    state["orchestrator_pid"] = os.getpid()
    _atomic_write_json(STATE_PATH, state)


def ensure_campaign_artifacts(*, profile: str) -> Path:
    """
    Durable per-campaign folder: README + mc/ for JSONL summaries.
    Reuses campaign_state.json artifact_dir on --resume.
    """
    artifacts_root = RUNS / "artifacts"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    state = _read_json(STATE_PATH)
    existing = state.get("artifact_dir")
    if isinstance(existing, str) and existing.strip():
        d = Path(existing)
        if d.is_dir():
            (d / "mc").mkdir(parents=True, exist_ok=True)
            return d
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d = artifacts_root / f"campaign_{stamp}_{profile}"
    mc = d / "mc"
    mc.mkdir(parents=True, exist_ok=True)
    readme = d / "README.md"
    if not readme.is_file():
        readme.write_text(
            "\n".join(
                [
                    f"# Campaign artifacts (`{profile}`)",
                    "",
                    f"Created: {_utc_now()}",
                    "",
                    "Research-only. Freeze / paper / live are not wired from this folder.",
                    "",
                    "## Layout",
                    "",
                    "| Path | Role |",
                    "|------|------|",
                    "| `mc/` | Monte Carlo durable exports (`runs.jsonl`, `summary.md`, …) |",
                    "| `../phaseN_*.log` | Phase stdout (sibling under `runs/`) |",
                    "| `MANIFEST.json` | Profile + paths snapshot |",
                    "",
                    "## Reading MC mid-flight",
                    "",
                    "```powershell",
                    f"Get-Content '{mc / 'summary.md'}'",
                    f"Get-Content '{mc / 'runs.jsonl'}' -Tail 5",
                    "```",
                    "",
                    "Partial until `mc/summary.json` has `\"complete\": true`.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    manifest = {
        "created_utc": _utc_now(),
        "profile": profile,
        "root": str(ROOT),
        "runs_dir": str(RUNS),
        "mc_dir": str(mc),
    }
    (d / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    write_state({"artifact_dir": str(d)})
    _log(f"Artifacts folder: {d}")
    return d


def phase_catalog(
    *,
    fast: bool = False,
    mc_export_dir: Path | None = None,
) -> list[tuple[int, str, list[str], str, bool]]:
    """
    (phase_num, name, argv, log_name, critical)
    critical=True => halt campaign after retries exhausted.
    fast=True => Monte Carlo 50 instead of 200 (caller still skips phases 6–7).
    """
    mc_runs = "50" if fast else "200"
    mc_cmd = [
        PY,
        "-u",
        "-B",
        "scripts/analysis/monte_carlo_backtest.py",
        "--paper-aggressive",
        "--days",
        "365",
        "--mc-runs",
        mc_runs,
    ]
    if mc_export_dir is not None:
        mc_cmd.extend(["--export-dir", str(mc_export_dir)])
    return [
        (
            1,
            "compare-final 365d + regimes",
            [
                PY,
                "-u",
                "-B",
                "backtester.py",
                "--compare-final",
                "--days",
                "365",
                "--paper-aggressive",
                "--regime-breakdown",
            ],
            "phase1_compare_final_365.log",
            True,
        ),
        (
            2,
            "STRICT vs FULL 365d",
            [PY, "-u", "-B", "scripts/analysis/eval_strict_vs_full.py", "--days", "365"],
            "phase2_eval_strict_vs_full_365.log",
            True,
        ),
        (
            3,
            "TOD analysis 365d",
            [PY, "-u", "-B", "scripts/analysis/run_tod_analysis.py", "--days", "365"],
            "phase3_tod_365.log",
            False,
        ),
        (
            4,
            f"Monte Carlo {mc_runs}",
            mc_cmd,
            f"phase4_mc_{mc_runs}_365.log",
            True,
        ),
        (
            5,
            "Walk-forward 4",
            [
                PY,
                "-u",
                "-B",
                "scripts/analysis/walk_forward.py",
                "--days",
                "365",
                "--paper-aggressive",
                "--walk-steps",
                "4",
            ],
            "phase5_walk_forward_4.log",
            True,
        ),
        (
            6,
            "Full strategy experiment (all phases)",
            [
                PY,
                "-u",
                "-B",
                "scripts/analysis/full_strategy_experiment.py",
                "--paper-aggressive",
                "--days",
                "365",
                "--phase",
                "all",
                "--mc-runs",
                "50",
                "--walk-forward",
                "4",
                "--resume",
            ],
            "phase6_full_strategy_experiment.log",
            False,
        ),
        (
            7,
            "Chaos scenarios",
            [PY, "-u", "-B", "scripts/analysis/chaotic_backtest.py", "--days", "365"],
            "phase7_chaotic_backtest.log",
            False,
        ),
        (
            8,
            "Intraday NYSE 5m 90d",
            [
                PY,
                "-u",
                "-B",
                "scripts/research/backtest_intraday.py",
                "--days",
                "90",
                "--quality-fixes",
            ],
            "phase8_backtest_intraday_90.log",
            False,
        ),
        (
            9,
            "Crypto vol research pack",
            [PY, "-u", "-B", "scripts/research/backtest_crypto_vol_v5.py", "--days", "90"],
            "phase8_crypto_vol_v5_90.log",
            False,
        ),
    ]


def _phase1_log_ok(log_path: Path) -> bool:
    if not log_path.is_file():
        return False
    try:
        # Read tail without loading entire multi‑MB log
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 120_000))
            tail = f.read().decode("utf-8", errors="ignore")
    except Exception:
        return False
    if "Traceback (most recent call last)" in tail and "SyntaxError" in tail:
        return False
    return any(m in tail for m in PHASE1_SUCCESS_MARKERS)


def _run_once(
    *,
    phase_num: int,
    name: str,
    args: list[str],
    log_name: str,
    heartbeat: Heartbeat,
) -> int:
    log_path = RUNS / log_name
    # Rotate previous failed attempt aside (keep evidence)
    if log_path.is_file() and log_path.stat().st_size > 0:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bak = RUNS / f"{log_path.stem}.prev_{stamp}{log_path.suffix}"
        try:
            log_path.replace(bak)
            _log(f"archived prior log -> {bak.name}")
        except Exception:
            pass

    _log(f"START phase={phase_num} {name}: {' '.join(args)}")
    write_state(
        {
            "status": "running",
            "phase": phase_num,
            "phase_name": name,
            "log": log_name,
            "cmd": args,
            "child_pid": None,
        }
    )
    heartbeat.set_extra(phase=phase_num, phase_name=name, log=log_name)

    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        log.write(f"# phase={phase_num} {name}\n# cmd: {args}\n# started: {_utc_now()}\n\n")
        log.flush()
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        # Durable MC status beside export-dir (phase 4).
        if phase_num == 4:
            art = _read_json(STATE_PATH).get("artifact_dir")
            if art:
                mc_dir = Path(str(art)) / "mc"
                mc_dir.mkdir(parents=True, exist_ok=True)
                env["MC_EXPORT_DIR"] = str(mc_dir)
                env["MC_STATUS_PATH"] = str(mc_dir / "progress.txt")
        proc = subprocess.Popen(
            args,
            cwd=str(ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        write_state({"child_pid": proc.pid})
        heartbeat.set_extra(child_pid=proc.pid)
        rc = int(proc.wait())

    ok_extra = ""
    if phase_num == 1 and rc == 0 and not _phase1_log_ok(log_path):
        _log(
            "Phase 1 returned 0 but success markers missing in log — treating as FAILURE"
        )
        rc = 3
        ok_extra = " (marker check failed)"
    elif phase_num == 1 and rc == 0:
        ok_extra = " (markers OK)"

    _log(f"END phase={phase_num} {name}: exit={rc}{ok_extra} log={log_name}")
    return rc


def run_phase_with_retries(
    *,
    phase_num: int,
    name: str,
    args: list[str],
    log_name: str,
    critical: bool,
    retries: int,
    heartbeat: Heartbeat,
) -> int:
    attempt = 0
    last_rc = 1
    while attempt <= retries:
        attempt += 1
        write_state({"attempt": attempt, "retries_max": retries})
        if attempt > 1:
            _log(f"RETRY phase={phase_num} {name} attempt={attempt}/{retries + 1}")
            time.sleep(min(30 * attempt, 120))
        try:
            last_rc = _run_once(
                phase_num=phase_num,
                name=name,
                args=args,
                log_name=log_name,
                heartbeat=heartbeat,
            )
        except Exception:
            last_rc = 99
            _log(f"EXCEPTION phase={phase_num} {name}:\n{traceback.format_exc()}")
        if last_rc == 0:
            write_state(
                {
                    "last_completed_phase": phase_num,
                    "last_completed_name": name,
                    "last_rc": 0,
                }
            )
            return 0
    write_state(
        {
            "last_failed_phase": phase_num,
            "last_failed_name": name,
            "last_rc": last_rc,
            "critical_halt": bool(critical),
        }
    )
    if critical:
        _log(
            f"HALT: critical phase={phase_num} {name} failed after {retries + 1} "
            f"attempts (rc={last_rc})"
        )
    else:
        _log(
            f"WARN: non-critical phase={phase_num} {name} failed after "
            f"{retries + 1} attempts (rc={last_rc}) — continuing"
        )
    return last_rc


def write_synthesis(failures: list[dict[str, Any]], halted: bool) -> None:
    synth = RUNS / "phase10_synthesis_stub.md"
    lines = [
        "# Exhaustive campaign synthesis (stub)",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Halted early: {halted}",
        f"Failures: {len(failures)}",
        "",
    ]
    for f in failures:
        lines.append(f"- phase {f.get('phase')}: {f.get('name')} rc={f.get('rc')}")
    lines += [
        "",
        "Review logs in this folder; promote only survivors that beat STRICT rules.",
        "Freeze unchanged — no paper/live wiring.",
        "",
    ]
    synth.write_text("\n".join(lines), encoding="utf-8")


def resolve_start_phase(args: argparse.Namespace) -> int:
    if args.resume:
        state = _read_json(STATE_PATH)
        if state.get("status") == "completed":
            _log("Resume: prior campaign already completed — nothing to do")
            raise SystemExit(0)
        last = state.get("last_completed_phase")
        if isinstance(last, int):
            nxt = last + 1
            _log(f"Resume: last_completed_phase={last} -> starting phase {nxt}")
            return nxt
        cur = state.get("phase")
        if isinstance(cur, int) and state.get("status") == "running":
            _log(f"Resume: incomplete phase={cur} will be re-run")
            return cur
        return int(args.from_phase)
    return int(args.from_phase)


def main() -> int:
    if not (ROOT / "backtester.py").is_file():
        print(f"FATAL: ROOT is not stock-bot: {ROOT}", file=sys.stderr)
        return 2

    ap = argparse.ArgumentParser(description="Durable exhaustive research campaign")
    ap.add_argument("--from-phase", type=int, default=1)
    ap.add_argument("--resume", action="store_true", help="Continue from campaign_state.json")
    ap.add_argument("--retries", type=int, default=2, help="Retries per phase after first try")
    ap.add_argument("--skip-heavy", action="store_true")
    ap.add_argument(
        "--fast",
        action="store_true",
        help="Faster profile: skip phases 6–7, Monte Carlo 50 (persisted in state for --resume)",
    )
    ap.add_argument(
        "--continue-on-critical-failure",
        action="store_true",
        help="Do NOT use for production research runs",
    )
    args = ap.parse_args()

    state0 = _read_json(STATE_PATH)
    fast = bool(args.fast) or str(state0.get("profile") or "") == "fast"
    skip_heavy = bool(args.skip_heavy) or fast

    acquire_lock()
    hb = Heartbeat(interval_sec=20.0)
    hb.start()
    failures: list[dict[str, Any]] = []
    halted = False
    try:
        start = resolve_start_phase(args)
        profile_name = "fast" if fast else "full"
        art = ensure_campaign_artifacts(profile=profile_name)
        mc_dir = art / "mc"
        write_state(
            {
                "status": "running",
                "started_utc": _utc_now(),
                "from_phase": start,
                "root": str(ROOT),
                "python": PY,
                "failures": [],
                "profile": profile_name,
                "skip_heavy": skip_heavy,
                "artifact_dir": str(art),
            }
        )
        _log(
            f"Campaign start root={ROOT} python={PY} from_phase={start} "
            f"profile={profile_name} skip_heavy={skip_heavy} artifacts={art}"
        )

        for phase_num, name, cmd, log_name, critical in phase_catalog(
            fast=fast, mc_export_dir=mc_dir
        ):
            if phase_num < start:
                continue
            if skip_heavy and phase_num in (6, 7):
                _log(f"SKIP {name} (--fast/--skip-heavy)")
                continue
            rc = run_phase_with_retries(
                phase_num=phase_num,
                name=name,
                args=cmd,
                log_name=log_name,
                critical=critical,
                retries=max(0, int(args.retries)),
                heartbeat=hb,
            )
            if rc != 0:
                failures.append({"phase": phase_num, "name": name, "rc": rc})
                write_state({"failures": failures})
                if critical and not args.continue_on_critical_failure:
                    halted = True
                    write_state({"status": "halted", "halt_phase": phase_num})
                    break

        if not halted:
            write_state({"status": "completed", "completed_utc": _utc_now()})
            _log("Campaign chain COMPLETED")
        else:
            _log("Campaign chain HALTED")

        write_synthesis(failures, halted=halted)
        return 0 if not failures and not halted else 1
    finally:
        hb.stop()
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
