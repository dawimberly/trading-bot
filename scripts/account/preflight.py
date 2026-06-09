"""One-shot checklist before leaving paper trading running for a month.

Run: python scripts/account/preflight.py
"""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config
from modules import alerts
from modules.alpaca_executor import AlpacaExecutor
from modules.data_loader import load_close_matrix
from modules.macro_signals import ensure_macro_daily, evaluate, load_daily_matrix
from modules.wisdom_sentiment import LIVE_MODES, DEPRECATED_MODES, MODES
from fetch_data import fetch_and_store

DB_PATH = Path(__file__).resolve().parents[2] / "market_data.db"


def _data_refresh_ok(data) -> tuple[bool, str]:
    if data.empty:
        return False, "market_data.db has no price rows — run fetch_data.py"
    if not DB_PATH.exists():
        return False, "market_data.db missing — run fetch_data.py"
    mtime_h = (time.time() - DB_PATH.stat().st_mtime) / 3600
    if mtime_h > 24:
        return False, f"DB file {mtime_h:.1f}h old — run fetch_data.py"
    last = data.index[-1]
    try:
        if hasattr(last, "to_pydatetime"):
            last_dt = last.to_pydatetime()
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            age_h = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
            if age_h > 48:
                return False, f"last bar ~{age_h:.0f}h old — run fetch_data.py"
            return True, f"price data through {last} ({mtime_h:.1f}h since refresh)"
    except Exception:
        pass
    return True, f"DB refreshed {mtime_h:.1f}h ago ({len(data)} rows)"


def _live_trading_checklist(acct, data) -> bool:
    print("\n=== Live Trading Checklist ===")
    ok = True
    if config.ALLOW_LIVE_TRADING:
        print("[OK] ALLOW_LIVE_TRADING=yes")
    else:
        print("[FAIL] ALLOW_LIVE_TRADING must be yes for live trading")
        ok = False

    equity = float(acct.equity)
    config.configure_account_profile(equity)
    if equity > 50:
        print(f"[OK] Equity ${equity:,.2f} (> $50 minimum)")
    else:
        print(f"[FAIL] Equity ${equity:,.2f} — need > $50 before going live")
        ok = False

    if alerts.alerts_configured():
        print("[OK] Alerts configured (Telegram and/or email)")
    else:
        print("[FAIL] Alerts not configured — set TELEGRAM_* or SMTP_* in .env")
        ok = False

    fresh, msg = _data_refresh_ok(data)
    if fresh:
        print(f"[OK] Recent data refresh: {msg}")
    else:
        print(f"[FAIL] {msg}")
        ok = False

    if config.is_small_account(equity):
        print(
            f"[OK] Small account safety mode (<${config.SMALL_ACCOUNT_EQUITY_THRESHOLD:,.0f}): "
            f"risk {config.effective_risk_per_trade():.0%} | "
            f"max order ${config.effective_max_notional_per_order():.2f} | "
            f"VTI {config.vti_core_allocation_pct():.0%}"
        )

    if config.WISDOM_MODE != "dynamic":
        print(f"[WARN] WISDOM_MODE={config.WISDOM_MODE} (recommended: dynamic)")
    if not config.GAME_PLAN_YIELD_GATE_ONLY:
        print("[WARN] GAME_PLAN_YIELD_GATE_ONLY=false (recommended: true)")
    if not config.VTI_CORE_ENABLED:
        print("[WARN] VTI_CORE_ENABLED=false (recommended: true for live)")

    return ok


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
        eq = float(acct.equity)
        config.configure_account_profile(eq)
        if live and config.is_small_account(eq):
            print(
                f"[INFO] Small account safety (${eq:,.2f}) — "
                f"risk {config.effective_risk_per_trade():.0%} | "
                f"max order ${config.effective_max_notional_per_order():.2f} | "
                f"VTI {config.vti_core_allocation_pct():.0%}"
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

    chase_extras = config.init_paper_chase_if_enabled()
    if chase_extras:
        print(f"\n[INFO] Paper chase extras: {', '.join(chase_extras)}")

    print()
    if config.paper_chase_mode_enabled():
        config.print_recommended_stack_flags(profile="paper")
    else:
        config.print_recommended_stack_flags(profile="live")

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
    try:
        live_eq = float(acct.equity)
        config.configure_account_profile(live_eq)
        print(f"  Risk per trade:     {config.effective_risk_per_trade():.0%}")
        print(
            f"  Order sizing:       min ${config.effective_min_notional(live_eq):.2f} "
            f"| max ${config.effective_max_notional_per_order(live_eq):.2f} "
            f"(ref ${config.REFERENCE_EQUITY:,.0f})"
        )
        if config.is_small_account(live_eq):
            print(
                f"  Small account:      VTI {config.vti_core_allocation_pct():.0%} | "
                f"threshold <${config.SMALL_ACCOUNT_EQUITY_THRESHOLD:,.0f}"
            )
        print(
            f"  At $100 account:   min ${config.effective_min_notional(100):.2f} "
            f"| chunk ${100 * config.effective_risk_per_trade(100):.2f} "
            f"| max ${config.effective_max_notional_per_order(100):.2f}"
        )
    except Exception:
        print(f"  Risk per trade:     {config.RISK_PER_TRADE:.0%}")
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

    if live and acct is not None:
        if not _live_trading_checklist(acct, data):
            ok = False

    if ok:
        if live:
            print("\n=== READY (LIVE): python run_all.py ===")
            print("    (10-second abort window on first startup when live)")
        else:
            print("\n=== READY: python run_all.py ===")
    else:
        print("\n=== FIX ISSUES ABOVE BEFORE STARTING ===")
        sys.exit(1)


if __name__ == "__main__":
    run()
