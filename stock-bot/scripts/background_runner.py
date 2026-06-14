"""Trading bot background runner — scheduled health checks and paper maintenance.

Mirrors ufc-predictor/scripts/background_runner pattern for PythonTrading:

  midnight  — full health check, optional data refresh, paper cycle when safe
  startup   — quick status, safety, ensure paper supervisor if configured

CLI:
    python scripts/background_runner.py --mode full --trigger midnight
    python scripts/background_runner.py --mode auto --trigger startup
    python scripts/background_runner.py --mode lightweight --trigger manual
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "logs" / "background_runner_manifest.json"
LOG_NAME = "background_runner.log"

FULL_STALE_HOURS = 24
HEARTBEAT_STALE_SEC = 1800  # 30 min
PAPER_HEARTBEAT = "paper_chase_heartbeat.json"
LIVE_HEARTBEAT = os.getenv("HEARTBEAT_FILE", "bot_heartbeat.json")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _age_hours(ts: str | None) -> float | None:
    dt = _parse_iso(ts)
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0


def _file_age_hours(path: Path) -> float | None:
    if not path.is_file():
        return None
    return (datetime.now().timestamp() - path.stat().st_mtime) / 3600.0


def _heartbeat_age_sec(path: Path) -> float | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ts = data.get("timestamp")
        dt = _parse_iso(str(ts) if ts else None)
        if dt is None:
            return path.stat().st_mtime and (
                datetime.now().timestamp() - path.stat().st_mtime
            )
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _python() -> str:
    venv = ROOT / ".venv" / "Scripts" / "python.exe"
    if venv.is_file():
        return str(venv)
    venv_unix = ROOT / ".venv" / "bin" / "python"
    if venv_unix.is_file():
        return str(venv_unix)
    return sys.executable


def _subprocess_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def read_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.is_file():
        return {}
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_manifest(payload: dict[str, Any]) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def is_full_run_stale(manifest: dict[str, Any] | None = None) -> bool:
    manifest = manifest if manifest is not None else read_manifest()
    full_at = manifest.get("full_run_at") or manifest.get("saved_at")
    age = _age_hours(full_at)
    return age is None or age > FULL_STALE_HOURS


def _find_script_pids(script_name: str) -> list[int]:
    try:
        from modules.portal_bot import _find_script_pids as find_pids

        return find_pids(script_name)
    except Exception:
        return []


def _paper_only_ok() -> bool:
    paper = os.getenv("PAPER_TRADING", "true").lower() in ("1", "true", "yes")
    live_allowed = os.getenv("ALLOW_LIVE_TRADING", "false").lower() in (
        "1",
        "true",
        "yes",
    )
    chase = os.getenv("PAPER_CHASE_MODE", "0").lower() in ("1", "true", "yes")
    return chase or (paper and not live_allowed)


def _live_mode_active() -> bool:
    return os.getenv("PAPER_TRADING", "true").lower() not in ("1", "true", "yes")


def _run_subprocess(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
) -> tuple[int, str]:
    cmd = [_python(), *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            env=env or os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_subprocess_flags(),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out.strip()
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "")
        return 124, str(out).strip() or "timeout"
    except OSError as exc:
        return 1, str(exc)


def _safety_snapshot() -> dict[str, Any]:
    import config
    from modules.trading_safety import get_daily_loss_status

    live_status = get_daily_loss_status(paper=False)
    paper_status = get_daily_loss_status(paper=True)
    return {
        "live_tripped": bool(live_status.get("tripped")),
        "live_limit_pct": live_status.get("limit_pct"),
        "live_loss_pct": live_status.get("loss_pct"),
        "paper_tripped": bool(paper_status.get("tripped")),
        "paper_limit_pct": paper_status.get("limit_pct"),
        "paper_loss_pct": paper_status.get("loss_pct"),
        "thinking_enabled": bool(config.effective_thinking_engine_enabled()),
        "paper_chase": bool(config.paper_chase_mode_enabled()),
        "live_profile": not config.PAPER_TRADING,
    }


def _heartbeat_snapshot() -> dict[str, Any]:
    live_path = ROOT / LIVE_HEARTBEAT
    paper_path = ROOT / os.getenv("PAPER_CHASE_HEARTBEAT", PAPER_HEARTBEAT)
    live_age = _heartbeat_age_sec(live_path)
    paper_age = _heartbeat_age_sec(paper_path)
    return {
        "live_file": str(live_path.name),
        "live_age_sec": live_age,
        "live_stale": live_age is None or live_age > HEARTBEAT_STALE_SEC,
        "paper_file": str(paper_path.name),
        "paper_age_sec": paper_age,
        "paper_stale": paper_age is None or paper_age > HEARTBEAT_STALE_SEC,
        "run_all_running": bool(_find_script_pids("run_all.py")),
        "paper_bot_running": bool(_find_script_pids("run_paper_bot.py")),
    }


def _run_status() -> tuple[int, str]:
    return _run_subprocess(["status.py"], timeout=120)


def _run_preflight(*, paper: bool | None = None) -> tuple[int, str]:
    env = os.environ.copy()
    if paper is True:
        env["PAPER_TRADING"] = "true"
        env["ALLOW_LIVE_TRADING"] = "false"
        env.setdefault("PAPER_CHASE_MODE", "1")
    elif paper is False:
        env["PAPER_TRADING"] = "false"
    return _run_subprocess(
        ["scripts/account/preflight.py"],
        env=env,
        timeout=600,
    )


def _refresh_data_if_stale(*, max_db_age_hours: float = 24.0) -> tuple[bool, str]:
    db = ROOT / "market_data.db"
    age = _file_age_hours(db)
    if age is not None and age <= max_db_age_hours:
        return True, f"market_data.db fresh ({age:.1f}h)"
    code, out = _run_subprocess(["fetch_data.py"], timeout=900)
    if code == 0:
        return True, "fetch_data.py completed"
    return False, out[:500] or f"fetch_data exit {code}"


def _run_paper_piece_apply() -> tuple[int, str]:
    if not _paper_only_ok():
        return 0, "skipped — not in paper-only / chase profile"
    from modules.trading_safety import daily_loss_circuit_tripped

    tripped, reason, _ = daily_loss_circuit_tripped(None, paper=True)
    if tripped:
        return 0, f"skipped — {reason}"

    try:
        from modules.market_hours import is_equity_market_open

        if not is_equity_market_open():
            return 0, "skipped — US equity market closed"
    except Exception as exc:
        logger.warning("Market hours check failed: %s", exc)

    env = os.environ.copy()
    env["PAPER_TRADING"] = "true"
    env["ALLOW_LIVE_TRADING"] = "false"
    env.setdefault("PAPER_CHASE_MODE", "1")
    return _run_subprocess(
        [
            "scripts/research/run_paper_piece.py",
            "--piece",
            "status",
            "--piece",
            "all-active",
            "--apply",
        ],
        env=env,
        timeout=600,
    )


def _ensure_paper_supervisor() -> tuple[bool, str]:
    """Start run_paper_bot.py if paper-only, not running, and auto-start enabled."""
    if os.getenv("TRADING_BOT_AUTO_START_PAPER", "true").lower() not in (
        "1",
        "true",
        "yes",
    ):
        return False, "auto-start disabled (TRADING_BOT_AUTO_START_PAPER=false)"
    if not _paper_only_ok():
        return False, "skipped — live profile or ALLOW_LIVE_TRADING set"
    if _find_script_pids("run_paper_bot.py") or _find_script_pids("run_all.py"):
        return True, "paper/live bot already running"
    script = ROOT / "run_paper_bot.py"
    if not script.is_file():
        return False, "run_paper_bot.py missing"
    log_path = ROOT / "logs" / "paper_bot_supervisor.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_handle:
        log_handle.write(f"\n--- auto-start {_utc_iso()} ---\n")
        subprocess.Popen(
            [_python(), str(script)],
            cwd=str(ROOT),
            env=os.environ.copy(),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=_subprocess_flags(),
        )
    return True, f"started run_paper_bot.py (log: {log_path.name})"


def _resolve_mode(mode: str, trigger: str) -> str:
    if mode != "auto":
        return mode
    if trigger == "midnight":
        return "full"
    if is_full_run_stale():
        return "full"
    return "lightweight"


def run_lightweight_job(*, trigger: str) -> int:
    logger.info("Lightweight health check — trigger=%s", trigger)
    issues: list[str] = []
    status_code, status_out = _run_status()
    if status_code != 0:
        issues.append(f"status.py exit {status_code}")

    hb = _heartbeat_snapshot()
    safety = _safety_snapshot()
    if safety.get("live_tripped"):
        issues.append("live daily loss circuit tripped")
    if safety.get("paper_tripped"):
        issues.append("paper daily loss circuit tripped")
    if _live_mode_active() and hb.get("live_stale") and not hb.get("run_all_running"):
        issues.append("live heartbeat stale and run_all not running")

    started, start_msg = _ensure_paper_supervisor()

    manifest = {
        "saved_at": _utc_iso(),
        "run_type": "lightweight",
        "trigger": trigger,
        "status_exit": status_code,
        "heartbeat": hb,
        "safety": safety,
        "paper_supervisor": start_msg,
        "issues": issues,
        "status_snip": status_out.splitlines()[0] if status_out else "",
    }
    write_manifest(manifest)

    if issues:
        logger.warning("Lightweight check issues: %s", "; ".join(issues))
        return 1
    logger.info("Lightweight check OK — %s", start_msg)
    return 0


def run_full_job(*, trigger: str) -> int:
    logger.info("Full health check — trigger=%s", trigger)
    issues: list[str] = []

    fresh_ok, fresh_msg = _refresh_data_if_stale()
    if not fresh_ok:
        issues.append(fresh_msg)

    status_code, status_out = _run_status()
    if status_code != 0:
        issues.append(f"status.py exit {status_code}")

    preflight_code, preflight_out = _run_preflight(paper=True)
    if preflight_code != 0:
        issues.append("paper preflight failed")

    if _live_mode_active():
        live_code, _ = _run_preflight(paper=False)
        if live_code != 0:
            issues.append("live preflight failed")

    paper_code, paper_msg = _run_paper_piece_apply()
    if paper_code not in (0,):
        issues.append(f"paper cycle exit {paper_code}")

    hb = _heartbeat_snapshot()
    safety = _safety_snapshot()
    started, start_msg = _ensure_paper_supervisor()

    manifest = {
        "saved_at": _utc_iso(),
        "full_run_at": _utc_iso(),
        "run_type": "full",
        "trigger": trigger,
        "data_refresh": fresh_msg,
        "status_exit": status_code,
        "preflight_paper_exit": preflight_code,
        "paper_cycle_exit": paper_code,
        "paper_cycle_msg": paper_msg[:400],
        "heartbeat": hb,
        "safety": safety,
        "paper_supervisor": start_msg,
        "issues": issues,
    }
    write_manifest(manifest)

    if issues:
        logger.warning("Full check issues: %s", "; ".join(issues))
        return 1
    logger.info("Full check OK | paper cycle: %s | %s", paper_msg[:120], start_msg)
    return 0


def run_background(*, mode: str = "auto", trigger: str = "manual") -> int:
    resolved = _resolve_mode(mode, trigger)
    if resolved == "skip":
        logger.info("Background run skipped — recent full run in manifest")
        return 0
    if resolved == "full":
        return run_full_job(trigger=trigger)
    if resolved == "lightweight":
        return run_lightweight_job(trigger=trigger)
    logger.error("Unknown mode: %s", mode)
    return 2


def _bootstrap() -> None:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv())
    from modules.logging_utils import setup_project_logging

    setup_project_logging()
    log_dir = ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_dir / LOG_NAME, encoding="utf-8")
    fh.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    logging.getLogger().addHandler(fh)


def main(argv: list[str] | None = None) -> int:
    _bootstrap()
    parser = argparse.ArgumentParser(description="PythonTrading background runner")
    parser.add_argument(
        "--mode",
        choices=("auto", "full", "lightweight", "skip"),
        default="auto",
        help="auto: full at midnight or if stale; startup uses lightweight",
    )
    parser.add_argument(
        "--trigger",
        choices=("startup", "midnight", "manual", "scheduled"),
        default="manual",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    logger.info(
        "Background runner start — root=%s mode=%s trigger=%s",
        ROOT,
        args.mode,
        args.trigger,
    )
    try:
        return run_background(mode=args.mode, trigger=args.trigger)
    except Exception as exc:
        logger.error("Unhandled error: %s", exc)
        logger.debug(traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
