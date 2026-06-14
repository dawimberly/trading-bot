"""Checklist before running the standalone SPY bot (run_spy.py).

Run: python scripts/account/preflight_spy.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config
from modules.alpaca_executor import AlpacaExecutor
from modules.data_loader import load_close_matrix
from fetch_data import fetch_daily_history


def run():
    ok = True
    print("=== SPY BOT PREFLIGHT ===\n")

    if not config.PAPER_TRADING:
        print("[FAIL] PAPER_TRADING is false. Use paper keys only.")
        ok = False
    else:
        print("[OK] Paper mode enabled")

    if config.spy_uses_separate_alpaca_account():
        print("[OK] Using separate SPY_APCA_* credentials")
    else:
        print("[WARN] Using shared APCA_* keys — do not run run_all.py on same account at 100% SPY")

    try:
        config.get_spy_alpaca_credentials()
        print("[OK] SPY Alpaca credentials found")
    except ValueError as e:
        print(f"[FAIL] {e}")
        return

    try:
        ex = AlpacaExecutor(credentials_fn=config.get_spy_alpaca_credentials)
        acct = ex.client.get_account()
        print(
            f"[OK] Alpaca connected | equity=${float(acct.equity):,.2f} "
            f"cash=${float(acct.cash):,.2f}"
        )
    except Exception as e:
        print(f"[FAIL] Alpaca connection: {e}")
        ok = False

    data = load_close_matrix()
    if config.SPY_BOT_SYMBOL not in data.columns or len(data) < config.SPY_MA_WINDOW:
        print(f"[WARN] Need {config.SPY_MA_WINDOW}+ bars for {config.SPY_BOT_SYMBOL}; refreshing daily data...")
        fetch_daily_history(max(config.BACKTEST_DAYS, config.SPY_MA_WINDOW + 50))
        data = load_close_matrix(interval="1d")
    if config.SPY_BOT_SYMBOL in data.columns and len(data) >= config.SPY_MA_WINDOW:
        print(f"[OK] {config.SPY_BOT_SYMBOL} data: {len(data)} bars")
    else:
        print(f"[FAIL] Missing {config.SPY_BOT_SYMBOL} or insufficient history")
        ok = False

    print(f"\n[OK] Signal: {config.SPY_BOT_SYMBOL} > MA{config.SPY_MA_WINDOW}")
    print(f"[OK] Allocation: {config.SPY_RISK_PER_TRADE:.0%} | MA exit: {config.SPY_EXIT_ON_MA_BREAK}")
    print(f"[OK] Logs: {config.SPY_PAPER_JOURNAL_CSV}, {config.SPY_HEARTBEAT_FILE}")
    print("\nStart bot: python run_spy.py")
    print("=== " + ("READY" if ok else "FIX ISSUES ABOVE") + " ===")


if __name__ == "__main__":
    run()
