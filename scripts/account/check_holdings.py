"""List Alpaca positions vs fund sleeve caps (pre-existing holdings audit).

Run: python scripts/account/check_holdings.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config
from modules.alpaca_executor import AlpacaExecutor
from modules.holdings_reconcile import holdings_audit
from modules.portfolio_manager import PortfolioManager


def main() -> None:
    ex = AlpacaExecutor()
    audit = holdings_audit(ex)
    print("=== ALPACA HOLDINGS AUDIT ===")
    print(f"Paper: {config.PAPER_TRADING}")
    print(f"Equity: ${audit['equity']:,.2f}  Cash: ${audit['cash']:,.2f}")

    positions = audit["positions"]
    print(f"\nOpen positions: {len(positions)}")
    if not positions:
        print("  (none — clean slate for run_all.py)")
    else:
        for p in positions:
            print(f"  {p['symbol']:12} -> {p['universe']:10} value=${p['value']:,.2f}")

    s = audit["sleeves"]
    o = audit["over_cap"]
    print("\nSleeve exposure:")
    print(f"  SPY:    ${s['spy_value']:,.2f} / ${s['spy_cap']:,.2f}  over ${o['spy']:,.2f}")
    print(f"  Crypto: ${s['crypto_value']:,.2f} / ${s['crypto_cap']:,.2f}  over ${o['crypto']:,.2f}")
    print(f"  NYSE:   ${s['nyse_value']:,.2f} / ${s['nyse_cap']:,.2f}  over ${o['nyse']:,.2f}")

    ledger = PortfolioManager().get_open_positions()
    print(f"\nLocal ledger open pairs: {len(ledger)}")
    for pair in ledger:
        print(f"  {pair}")

    min_n = config.effective_min_notional(audit["equity"])
    if o["crypto"] >= min_n or o["spy"] >= min_n or o["nyse"] >= min_n:
        print(
            "\nFix: python scripts/account/reconcile_holdings.py --apply --trim"
        )


if __name__ == "__main__":
    main()
