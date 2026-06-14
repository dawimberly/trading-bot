"""Full-control checklist: Kraken stocks via API (xStocks + migration off .EQ).

Run: python scripts/account/kraken_stock_setup.py
     python scripts/account/kraken_stock_setup.py --probe
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from modules.kraken_capabilities import probe_kraken_capabilities
from modules.kraken_portfolio import build_portfolio_snapshot, holdings_by_ticker
from modules.kraken_rebalance import TARGETS_FILE, build_rebalance_plan
from modules.kraken_stock_routes import format_route_line, resolve_tradable_route
from modules.kraken_xstocks import _load_xstock_pairs
from modules.data_loader import load_close_matrix
from modules.wisdom_sentiment import resolve_wisdom_regime
from modules.macro_signals import evaluate, load_daily_matrix
from modules.crypto_vol_gate import crypto_trading_allowed
from modules.pipeline_strategies import PAUSED_REGIMES


def main() -> int:
    parser = argparse.ArgumentParser(description="Kraken full stock control setup")
    parser.add_argument("--probe", action="store_true", help="Fresh API capability probe")
    args = parser.parse_args()

    cap = probe_kraken_capabilities(force=args.probe)
    xstocks = _load_xstock_pairs()

    print("=== KRAKEN FULL STOCK CONTROL ===\n")

    print("--- Step 1: Kraken account (one-time, in browser) ---")
    print("  A. pro.kraken.com -> Settings -> Connections & API")
    print("     (Order permissions you already have are correct.)")
    print("  B. Kraken Pro -> Account / Identity -> complete xStocks verification")
    print("     (Not an API checkbox - separate account unlock.)")
    print("  C. If xStocks is not offered in your region, stock API stays manual;")
    print("     crypto still automates.\n")

    print("--- Step 2: API probe ---")
    print(f"  crypto:      {cap.get('crypto_ok')}  {cap.get('crypto_error') or ''}")
    print(f"  xStocks:     {cap.get('xstock_ok')}  {cap.get('xstock_error') or ''}")
    print(f"  equity spot: {cap.get('equity_spot_ok')}  {cap.get('equity_error') or ''}")
    print(f"  xStock pairs on Kraken: {len(xstocks)}")
    print(f"    {', '.join(sorted(xstocks.keys()))}\n")

    snap = build_portfolio_snapshot()
    holdings = holdings_by_ticker(snap) if snap.get("ok") else {}
    eq_holdings = [
        (t, h) for t, h in holdings.items()
        if t != "USD" and t not in {"BTC", "ETH", "SOL", "RENDER", "ADA", "AVAX", "LINK"}
    ]

    print("--- Step 3: Your stock holdings (.EQ = app-only until migrated) ---")
    if not eq_holdings:
        print("  (no equity holdings detected in API balances)")
    else:
        manual_sells = []
        for ticker, pos in sorted(eq_holdings, key=lambda x: -float(x[1].get("usd") or 0)):
            route = resolve_tradable_route(ticker, capabilities=cap)
            usd = float(pos.get("usd") or 0)
            vol = pos.get("volume")
            line = format_route_line(route)
            print(f"  {line} | ~${usd:.0f} vol={vol}")
            if route.get("route") == "eq_manual" or not route.get("api_ok"):
                manual_sells.append((ticker, usd, route))
        if manual_sells:
            print("\n  ONE-TIME in Kraken app (Stocks & ETFs tab):")
            for i, (t, usd, route) in enumerate(manual_sells, 1):
                xpair = xstocks.get(t)
                hint = f"then buy {xpair} via bot later" if xpair else "no xStock - use VOOI/SLVI or hold cash"
                print(f"    {i}. SELL {t}.EQ (~${usd:.0f}) -> {hint}")

    print("\n--- Step 4: Bot target tickers (reference/kraken_targets.json) ---")
    if TARGETS_FILE.exists():
        cfg = json.loads(TARGETS_FILE.read_text(encoding="utf-8"))
        for profile in ("calm", "stress"):
            eq = (cfg.get("profiles") or {}).get(profile, {}).get("equities") or {}
            print(f"  {profile}:")
            for sym in eq:
                route = resolve_tradable_route(sym.upper(), capabilities=cap)
                print(f"    {format_route_line(route)}")

    print("\n--- Step 5: Planned rebalance trades ---")
    data = load_close_matrix()
    if not data.empty:
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
        if plan.get("ok"):
            api_n = manual_n = 0
            for t in plan.get("trades") or []:
                sym = (t.get("symbol") or "").upper()
                route = resolve_tradable_route(sym, capabilities=cap)
                if route.get("api_ok"):
                    api_n += 1
                else:
                    manual_n += 1
            print(f"  {len(plan.get('trades') or [])} trade(s): {api_n} API-ready, {manual_n} need app/migration")
        else:
            print(f"  plan error: {plan.get('error')}")

    print("\n--- Step 6: .env (bot live flags) ---")
    print(f"  ALLOW_KRAKEN_TRADING={config.ALLOW_KRAKEN_TRADING}")
    print(f"  KRAKEN_DRY_RUN={config.KRAKEN_DRY_RUN}")
    print(f"  KRAKEN_AUTOPILOT_ENABLED={config.KRAKEN_AUTOPILOT_ENABLED}")
    print(f"  KRAKEN_REBALANCE_ENABLED={config.KRAKEN_REBALANCE_ENABLED}")

    ready = cap.get("crypto_ok") and cap.get("xstock_ok")
    print("\n=== FULL API STOCK CONTROL ===")
    if ready:
        print("  READY after .EQ migration (Step 3 sells in app).")
    elif cap.get("xstock_ok"):
        print("  xStocks OK - migrate .EQ holdings (Step 3), then run run_all.py")
    elif cap.get("equity_spot_ok"):
        print("  PARTIAL: VOO/SLV via equity spot only; SPY/NVDA/AAPL need xStocks unlock")
    else:
        print("  BLOCKED: enable xStocks on Kraken account, then re-run with --probe")

    print("\n  python scripts/account/preflight_kraken.py --probe")
    print("  python run_all.py")
    return 0 if cap.get("crypto_ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
