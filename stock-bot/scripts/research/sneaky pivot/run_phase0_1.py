"""
Phase 0+1 runner.

Usage (from stock-bot/):
    python "scripts/research/sneaky pivot/run_phase0_1.py"
    python "scripts/research/sneaky pivot/run_phase0_1.py" --sanity-only
    python "scripts/research/sneaky pivot/run_phase0_1.py" --nyse-limit 8

Outputs:
    - summary stats (win rate, R-multiple, pnl bps) for equity + crypto sleeves
    - rhyme breakdown (Phase 2 preview)
    - saved trades CSV under scripts/research/sneaky pivot/output/

Research-only. Freeze-safe. Does not touch live/paper accounts.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]  # stock-bot
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness import (  # noqa: E402
    BarCache,
    DEFAULT_FEE_MODEL,
    SLEEVE_UNIVERSES,
    _ensure_rhyme_calendar,
)
from report import rhyme_breakdown, summary_stats  # noqa: E402
from sneaky_pivot_v2 import run_sneaky_pivot  # noqa: E402

OUTPUT_DIR = HERE / "output"
OUTPUT_DIR.mkdir(exist_ok=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sneaky Pivot Phase 0+1 research runner")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--refresh", action="store_true", help="Bypass disk cache / refetch")
    ap.add_argument(
        "--sanity-only",
        action="store_true",
        help="Use sanity_check universe only (skip full NYSE + crypto_vol)",
    )
    ap.add_argument(
        "--nyse-limit",
        type=int,
        default=0,
        help="Cap NYSE symbols (0 = all). Useful before full 1m fetch.",
    )
    ap.add_argument("--skip-crypto", action="store_true")
    args = ap.parse_args(argv)

    end = dt.date.today()
    start = end - dt.timedelta(days=args.days)

    cache = BarCache(refresh=args.refresh)

    # Pre-build market-wide RHYME calendar once (same classifiers as backtester).
    print("=== Building RHYME calendar (market_context) ===")
    _ensure_rhyme_calendar(start, end)

    print("\n=== Equity sleeve (nyse_momentum) ===")
    if args.sanity_only:
        equity_symbols = list(SLEEVE_UNIVERSES["sanity_check"])
    else:
        equity_symbols = list(SLEEVE_UNIVERSES["nyse_momentum"])
        if args.nyse_limit and args.nyse_limit > 0:
            equity_symbols = equity_symbols[: args.nyse_limit]
        if not equity_symbols:
            equity_symbols = list(SLEEVE_UNIVERSES["sanity_check"])

    print(f"  symbols ({len(equity_symbols)}): {', '.join(equity_symbols)}")
    cache.load(equity_symbols, start, end)
    equity_trades = run_sneaky_pivot(
        cache, equity_symbols, asset_class="equity", fees=DEFAULT_FEE_MODEL
    )
    print(summary_stats(equity_trades))
    if not equity_trades.empty:
        equity_trades.to_csv(OUTPUT_DIR / "sneaky_pivot_equity_trades.csv", index=False)
        print(rhyme_breakdown(equity_trades, cache))

    if args.sanity_only or args.skip_crypto:
        print("\nDone (crypto skipped). Research only — freeze unchanged.")
        return 0

    print("\n=== Crypto sleeve (crypto_vol) ===")
    crypto_symbols = list(SLEEVE_UNIVERSES["crypto_vol"])
    print(f"  symbols ({len(crypto_symbols)}): {', '.join(crypto_symbols)}")
    cache.load(crypto_symbols, start, end)
    crypto_trades = run_sneaky_pivot(
        cache, crypto_symbols, asset_class="crypto", fees=DEFAULT_FEE_MODEL
    )
    print(summary_stats(crypto_trades))
    if not crypto_trades.empty:
        crypto_trades.to_csv(OUTPUT_DIR / "sneaky_pivot_crypto_trades.csv", index=False)
        print(rhyme_breakdown(crypto_trades, cache))

    print("\nDone. Research only — freeze unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
