"""Reconcile Alpaca holdings with ledger and trim over-cap sleeves.

Run:  python scripts/account/reconcile_holdings.py          # audit only
      python scripts/account/reconcile_holdings.py --apply
      python scripts/account/reconcile_holdings.py --apply --trim
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config
from modules.alpaca_executor import AlpacaExecutor
from modules.holdings_reconcile import holdings_audit, reconcile
from modules.portfolio_manager import PortfolioManager


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile Alpaca vs ledger vs sleeve caps")
    parser.add_argument("--apply", action="store_true", help="Rebuild ledger from Alpaca")
    parser.add_argument(
        "--trim",
        action="store_true",
        help="Sell excess above sleeve caps (paper/live orders)",
    )
    args = parser.parse_args()

    ex = AlpacaExecutor()
    pm = PortfolioManager()

    if not args.apply and not args.trim:
        print(json.dumps(holdings_audit(ex), indent=2))
        print("\nDry run. Use --apply to rebuild ledger, --trim to sell over-cap sleeves.")
        return

    result = reconcile(
        ex,
        pm,
        rebuild=args.apply or args.trim,
        trim=args.trim,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
