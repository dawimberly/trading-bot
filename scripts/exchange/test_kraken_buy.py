"""Place a small Kraken spot test order (e.g. 0.01 ETH).

Run:  python scripts/exchange/test_kraken_buy.py --dry-run
      python scripts/exchange/test_kraken_buy.py --apply
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config


def main() -> None:
    parser = argparse.ArgumentParser(description="Kraken spot market buy test")
    parser.add_argument("--apply", action="store_true", help="Place live order")
    parser.add_argument("--dry-run", action="store_true", help="Validate only (default)")
    parser.add_argument("--volume", type=float, default=0.01, help="Base asset amount")
    parser.add_argument("--pair", default="ETHUSD", help="Kraken pair (default ETHUSD)")
    args = parser.parse_args()

    if args.apply and not config.ALLOW_KRAKEN_TRADING:
        print("Set ALLOW_KRAKEN_TRADING=yes in .env before --apply (live order).")
        sys.exit(1)

    key, secret = config.get_kraken_credentials()
    if not key or not secret:
        print("Kraken credentials missing in .env")
        sys.exit(1)

    from kraken.spot import Trade

    trade = Trade(key=key, secret=secret)
    volume = str(args.volume)
    validate = not args.apply

    print(f"Pair: {args.pair} | volume: {volume} | validate: {validate}")
    try:
        result = trade.create_order(
            pair=args.pair,
            side="buy",
            ordertype="market",
            volume=volume,
            validate=validate,
        )
    except Exception as exc:
        print(f"Order failed: {exc}")
        sys.exit(1)

    print(result)
    if validate:
        print("\nDry run OK. Re-run with --apply to execute (requires ALLOW_KRAKEN_TRADING=yes).")
    else:
        print("\nLive order submitted.")


if __name__ == "__main__":
    main()
