"""Kraken live-readiness checklist before Monday (or any session).

Run: python scripts/account/preflight_kraken.py
     python scripts/account/preflight_kraken.py --probe   # force fresh API capability probe
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from modules.kraken_capabilities import probe_kraken_capabilities
from modules.kraken_rebalance import build_rebalance_plan
from modules.kraken_spot import autopilot_enabled, kraken_configured, trading_allowed
from modules.data_loader import load_close_matrix
from modules.wisdom_sentiment import resolve_wisdom_regime
from modules.macro_signals import evaluate, load_daily_matrix
from modules.crypto_vol_gate import crypto_trading_allowed
from modules.pipeline_strategies import PAUSED_REGIMES


def _status(ok: bool) -> str:
    return "[OK]" if ok else "[FAIL]"


def _warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Kraken live readiness preflight")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Force fresh Kraken API capability probe (validate orders)",
    )
    args = parser.parse_args()

    ok = True
    print("=== KRAKEN LIVE PREFLIGHT ===\n")

    key, secret = config.get_kraken_credentials()
    if not key or not secret:
        _fail("Kraken API keys missing (KRAKEN_API_KEY + KRAKEN_SECRET_KEY in .env)")
        return 1
    print(_status(True), "Kraken API keys present")

    if not config.KRAKEN_AUTOPILOT_ENABLED:
        _fail("KRAKEN_AUTOPILOT_ENABLED=false - run_all will not manage Kraken")
        ok = False
    else:
        print(_status(True), "Kraken autopilot enabled")

    if not config.ALLOW_KRAKEN_TRADING:
        _fail("ALLOW_KRAKEN_TRADING not set - live orders blocked")
        ok = False
    else:
        print(_status(True), "ALLOW_KRAKEN_TRADING=yes")

    if config.KRAKEN_DRY_RUN:
        _warn("KRAKEN_DRY_RUN=true - orders validate only, no fills")
        ok = False
    else:
        print(_status(True), "KRAKEN_DRY_RUN=false (live fills)")

    print(
        f"     Order cap: ${config.KRAKEN_MAX_ORDER_USD:.0f}/trade | "
        f"cycle buy budget: ${config.KRAKEN_CYCLE_BUDGET_USD:.0f}"
    )

    if config.PAPER_TRADING:
        print(_status(True), "Alpaca paper mode (signals only) - expected for Kraken mirror setup")
    else:
        _warn("Alpaca is also live - confirm that is intentional")

    print("\n--- API capabilities (validate orders) ---")
    cap = probe_kraken_capabilities(force=args.probe)
    for label, key_name, err_key in (
        ("Crypto spot", "crypto_ok", "crypto_error"),
        ("xStocks (SPY/NYSE API)", "xstock_ok", "xstock_error"),
        ("Legacy equity spot", "equity_spot_ok", "equity_error"),
    ):
        good = bool(cap.get(key_name))
        err = cap.get(err_key) or ""
        print(f"{_status(good)} {label}", f"- {err}" if err and not good else "")
        if not good and label.startswith("xStocks"):
            ok = False

    if not cap.get("crypto_ok"):
        _fail("Crypto API not working - fix key permissions first")
        ok = False

    if not cap.get("xstock_ok"):
        print("\n--- Fix xStocks (required for automated SPY/NYSE Monday) ---")
        print("  1. Kraken > Settings (gear) > API > edit your bot key")
        print("  2. Permissions: Query Funds, Query Open/Closed Orders & Trades,")
        print("     Create & Modify Orders, Cancel/Close Orders")
        print("  3. Enable tokenized / xStocks trading if shown (account must be verified)")
        print("  4. Re-run: python scripts/account/kraken_capabilities_check.py")
        print("  5. If xStocks stays false: your region may block stock API - use")
        print("     python scripts/kraken_manual_checklist.py for app trades")

    print("\n--- Rebalance plan (dry intent, no orders) ---")
    data = load_close_matrix()
    if data.empty:
        _warn("No price matrix - run fetch_data or start run_all first")
    else:
        wisdom = resolve_wisdom_regime(data)
        regime = wisdom["regime"]
        daily = load_daily_matrix(days=450)
        gp = evaluate(daily, regime) if daily is not None and not daily.empty else {}
        gate = crypto_trading_allowed(wisdom["volatility"], regime)
        stress = (
            bool(gp.get("stress"))
            or bool(wisdom.get("governor_stress"))
            or regime in PAUSED_REGIMES
        )
        plan = build_rebalance_plan(
            stress=stress,
            crypto_allowed=bool(gate.get("allowed")),
            entries_blocked=bool(wisdom.get("wisdom_paused")),
        )
        if not plan.get("ok"):
            _warn(f"Rebalance plan error: {plan.get('error')}")
        else:
            trades = plan.get("trades") or []
            api_trades = []
            manual_trades = []
            for t in trades:
                sym = (t.get("symbol") or "").upper()
                if sym in {"BTC", "ETH", "SOL", "RENDER", "ADA", "AVAX", "LINK"}:
                    api_trades.append(t)
                else:
                    manual_trades.append(t)
            print(
                f"     Profile: {plan.get('profile')} | "
                f"~${plan.get('total_usd', 0):.0f} | "
                f"{len(trades)} planned trade(s)"
            )
            if api_trades:
                print(f"     API crypto: {len(api_trades)}")
            if manual_trades and not cap.get("xstock_ok"):
                print(f"     Manual app (until xStocks): {len(manual_trades)}")
                for t in manual_trades[:5]:
                    print(
                        f"       {t.get('side', '').upper()} {t.get('symbol')} "
                        f"~${float(t.get('usd') or 0):.0f} - {t.get('reason', '')}"
                    )
                if len(manual_trades) > 5:
                    print(f"       ... +{len(manual_trades) - 5} more")

    print("\n--- Monday schedule ---")
    if config.SCAN_SCHEDULE_ENABLED:
        print(
            f"     Equity prep: {config.EQUITY_SCAN_BEFORE_OPEN_MIN}m before open | "
            f"SPY/NYSE scans: {config.EQUITY_SCAN_AFTER_OPEN_MIN}m after open through close"
        )
        print(f"     Overnight: crypto only every {config.CRYPTO_ONLY_CYCLE_INTERVAL_SEC // 60}m")
    else:
        _warn("SCAN_SCHEDULE_ENABLED=false - legacy equity timing")

    print("\n--- Ready checks ---")
    ready_live = (
        kraken_configured()
        and autopilot_enabled()
        and trading_allowed()
        and cap.get("crypto_ok")
    )
    ready_monday_stocks = ready_live and cap.get("xstock_ok")

    if ready_live:
        print(_status(True), "Kraken crypto live automation ready")
    else:
        print(_status(False), "Kraken crypto live automation NOT ready")
        ok = False

    if ready_monday_stocks:
        print(_status(True), "Automated SPY/NYSE mirror on Kraken ready for Monday")
    else:
        print(_status(False), "Automated SPY/NYSE on Kraken NOT ready (xStocks or region)")
        if ready_live:
            _warn("Crypto will auto-trade; stock mirrors need xStocks or manual app")

    print("\n--- Next commands ---")
    print("  python scripts/account/kraken_stock_setup.py --probe   # full stock control plan")
    print("  python scripts/account/kraken_capabilities_check.py")
    print("  python scripts/kraken_autopilot_once.py")
    print("  python scripts/kraken_manual_checklist.py")
    print("  python run_all.py")

    print(f"\n=== RESULT: {'PASS' if ok and ready_live else 'ACTION REQUIRED'} ===")
    return 0 if ok and ready_live else 1


if __name__ == "__main__":
    raise SystemExit(main())
