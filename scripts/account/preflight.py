"""One-shot checklist before leaving paper trading running for a month.

Run: python scripts/account/preflight.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config
from modules.alpaca_executor import AlpacaExecutor
from modules.data_loader import load_close_matrix
from modules.macro_signals import ensure_macro_daily, evaluate, load_daily_matrix
from modules.wisdom_sentiment import MODES
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
        symbols = list(config.equity_universe())
        if config.GAME_PLAN_ENABLED:
            symbols.extend(s for s in config.live_metal_universe() if s not in symbols)
        fetch_and_store(symbols)
        print(f"[OK] fetch_data complete ({len(symbols)} equity tickers incl. metals)")
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

    if config.GAME_PLAN_ENABLED:
        print("\n--- Game plan (game_plan_gld_slv_cper) ---")
        blend = config.metal_blend_weights()
        alloc = config.fund_allocation_pct()
        print(
            f"[OK] Enabled | metal {alloc['metal']:.0%} "
            f"({blend['GLD']:.0%} GLD / {blend['SLV']:.0%} SLV / {blend['CPER']:.0%} CPER)"
        )
        print(
            f"     Cash buffer {alloc['cash_buffer']:.0%} | "
            f"stress cash {config.STRESS_CASH_PCT:.0%} | "
            f"yield gate {'ON' if config.YIELD_GATE_ENABLED else 'OFF'}"
        )
        for sym in config.live_metal_universe():
            if sym in data.columns or sym in config.UNIVERSE:
                print(f"[OK] {sym} in universe")
            else:
                print(f"[WARN] {sym} missing from price data")
        try:
            ensure_macro_daily(refresh=True)
            daily = load_daily_matrix(days=450)
            sig = evaluate(daily, "PREFLIGHT")
            if sig.get("ok"):
                print(
                    f"[OK] Macro signals | stress={sig.get('stress')} "
                    f"yield_gate={sig.get('yield_gate')} bond_stress={sig.get('bond_stress')}"
                )
            else:
                print("[WARN] Macro daily data thin — game plan may skip until history fills")
        except Exception as e:
            print(f"[WARN] Macro daily bootstrap: {e}")
    else:
        print("\n[INFO] GAME_PLAN_ENABLED=false — baseline fund only")

    print("\n--- Settings ---")
    wisdom_mode = config.WISDOM_MODE.strip().lower()
    if wisdom_mode not in MODES:
        print(f"  Wisdom mode:        {config.WISDOM_MODE} [INVALID — use one of: {', '.join(MODES)}]")
        ok = False
    else:
        print(f"  Wisdom mode:        {wisdom_mode}")
        if wisdom_mode == "governor" and not config.GAME_PLAN_ENABLED:
            print("  [WARN] governor mode works best with GAME_PLAN_ENABLED=true")
    print(f"  Risk per trade:     {config.RISK_PER_TRADE:.0%}")
    print(f"  Stop loss:          {config.STOP_LOSS_PCT:.0%}")
    print(f"  Max drawdown halt:  {config.MAX_DRAWDOWN_PCT:.0%}")
    print(f"  Kraken max names:   {config.KRAKEN_MAX_POSITIONS} (cleanup only)")
    print(f"  Journal:            {config.PAPER_JOURNAL_CSV}")

    if ok:
        print("\n=== READY: python run_all.py ===")
    else:
        print("\n=== FIX ISSUES ABOVE BEFORE STARTING ===")
        sys.exit(1)


if __name__ == "__main__":
    run()
