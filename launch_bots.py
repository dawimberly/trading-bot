"""Start or stop **both** fund bots: live (~$100) + paper Sharpe chase.

Each bot is a separate run_all.py process with isolated logs under data/portal/users/.

Setup (once):
  1. Register two portal users (portal.py): e.g. you-live and you-paper
  2. you-live  → live Alpaca keys, Paper trading OFF, Allow live ON
  3. you-paper → paper Alpaca keys, Paper trading ON
  4. Copy data/portal/fund_pair.json.example → fund_pair.json and edit names

Run:
    python launch_bots.py
    python launch_bots.py --status
    python launch_bots.py --stop

Or: .\\launch_both.bat

Shared: market_data.db (run fetch_data once)
Separate per user: heartbeat, journal, bot.log, wisdom files, .env
"""

from __future__ import annotations

import argparse
import sys

from modules.fund_config import (
    ensure_example_file,
    is_root_slot,
    load_fund_pair,
    root_env_path,
    save_fund_pair,
    validate_fund_pair,
)
from modules.portal_bot import (
    bot_env_running,
    bot_running,
    start_bot,
    start_bot_env,
    stop_bot,
    stop_bot_env,
)


def _resolve_pair(args) -> tuple[str, str]:
    live, paper = load_fund_pair()
    if args.live_user:
        live = args.live_user.strip().lower()
    if args.paper_user:
        paper = args.paper_user.strip().lower()
    if args.init_pair and live and paper:
        save_fund_pair(live, paper)
        print(f"Saved fund pair: live={live} paper={paper}")
    return live, paper


def _start_side(label: str, name: str, *, paper_chase: bool) -> tuple[bool, str]:
    if is_root_slot(name):
        return start_bot_env(root_env_path(), "paper" if paper_chase else "live", paper_chase=paper_chase)
    return start_bot(name)


def _side_running(name: str, *, paper: bool) -> bool:
    if is_root_slot(name):
        return bot_env_running("paper" if paper else "live")
    return bot_running(name)


def _side_data_path(name: str, *, paper: bool) -> str:
    if is_root_slot(name):
        slot = "paper" if paper else "live"
        return f"data/fund/{slot}/bot_heartbeat.json"
    return f"data/portal/users/{name}/bot_heartbeat.json"


def cmd_start(live: str, paper: str) -> int:
    errors = validate_fund_pair(live, paper)
    if errors:
        for err in errors:
            print(f"ERROR: {err}")
        ensure_example_file()
        print("\nSee data/portal/fund_pair.json.example and README (Dual fund bots).")
        print("Tip: paper keys still in project .env → use \"paper_user\": \"@root\" in fund_pair.json")
        return 1

    results: list[tuple[bool, str]] = []
    for label, user, chase in (("LIVE", live, False), ("PAPER", paper, True)):
        ok, msg = _start_side(label, user, paper_chase=chase)
        results.append((ok, msg))
        print(f"[{label}] {msg}")

    if all(ok for ok, _ in results):
        print("\nBoth bots running. Heartbeats:")
        print(f"  live  → {_side_data_path(live, paper=False)}")
        print(f"  paper → {_side_data_path(paper, paper=True)}")
        return 0
    return 1


def cmd_stop(live: str, paper: str) -> int:
    for label, user, is_paper in (("LIVE", live, False), ("PAPER", paper, True)):
        if not user:
            continue
        if is_root_slot(user):
            ok, msg = stop_bot_env("paper" if is_paper else "live")
        else:
            ok, msg = stop_bot(user)
        print(f"[{label}] {msg}")
    return 0


def cmd_status(live: str, paper: str) -> int:
    if not live or not paper:
        print("Fund pair not configured. Set fund_pair.json or FUND_LIVE_USER / FUND_PAPER_USER.")
        return 1
    for label, user, is_paper in (("LIVE", live, False), ("PAPER", paper, True)):
        running = _side_running(user, paper=is_paper)
        print(f"[{label}] user={user} running={running} data={_side_data_path(user, paper=is_paper)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Live + paper dual-bot launcher")
    parser.add_argument("--live-user", help="Portal username for live ~$100 fund")
    parser.add_argument("--paper-user", help="Portal username for paper Sharpe chase")
    parser.add_argument("--init-pair", action="store_true", help="Save --live-user/--paper-user to fund_pair.json")
    parser.add_argument("--status", action="store_true", help="Show running state")
    parser.add_argument("--stop", action="store_true", help="Stop both bots")
    args = parser.parse_args()

    live, paper = _resolve_pair(args)
    if args.status:
        return cmd_status(live, paper)
    if args.stop:
        return cmd_stop(live, paper)
    return cmd_start(live, paper)


if __name__ == "__main__":
    sys.exit(main())
