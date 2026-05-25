"""Rebalance paper/live account toward fund sleeve targets.

Run:  python scripts/account/rebalance_fund.py              # plan only
      python scripts/account/rebalance_fund.py --apply
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config
from modules.alpaca_executor import AlpacaExecutor
from modules.data_loader import load_close_matrix
from modules.holdings_rebalance import rebalance_to_targets
from modules.portfolio_manager import PortfolioManager
from modules.wisdom_sentiment import resolve_wisdom_regime


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebalance to SPY/crypto/NYSE sleeve targets")
    parser.add_argument("--apply", action="store_true", help="Execute orders (default: dry run)")
    args = parser.parse_args()

    data = load_close_matrix()
    if data.empty or len(data) < 20:
        raise SystemExit("Need market data — run fetch_data.py first.")

    ex = AlpacaExecutor()
    pm = PortfolioManager()
    wisdom = resolve_wisdom_regime(data)
    regime = wisdom["regime"]
    vol = wisdom["volatility"]

    # Equity buys need session open; crypto sells work 24/7
    try:
        clock = ex.client.get_clock()
        market_open = bool(clock.is_open)
    except Exception:
        market_open = True

    result = rebalance_to_targets(
        ex,
        data,
        regime=regime,
        volatility=vol,
        market_open=market_open,
        portfolio_manager=pm,
        dry_run=not args.apply,
    )

    print(json.dumps(result, indent=2))
    if not args.apply:
        print("\nDry run. Re-run with --apply to execute.")


if __name__ == "__main__":
    main()
