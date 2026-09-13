"""Full safe reset + restart of the paper bot with recovery verification.

Paper-focused: preserves the live bot, kills stray paper/engine processes,
restarts the paper supervisor, then waits for a fresh heartbeat to confirm the
bot is responding again. Telegram commands recover as soon as the engine's first
cycle completes.

Run from stock-bot/:
  python scripts/recover_bot.py
  python scripts/recover_bot.py --no-verify
  python scripts/recover_bot.py --timeout 120
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("PYTHONTRADING_ROOT", str(ROOT))


def _log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover (safe reset + restart) the paper bot")
    parser.add_argument(
        "--username",
        default=os.getenv("PORTAL_USERNAME"),
        help="Portal username (defaults to last dashboard user)",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the post-restart heartbeat verification wait",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=90,
        help="Seconds to wait for a fresh heartbeat (default 90)",
    )
    args = parser.parse_args()

    from scripts.owner_reset import (
        _default_username,
        _force_clear_all_pids,
        clean_restart_paper_only,
        wait_for_paper_heartbeat,
    )
    from modules.portal_paths import bind_project_root, has_alpaca_config

    username = (args.username or _default_username()).strip().lower()
    bind_project_root(ROOT)

    print("=" * 60, flush=True)
    print(f"=== PythonTrading BOT RECOVERY (paper) — {username} ===", flush=True)
    print(f"Project root: {ROOT}", flush=True)
    print("=" * 60, flush=True)

    if not has_alpaca_config(username, "alpaca_paper"):
        _log("[ERROR] alpaca_paper Alpaca keys missing in portal — cannot recover.")
        _log("Add paper keys in the portal, then re-run: python scripts/recover_bot.py")
        return 1

    _log("Step 1/3: Clearing stale paper PID file...")
    _force_clear_all_pids(username)

    _log("Step 2/3: Killing stray paper/engine processes and restarting (live preserved)...")
    ok, msg = clean_restart_paper_only(username)
    _log(msg if ok else f"[ERROR] {msg}")
    if not ok:
        _log("Recovery FAILED at restart step. Try: python scripts/owner_reset.py")
        return 1

    if args.no_verify:
        _log("Step 3/3: Skipped (--no-verify).")
        _log("Bot restarted successfully (paper). Give it ~60s, then test /status in Telegram.")
        return 0

    _log("Step 3/3: Verifying the bot is responding (waiting for fresh heartbeat)...")
    fresh, detail = wait_for_paper_heartbeat(username, timeout_sec=args.timeout)
    _log(f"Heartbeat: {detail}")

    print("=" * 60, flush=True)
    if fresh:
        _log(">>> RECOVERY COMPLETE — Bot Status: RESPONDING <<<")
        _log("Paper bot restarted successfully. Test /status in Telegram to confirm.")
        print("=" * 60, flush=True)
        return 0

    _log(">>> RECOVERY PARTIAL — bot restarted, heartbeat not yet fresh <<<")
    _log(
        "The engine's first cycle may still be warming up. Wait ~60s and re-check "
        "the dashboard Overview tab or run: python scripts/recover_bot.py"
    )
    print("=" * 60, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
