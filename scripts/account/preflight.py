"""One-shot checklist before leaving paper trading running for a month.

Run: python scripts/account/preflight.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config
from modules.alpaca_executor import AlpacaExecutor
from modules.data_loader import load_close_matrix
from fetch_data import fetch_and_store


def run():
    ok = True
    print("=== PAPER TRADING PREFLIGHT ===\n")

    if not config.PAPER_TRADING:
        print("[FAIL] PAPER_TRADING is false. Use paper keys only.")
        ok = False
    else:
        print("[OK] Paper mode enabled")

    try:
        config.get_alpaca_credentials()
        print("[OK] Alpaca credentials found")
    except ValueError as e:
        print(f"[FAIL] {e}")
        ok = False
        return

    try:
        ex = AlpacaExecutor()
        acct = ex.client.get_account()
        print(f"[OK] Alpaca connected | equity=${float(acct.equity):,.2f} cash=${float(acct.cash):,.2f}")
    except Exception as e:
        print(f"[FAIL] Alpaca connection: {e}")
        ok = False

    print("\n--- Refreshing 5m market data ---")
    try:
        fetch_and_store()
        print("[OK] fetch_data complete")
    except Exception as e:
        print(f"[WARN] fetch_data: {e}")

    data = load_close_matrix()
    if data.empty:
        print("[FAIL] market_data.db has no price matrix")
        ok = False
    else:
        print(f"[OK] Price matrix: {len(data)} rows x {len(data.columns)} symbols")

    crypto = [c for c in data.columns if config.is_crypto(c)]
    equity = [c for c in data.columns if not config.is_crypto(c)]
    print(f"[OK] Crypto columns: {len(crypto)} | Equity columns: {len(equity)}")
    if "VTI" in equity:
        print("[OK] VTI classified as equity (not crypto)")
    else:
        print("[WARN] VTI missing from data")

    print("\n--- Settings ---")
    print(f"  Risk per trade:     {config.RISK_PER_TRADE:.0%}")
    print(f"  Stop loss:          {config.STOP_LOSS_PCT:.0%}")
    print(f"  Max drawdown halt:  {config.MAX_DRAWDOWN_PCT:.0%}")
    print(f"  Max positions:      {config.MAX_OPEN_POSITIONS}")
    print(f"  Journal:            {config.PAPER_JOURNAL_CSV}")

    if ok:
        print("\n=== READY: python run_all.py ===")
    else:
        print("\n=== FIX ISSUES ABOVE BEFORE STARTING ===")
        sys.exit(1)


if __name__ == "__main__":
    run()
