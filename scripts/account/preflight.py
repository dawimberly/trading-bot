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
from modules.wisdom_sentiment import LIVE_MODES, DEPRECATED_MODES, MODES
from fetch_data import fetch_and_store


def run():
    ok = True
    live = not config.PAPER_TRADING
    title = "LIVE TRADING PREFLIGHT" if live else "PAPER TRADING PREFLIGHT"
    print(f"=== {title} ===\n")

    if live:
        if config.ALLOW_LIVE_TRADING:
            print("[OK] Live mode enabled (ALLOW_LIVE_TRADING=yes)")
        else:
            print("[FAIL] PAPER_TRADING=false but ALLOW_LIVE_TRADING is not yes")
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

    acct = None
    try:
        ex = AlpacaExecutor()
        acct = ex.client.get_account()
        mode = "LIVE" if live else "PAPER"
        print(
            f"[OK] Alpaca connected ({mode}) | "
            f"equity=${float(acct.equity):,.2f} cash=${float(acct.cash):,.2f}"
        )
        if live and float(acct.equity) < 500:
            print(
                f"[INFO] Small account (${float(acct.equity):,.2f}) — "
                f"order min ${config.effective_min_notional(float(acct.equity)):.2f}, "
                f"2% chunk ${float(acct.equity) * config.RISK_PER_TRADE:.2f}"
            )
    except Exception as e:
        hint = ""
        if live and "401" in str(e):
            hint = " (check live keys + PAPER_TRADING=false)"
        elif not live and "401" in str(e):
            hint = " (live keys need PAPER_TRADING=false; paper keys need PAPER_TRADING=true)"
        print(f"[FAIL] Alpaca connection: {e}{hint}")
        ok = False

    print("\n--- Refreshing 5m market data ---")
    try:
        symbols = list(config.equity_universe())
        if config.metal_sleeve_enabled():
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

    if config.game_plan_active():
        alloc = config.fund_allocation_pct()
        if config.GAME_PLAN_YIELD_GATE_ONLY:
            print("\n--- Game plan (yield-gate-only) ---")
            print(
                f"[OK] Enabled | full sleeve caps | yield gate "
                f"{'ON' if config.YIELD_GATE_ENABLED else 'OFF'}"
            )
            print(f"     Cash buffer {alloc['cash_buffer']:.0%} | metal sleeve off")
        else:
            print("\n--- Game plan (game_plan_gld_slv_cper) ---")
            blend = config.metal_blend_weights()
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
        print("\n[INFO] game_plan off — baseline fund only")

    print()
    config.print_recommended_stack_flags()

    print("\n--- Settings ---")
    wisdom_mode = config.WISDOM_MODE.strip().lower()
    if wisdom_mode not in MODES:
        print(f"  Wisdom mode:        {config.WISDOM_MODE} [INVALID — use one of: {', '.join(LIVE_MODES)}]")
        ok = False
    else:
        print(f"  Wisdom mode:        {wisdom_mode}")
        if wisdom_mode in DEPRECATED_MODES:
            print("  [WARN] mode is deprecated — maps to dynamic at runtime")
        elif wisdom_mode == "dynamic" and not config.AUTO_DYNAMIC_ENABLED:
            print("  [WARN] AUTO_DYNAMIC_ENABLED=false — dynamic runs price-only")
    print(f"  Risk per trade:     {config.RISK_PER_TRADE:.0%}")
    try:
        live_eq = float(acct.equity)
        print(
            f"  Order sizing:       min ${config.effective_min_notional(live_eq):.2f} "
            f"| max ${config.effective_max_notional_per_order(live_eq):.2f} "
            f"(ref ${config.REFERENCE_EQUITY:,.0f})"
        )
        print(
            f"  At $100 account:   min ${config.effective_min_notional(100):.2f} "
            f"| 2% chunk ${100 * config.RISK_PER_TRADE:.2f}"
        )
    except Exception:
        pass
    print(f"  Stop loss:          {config.STOP_LOSS_PCT:.0%}")
    print(f"  Max drawdown halt:  {config.MAX_DRAWDOWN_PCT:.0%}")
    print(f"  Kraken max names:   {config.KRAKEN_MAX_POSITIONS} (cleanup only)")
    if config.KRAKEN_AUTOPILOT_ENABLED and not config.KRAKEN_DRY_RUN:
        print(
            f"  Kraken autopilot:   ON (live trades, budget ${config.KRAKEN_CYCLE_BUDGET_USD:.0f}/cycle)"
        )
        if live:
            print("  [WARN] Kraken + Alpaca both live — set KRAKEN_AUTOPILOT_ENABLED=false for Alpaca-only")
    else:
        print("  Kraken autopilot:   off or dry-run")
    print(f"  Journal:            {config.PAPER_JOURNAL_CSV}")

    if ok:
        if live:
            print("\n=== READY (LIVE): python run_all.py ===")
        else:
            print("\n=== READY: python run_all.py ===")
    else:
        print("\n=== FIX ISSUES ABOVE BEFORE STARTING ===")
        sys.exit(1)


if __name__ == "__main__":
    run()
