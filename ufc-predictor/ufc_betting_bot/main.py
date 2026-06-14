#!/usr/bin/env python3
"""UFC Betting Bot CLI — separate from crypto trading bot."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow running as `python main.py` from package dir
if __name__ == "__main__" and str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ufc_betting_bot.backtester import backtest_2025, print_backtest_summary
from ufc_betting_bot.config.settings import ensure_dirs, get_settings
from ufc_betting_bot.live_runner import run_live_dry_run
from ufc_betting_bot.modules.model_bridge import load_fights, rebuild_features, save_fights
from ufc_betting_bot.modules.odds import merge_historical_odds

BANNER = """
  +-----------------------------------------------------------+
  |        UFC BETTING BOT  |  odds · bankroll · backtest     |
  +-----------------------------------------------------------+
"""


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_merge_odds(_: argparse.Namespace) -> int:
    fights = load_fights()
    merged = merge_historical_odds(fights)
    save_fights(merged)
    y = merged["date"].dt.year
    print(f"Merged odds onto {len(merged)} fights")
    for yr in sorted(y.dropna().unique())[-3:]:
        sub = merged[y == yr]
        print(f"  {int(yr)}: {sub['f1_odds'].notna().sum()}/{len(sub)} with odds")
    return 0


def cmd_backtest_2025(args: argparse.Namespace) -> int:
    if args.refresh_odds:
        cmd_merge_odds(args)
        fights = load_fights()
        rebuild_features(fights)

    result = backtest_2025(target_year=args.year)
    print_backtest_summary(result)
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    df = run_live_dry_run()
    print(f"Generated {len(df)} signals (dry_run={not args.live})")
    if not df.empty:
        print(df[["fighter_1", "fighter_2", "bet_side", "edge", "recommended_stake", "blocked_reason"]].to_string())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="UFC betting bot")
    p.add_argument("--verbose", action="store_true")
    sub = p.add_subparsers(dest="command")

    merge = sub.add_parser("merge-odds", help="Merge historical odds into ufc-predictor fights.csv")
    merge.set_defaults(func=cmd_merge_odds)

    bt = sub.add_parser("backtest-2025", help="Run event walk-forward backtest")
    bt.add_argument("--year", type=int, default=get_settings().backtest_year)
    bt.add_argument("--refresh-odds", action="store_true", help="Merge odds + rebuild features first")
    bt.set_defaults(func=cmd_backtest_2025)

    live = sub.add_parser("live", help="Generate live bet signals (dry-run by default)")
    live.add_argument("--live", action="store_true", help="Reserved for future real execution")
    live.set_defaults(func=cmd_live)

    # Shortcuts
    p.add_argument("--merge-odds", action="store_true")
    p.add_argument("--backtest-2025", action="store_true")
    p.add_argument("--refresh-odds", action="store_true")
    p.add_argument("--year", type=int, default=get_settings().backtest_year)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    ensure_dirs()
    print(BANNER)

    if args.merge_odds:
        return cmd_merge_odds(args)
    if args.backtest_2025:
        return cmd_backtest_2025(args)
    if args.command and hasattr(args, "func"):
        return args.func(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
