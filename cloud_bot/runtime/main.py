"""Cloud bot entrypoint — 24/7 paper trading, backtest, or status."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import signal
import sys
import time
from pathlib import Path

# Repo root on path for `cloud_bot` package and parent imports
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from dotenv import find_dotenv, load_dotenv

from cloud_bot.config.profile import apply_best_paper_profile
from cloud_bot.config.settings import CloudSettings, load_settings
from cloud_bot.runtime.logging_setup import setup_logging


def _load_cloud_settings(args: argparse.Namespace) -> CloudSettings:
    if args.profile:
        os.environ["CLOUD_BOT_PROFILE"] = args.profile
    if args.dry_run:
        os.environ["CLOUD_BOT_DRY_RUN"] = "true"

    settings = load_settings()
    if args.dry_run and not settings.dry_run:
        settings = dataclasses.replace(settings, dry_run=True)
    return settings


def _load_heartbeat(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _active_sleeves(heartbeat: dict | None) -> list[str]:
    if not heartbeat:
        return []
    exposure = heartbeat.get("sleeve_exposure") or {}
    return [key for key, value in exposure.items() if value]


def _check_running(pid_file: Path) -> str:
    if not pid_file.exists():
        return "no"
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except ValueError:
        return "invalid pid"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "stale"
    except PermissionError:
        return "access denied"
    return f"yes ({pid})"


def _stop_loop(settings: CloudSettings, logger) -> int:
    if not settings.pid_file.exists():
        print("No PID file found; is the cloud bot loop running?")
        return 1
    try:
        pid = int(settings.pid_file.read_text(encoding="utf-8").strip())
    except ValueError:
        print("PID file contains invalid PID")
        return 1
    print(f"Stopping cloud bot process {pid}...")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print("Process not found; removing stale PID file")
        settings.pid_file.unlink(missing_ok=True)
        return 1
    except PermissionError as exc:
        print(f"Permission denied while stopping process: {exc}")
        return 1
    logger.info("sent SIGTERM to pid=%s", pid)
    return 0


def _print_status(settings: CloudSettings) -> int:
    print("Cloud Bot status")
    print("---------------")
    print(f"profile:          {settings.profile}")
    print(f"dry_run:          {settings.dry_run}")
    print(f"paper_trading:    {settings.paper_trading}")
    print(f"cycle_sec:        {settings.cycle_sec}")
    print(f"heartbeat_file:   {settings.heartbeat_file}")
    print(f"journal_csv:      {settings.journal_csv}")
    print(f"repo_root:        {settings.repo_root}")
    print(f"run_all_script:   {settings.run_all_script}")
    print(f"log_dir:          {settings.log_dir}")
    print(f"run_all exists:   {settings.run_all_script.is_file()}")
    heartbeat = _load_heartbeat(settings.heartbeat_file)
    running = _check_running(settings.pid_file)
    print(f"running:           {running}")
    if heartbeat:
        print(f"last_heartbeat:    {heartbeat.get('timestamp')}")
        print(f"equity:            ${float(heartbeat.get('equity', 0)):,.2f}")
        print(f"cash:              ${float(heartbeat.get('cash', 0)):,.2f}")
        print(f"regime:            {heartbeat.get('regime', '—')}")
        print(f"halted:            {heartbeat.get('halted', False)}")
        print(f"paper:             {heartbeat.get('paper', False)}")
        print(f"sleeves:           {', '.join(_active_sleeves(heartbeat)) or 'none'}")
        print(f"heartbeat_age:     {int(time.time() - settings.heartbeat_file.stat().st_mtime)}s")
        print(f"dynamic_vol_score: {heartbeat.get('dynamic_vol_score', '—')}")
        print(f"vti_target_pct:    {float((heartbeat.get('sleeve_caps') or {}).get('vti_core', 0)):.2%}")
    else:
        print("heartbeat:         none")
        print(f"pid_file:          {settings.pid_file}")
    return 0


def main() -> int:
    load_dotenv(find_dotenv())
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")

    parser = argparse.ArgumentParser(
        description="Cloud bot — best paper profile, 24/7 VPS ready",
    )
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="Run backtest instead of live paper loop",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Start the 24/7 cloud bot loop",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print cloud bot status and config summary",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="Stop a running cloud bot loop using the stored PID",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="With --backtest: full compare vs legacy/VTI",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=365,
        help="Backtest window in days (default 365)",
    )
    parser.add_argument(
        "--max",
        action="store_true",
        help="Use maximum available history",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-download daily data before backtest",
    )
    parser.add_argument(
        "--profile",
        type=str,
        help="Override CLOUD_BOT_PROFILE (default paper_aggressive)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not start run_all.py (inspect config only)",
    )
    args = parser.parse_args()

    settings = _load_cloud_settings(args)
    logger = setup_logging(settings.log_dir)

    env = apply_best_paper_profile(
        overrides={
            "HEARTBEAT_FILE": str(settings.heartbeat_file),
            "PAPER_JOURNAL_CSV": str(settings.journal_csv),
            "PYTHONUNBUFFERED": "1",
        }
    )
    for key, val in env.items():
        os.environ.setdefault(key, val)

    logger.info(
        "cloud bot startup | profile=%s | dry_run=%s | backtest=%s | run=%s | status=%s | stop=%s",
        settings.profile,
        settings.dry_run,
        args.backtest,
        args.run,
        args.status,
        args.stop,
    )

    if args.status:
        return _print_status(settings)

    if args.stop:
        return _stop_loop(settings, logger)

    if args.backtest:
        from cloud_bot.runtime.backtest import run_compare, run_single

        if args.compare:
            return run_compare(days=args.days, use_max=args.max, refresh=args.refresh)
        return run_single(days=args.days, use_max=args.max, refresh=args.refresh)

    if args.run or not args.backtest:
        if settings.dry_run:
            logger.info("dry-run | config OK, not launching run_all.py")
            print("Cloud bot config OK (dry-run). Best paper profile applied.")
            print(f"  data: {settings.data_dir}")
            print(f"  dry_run env: {os.getenv('CLOUD_BOT_DRY_RUN')}")
            return 0

        from cloud_bot.runtime.loop import run_forever
        return run_forever(settings, logger)

    logger.error("No action selected; use --backtest, --run, or --status")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
