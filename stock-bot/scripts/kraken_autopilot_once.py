"""Run one Kraken autopilot cycle (dry-run by default).

  python scripts/kraken_autopilot_once.py
  python scripts/kraken_autopilot_once.py --live   # requires ALLOW_KRAKEN_TRADING + KRAKEN_DRY_RUN=false
"""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from modules.data_loader import load_close_matrix
from modules.crypto_vol_gate import crypto_trading_allowed
from modules.game_plan import run_game_plan_cycle
from modules.kraken_autopilot import format_autopilot_line, run_kraken_autopilot
from modules.macro_signals import evaluate, load_daily_matrix
from modules.wisdom_sentiment import resolve_wisdom_regime


def main() -> None:
    parser = argparse.ArgumentParser(description="Kraken autopilot one-shot")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Set KRAKEN_DRY_RUN=false for this run (still needs ALLOW_KRAKEN_TRADING)",
    )
    args = parser.parse_args()

    if args.live:
        config.KRAKEN_DRY_RUN = False

    data = load_close_matrix()
    wisdom = resolve_wisdom_regime(data)
    regime = wisdom["regime"]
    vol = wisdom["volatility"]
    daily = load_daily_matrix(days=450)
    gp_signals = evaluate(daily, regime) if daily is not None and not daily.empty else {}
    crypto_gate = crypto_trading_allowed(vol, regime)
    gp_result = {
        "enabled": config.GAME_PLAN_ENABLED,
        "signals": gp_signals,
        "actions": [],
    }

    if not config.KRAKEN_AUTOPILOT_ENABLED:
        print("Tip: set KRAKEN_AUTOPILOT_ENABLED=true in .env (run_all uses same flag).")
    result = run_kraken_autopilot(
        wisdom=wisdom,
        gp_signals=gp_signals,
        gp_result=gp_result,
        crypto_gate=crypto_gate,
        data=data,
        regime=regime,
        now=datetime.datetime.now(),
        pair_cooldown={},
        market_open=True,
    )
    print(format_autopilot_line(result))
    import json

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
