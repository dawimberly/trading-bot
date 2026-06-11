"""Cloud bot entrypoint — 24/7 paper trading, backtest, status, or stop."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import signal
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cloud_bot.config.env_loader import (  # noqa: E402
    apply_runtime_env,
    build_runtime_env,
    load_cloud_dotenv,
)
from cloud_bot.config.profile import BEST_PAPER_ENV  # noqa: E402
from cloud_bot.config.settings import CloudSettings, load_settings  # noqa: E402
from cloud_bot.modules.stack import STACK_FEATURES  # noqa: E402
from cloud_bot.runtime.logging_setup import setup_logging  # noqa: E402


def _load_cloud_settings(args: argparse.Namespace) -> CloudSettings:
    if args.profile:
        os.environ["CLOUD_BOT_PROFILE"] = args.profile
    if args.dry_run:
        os.environ["CLOUD_BOT_DRY_RUN"] = "true"
    elif args.run:
        os.environ["CLOUD_BOT_DRY_RUN"] = "false"

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


def _check_running(pid_file: Path) -> tuple[str, int | None]:
    if not pid_file.exists():
        return "no", None
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except ValueError:
        return "invalid pid", None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "stale", pid
    except PermissionError:
        return "access denied", pid
    return f"yes ({pid})", pid


def _stop_loop(settings: CloudSettings, logger) -> int:
    state, pid = _check_running(settings.pid_file)
    if state == "no":
        print("No PID file found; is the cloud bot loop running?")
        return 1
    if pid is None:
        print(f"PID file invalid: {settings.pid_file}")
        settings.pid_file.unlink(missing_ok=True)
        return 1
    if state == "stale":
        print("Stale PID file removed (process not running).")
        settings.pid_file.unlink(missing_ok=True)
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
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            print("Cloud bot stopped.")
            settings.pid_file.unlink(missing_ok=True)
            return 0
        time.sleep(0.5)
    print("Process still running after 20s; send SIGKILL manually if needed.")
    return 1


def _print_stack_flags() -> None:
    on = [k for k, v in BEST_PAPER_ENV.items() if v.lower() in ("1", "true", "yes")]
    print("best_paper_stack:")
    for feature in STACK_FEATURES:
        print(f"  - {feature}")
    print(f"env_flags_on: {len(on)} (see cloud_bot/config/profile.py)")


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
    print(f"pid_file:         {settings.pid_file}")
    print(f"run_all exists:   {settings.run_all_script.is_file()}")
    _print_stack_flags()
    heartbeat = _load_heartbeat(settings.heartbeat_file)
    running, _ = _check_running(settings.pid_file)
    print(f"running:          {running}")
    if heartbeat:
        print(f"last_heartbeat:   {heartbeat.get('timestamp')}")
        print(f"equity:           ${float(heartbeat.get('equity', 0)):,.2f}")
        print(f"cash:             ${float(heartbeat.get('cash', 0)):,.2f}")
        print(f"regime:           {heartbeat.get('regime', '—')}")
        print(f"halted:           {heartbeat.get('halted', False)}")
        print(f"paper:            {heartbeat.get('paper', False)}")
        print(f"sleeves:          {', '.join(_active_sleeves(heartbeat)) or 'none'}")
        if settings.heartbeat_file.exists():
            print(
                f"heartbeat_age:    "
                f"{int(time.time() - settings.heartbeat_file.stat().st_mtime)}s"
            )
        print(f"dynamic_vol_score:{heartbeat.get('dynamic_vol_score', '—')}")
        caps = heartbeat.get("sleeve_caps") or {}
        print(f"vti_target_pct:   {float(caps.get('vti_core', 0)):.2%}")
    else:
        print("heartbeat:        none")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cloud bot — Best Paper stack, 24/7 VPS production entry",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python runtime/main.py --backtest --days 365 --compare
  python runtime/main.py --run
  python runtime/main.py --status
  python runtime/main.py --stop
  python runtime/main.py --dry-run
        """.strip(),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--backtest", action="store_true", help="Run backtest")
    mode.add_argument("--run", action="store_true", help="Start 24/7 supervisor loop")
    mode.add_argument("--status", action="store_true", help="Print status summary")
    mode.add_argument("--stop", action="store_true", help="Stop running loop (SIGTERM)")
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate config; do not start run_all.py",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="With --backtest: full compare vs legacy / live-parity / VTI",
    )
    parser.add_argument("--days", type=int, default=365, help="Backtest window (default 365)")
    parser.add_argument("--max", action="store_true", help="Use maximum available history")
    parser.add_argument("--refresh", action="store_true", help="Re-download daily data")
    parser.add_argument("--profile", type=str, help="Override CLOUD_BOT_PROFILE")
    return parser.parse_args()


def main() -> int:
    env_path = load_cloud_dotenv()
    args = _parse_args()

    if not any(
        (args.backtest, args.run, args.status, args.stop, args.dry_run)
    ):
        print("No mode selected. Use --run, --backtest, --status, --stop, or --dry-run.")
        print("Run with -h for examples.")
        return 2

    settings = _load_cloud_settings(args)
    runtime_env = build_runtime_env(settings)
    apply_runtime_env(runtime_env)
    logger = setup_logging(settings.log_dir)

    if env_path:
        logger.info("loaded env from %s", env_path)
    logger.info(
        "cloud bot | profile=%s | dry_run=%s | backtest=%s | run=%s | status=%s | stop=%s",
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

    if args.dry_run:
        logger.info("dry-run | config OK, not launching run_all.py")
        print("Cloud bot config OK (dry-run). Best paper profile applied.")
        print(f"  env file:    {env_path or '(none — using defaults)'}")
        print(f"  data dir:    {settings.data_dir}")
        print(f"  heartbeat:   {settings.heartbeat_file}")
        print(f"  journal:     {settings.journal_csv}")
        print(f"  dry_run:     {settings.dry_run}")
        _print_stack_flags()
        return 0

    if args.run:
        from cloud_bot.runtime.loop import run_forever

        return run_forever(settings, logger, runtime_env=runtime_env)

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
