"""Print in-app Kraken steps when stock API is blocked (e.g. US:TX)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.data_loader import load_close_matrix
from modules.kraken_rebalance import build_rebalance_plan
from modules.macro_signals import evaluate, load_daily_matrix
from modules.wisdom_sentiment import resolve_wisdom_regime
from modules.crypto_vol_gate import crypto_trading_allowed
from modules.spacex_ipo_monitor import get_spacex_ipo_monitor
from modules.pipeline_strategies import PAUSED_REGIMES


def main() -> None:
    data = load_close_matrix()
    wisdom = resolve_wisdom_regime(data)
    regime = wisdom["regime"]
    daily = load_daily_matrix(days=450)
    gp = evaluate(daily, regime) if daily is not None and not daily.empty else {}
    sp = get_spacex_ipo_monitor()
    gate = crypto_trading_allowed(wisdom["volatility"], regime, spacex_snapshot=sp)
    stress = (
        bool(gp.get("stress"))
        or bool(wisdom.get("governor_stress"))
        or regime in PAUSED_REGIMES
    )
    plan = build_rebalance_plan(
        stress=stress,
        crypto_allowed=bool(gate.get("allowed")),
        entries_blocked=False,
    )
    if not plan.get("ok"):
        print("Plan error:", plan.get("error"))
        return

    total = plan["total_usd"]
    w = plan.get("weights") or {}
    print(f"Profile: {plan.get('profile')} | ~${total:.0f} total")
    print("Target:", ", ".join(f"{k} {v}%" for k, v in w.items()))
    print()
    sells = [t for t in plan.get("trades", []) if t.get("side") == "sell"]
    buys = [t for t in plan.get("trades", []) if t.get("side") == "buy"]
    if sells:
        print("SELL in Kraken app (Stocks & ETFs tab):")
        for i, t in enumerate(sells, 1):
            print(f"  {i}. {t['symbol']}  ~${t.get('usd', 0):.0f}  — {t.get('reason', '')}")
    if buys:
        print()
        print("BUY in Kraken app:")
        for i, t in enumerate(buys, 1):
            print(f"  {i}. {t['symbol']}  ~${t.get('usd', 0):.0f}  — {t.get('reason', '')}")
    print()
    print("Crypto tab: sell RENDER via app if API min-size fails (~$5.60).")
    print("Use 'Convert small balances' for dust (SOL, BTC, BABY).")


if __name__ == "__main__":
    main()
