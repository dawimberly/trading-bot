"""
Phase 0+1 runner -- 365d config.

RESEARCH SIDECAR ONLY. NOT WIRED TO PAPER OR LIVE.
Promotion of any gate (block-C, D+E, etc.) requires:
    1. OOS validation across a split that includes ALL observed regimes
       in both train and holdout (not just C/D as in the 180d run)
    2. At least some A/B exposure, or an explicit "C/D/E-only validated"
       label if A/B still doesn't appear
    3. A conscious, separate freeze-lift decision -- this script produces
       evidence, not a wiring decision
    4. A separate position-sizing design review before anything touches
       run_paper_bot / .env -- a validated signal is not a validated
       sizing rule

Fidelity notes (carry forward from 90d/180d -- do not silently change):
    - RHYME: get_price_sentiment -> get_volatility -> get_market_regime
      (expanding-window, same as backtester / harness._ensure_rhyme_calendar)
    - Fees: FeeModel equity_bps=1.0, crypto_bps=5.0, slippage_bps=2.0
    - Universe: same 8 mega-caps as 90d/180d series (fixed list below)
    - Cache: versioned by start/end so 365d does not overwrite 90d/180d

Usage (from stock-bot/):
    python "scripts/research/sneaky pivot/run_phase0_1_365d.py"
    python "scripts/research/sneaky pivot/run_phase0_1_365d.py" --skip-crypto
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness import (  # noqa: E402
    DEFAULT_FEE_MODEL,
    FeeModel,
    BarCache,
    SLEEVE_UNIVERSES,
    _ensure_rhyme_calendar,
)
from report import rhyme_breakdown, summary_stats  # noqa: E402
from sneaky_pivot_v2 import run_sneaky_pivot  # noqa: E402

REPORT_ONLY = True  # hard flag -- CSVs only, never touches paper/live

# Locked series universe (same first-8 as 90d/180d get_nyse_universe_fixed).
SERIES_EQUITY_8 = [
    "AAPL",
    "MSFT",
    "NVDA",
    "AMD",
    "GOOGL",
    "AMZN",
    "TSLA",
    "META",
]

OUTPUT_DIR = HERE / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# Assert fee fidelity vs Phase 0 defaults
assert DEFAULT_FEE_MODEL.equity_bps == 1.0
assert DEFAULT_FEE_MODEL.crypto_bps == 5.0
assert DEFAULT_FEE_MODEL.slippage_bps == 2.0


def main(argv: list[str] | None = None) -> int:
    if not REPORT_ONLY:
        raise RuntimeError("REPORT_ONLY is False -- refusing to run (safety)")

    ap = argparse.ArgumentParser(description="Sneaky Pivot Phase 0+1 -- 365d research sidecar")
    ap.add_argument("--skip-crypto", action="store_true")
    ap.add_argument(
        "--refresh",
        action="store_true",
        help="Refetch bars even if versioned 365d cache exists",
    )
    args = ap.parse_args(argv)

    end = dt.date.today()
    start = end - dt.timedelta(days=365)

    fees: FeeModel = DEFAULT_FEE_MODEL
    equity_symbols = list(SERIES_EQUITY_8)
    # Sanity: series list must match fixed-universe head
    fixed = list(SLEEVE_UNIVERSES["nyse_momentum"])[:8]
    if equity_symbols != fixed:
        print(
            f"WARNING: SERIES_EQUITY_8 {equity_symbols} != "
            f"nyse_momentum[:8] {fixed} -- using SERIES_EQUITY_8 for fidelity"
        )

    print("=" * 64)
    print("Phase 0+1 -- 365d RESEARCH SIDECAR (REPORT_ONLY=True)")
    print("=" * 64)
    print(f"  window: {start} -> {end}")
    print(f"  equity: {', '.join(equity_symbols)}")
    print(
        f"  fees: equity_bps={fees.equity_bps} crypto_bps={fees.crypto_bps} "
        f"slippage_bps={fees.slippage_bps}"
    )
    print("  rhyme: market_context get_price_sentiment/get_volatility/get_market_regime")
    print("  outputs: *_365d.csv (does not overwrite 90d/180d archives)")
    print("  cache: versioned {sym}_{start}_{end}.pkl")
    print("  NOT wired to paper/live. Freeze unchanged.")
    print()

    cache = BarCache(refresh=args.refresh, versioned=True)

    print("=== Building RHYME calendar (market_context) ===")
    _ensure_rhyme_calendar(start, end)

    print("\n=== Equity sleeve (series 8) ===")
    cache.load(equity_symbols, start, end)
    equity_trades = run_sneaky_pivot(
        cache, equity_symbols, asset_class="equity", fees=fees
    )
    print(summary_stats(equity_trades))
    eq_path = OUTPUT_DIR / "sneaky_pivot_equity_trades_365d.csv"
    if not equity_trades.empty:
        equity_trades.to_csv(eq_path, index=False)
        print(f"  wrote {eq_path.name}")
        print(rhyme_breakdown(equity_trades, cache))

        from harness import label_rhyme_for_universe
        import pandas as pd

        days = sorted(pd.to_datetime(equity_trades["session_date"]).dt.date.unique().tolist())
        rmap = label_rhyme_for_universe(days)
        # Letters that actually appear on trade days (not just calendar)
        merged = equity_trades.copy()
        merged["session_date"] = pd.to_datetime(merged["session_date"]).dt.date
        merged = merged.merge(rmap, left_on="session_date", right_on="day", how="left")
        present = sorted(merged["rhyme"].dropna().unique().tolist())
        missing = [x for x in ("A", "B", "C", "D", "E") if x not in present]
        print(f"\n  RHYME letters on trade days: {present}")
        if missing:
            print(
                f"  NOTE: missing {missing} — any gate result is "
                f"'validated on {present} only' until A/B appear or you "
                f"explicitly accept C/D/E-only validation."
            )

    if args.skip_crypto:
        print("\nDone (crypto skipped). REPORT_ONLY -- freeze unchanged.")
        return 0

    print("\n=== Crypto sleeve (crypto_vol) ===")
    crypto_symbols = list(SLEEVE_UNIVERSES["crypto_vol"])
    print(f"  symbols: {', '.join(crypto_symbols)}")
    cache.load(crypto_symbols, start, end)
    crypto_trades = run_sneaky_pivot(
        cache, crypto_symbols, asset_class="crypto", fees=fees
    )
    print(summary_stats(crypto_trades))
    if not crypto_trades.empty:
        cx_path = OUTPUT_DIR / "sneaky_pivot_crypto_trades_365d.csv"
        crypto_trades.to_csv(cx_path, index=False)
        print(f"  wrote {cx_path.name}")
        print(rhyme_breakdown(crypto_trades, cache))

    print("\nDone. REPORT_ONLY -- freeze unchanged. No paper/live wiring.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
