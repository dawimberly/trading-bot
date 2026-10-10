"""Dry-run portfolio guards: concentration (8%), auto-dust (<$10), max 25 tickers.

Usage:
  # Offline mock (no broker) — default
  python scripts/analysis/dry_run_executor_guards.py

  # Against paper Alpaca with DRY_RUN=1 (no orders submitted)
  python scripts/analysis/dry_run_executor_guards.py --paper
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))


def _mock_dry_run() -> dict:
    import config
    from modules.alpaca_executor import AlpacaExecutor

    def pos(symbol: str, qty: float, price: float):
        return SimpleNamespace(
            symbol=symbol,
            qty=qty,
            current_price=price,
            avg_entry_price=price,
            market_value=qty * price,
        )

    equity = 100_000.0
    positions = [
        pos("VTI", 300, 200.0),  # core — exempt
        pos("FAT", 100, 100.0),  # 10% → trim
        pos("DUST1", 0.1, 40.0),  # $4 dust
        pos("DUST2", 2.0, 4.0),  # $8 dust
        pos("OK", 50, 100.0),  # 5% — under cap
        *[pos(f"T{i:02d}", 1, 80.0) for i in range(24)],  # fill toward 25
    ]
    ex = AlpacaExecutor.__new__(AlpacaExecutor)
    ex.dry_run = True
    ex.paper = True
    ex._positions = positions
    ex._account = SimpleNamespace(equity=equity, cash=20_000.0)
    ex.refresh_cache = lambda: None
    ex._get_positions = lambda: list(ex._positions)
    ex._account_equity = lambda: equity
    ex._find_position = lambda symbol: next(
        (
            p
            for p in ex._positions
            if config.normalize_symbol(p.symbol) == config.normalize_symbol(symbol)
        ),
        None,
    )
    ex._normalize_pos_symbol = AlpacaExecutor._normalize_pos_symbol
    ex._min_notional = lambda: 1.0

    summary = ex.enforce_portfolio_guards(dry_run=True)
    summary["mode"] = "mock"
    summary["would_block_new"] = ex._blocks_new_active_ticker("NEWTICKER")
    summary["sample_cap"] = {
        "FAT_buy_2000": ex._apply_concentration_cap("FAT", 2000.0),
        "OK_buy_2000": ex._apply_concentration_cap("OK", 2000.0),
    }
    return summary


def _paper_dry_run() -> dict:
    os.environ["DRY_RUN"] = "1"
    import config
    from modules.alpaca_executor import AlpacaExecutor

    config.PAPER_TRADING = True
    ex = AlpacaExecutor(paper=True)
    assert ex.dry_run, "expected DRY_RUN=1"
    summary = ex.enforce_portfolio_guards(dry_run=True)
    summary["mode"] = "paper_dry_run"
    summary["would_block_new"] = ex._blocks_new_active_ticker("NEWTICKER")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run executor portfolio guards")
    parser.add_argument(
        "--paper",
        action="store_true",
        help="Connect to paper Alpaca with DRY_RUN (no orders)",
    )
    args = parser.parse_args()
    summary = _paper_dry_run() if args.paper else _mock_dry_run()

    print("=== Executor portfolio guards (DRY RUN) ===")
    print(
        f"mode={summary.get('mode')} | active={summary['active_count']}/"
        f"{summary['max_active_tickers']} | per_name={summary['per_name_max_pct']:.0%} | "
        f"dust_thresh=${summary['auto_dust_max_notional']:.0f}"
    )
    print(f"would_block_new_ticker={summary.get('would_block_new')}")
    if summary.get("sample_cap"):
        print(f"sample_caps={summary['sample_cap']}")
    print(f"concentration_trims ({len(summary['concentration_trims'])}):")
    for row in summary["concentration_trims"][:10]:
        print(
            f"  {row['symbol']}: would sell ${row['notional']:.2f} "
            f"(val ${row['market_value']:.2f}, {row['pct_of_equity']:.1%} eq)"
        )
    print(f"dust_actions ({len(summary['dust_actions'])}):")
    for row in summary["dust_actions"][:15]:
        print(
            f"  {row['symbol']}: {row['status']} qty={row['qty']} "
            f"notional=${row['notional']:.2f}"
        )

    out = ROOT / "scripts" / "analysis" / "dry_run_executor_guards.json"
    out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"Saved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
