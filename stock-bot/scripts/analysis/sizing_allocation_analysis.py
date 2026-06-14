"""Position sizing & sleeve cap fill-rate simulation (live rules vs backtester).

Run from repo root:
  python scripts/analysis/sizing_allocation_analysis.py
  python scripts/analysis/sizing_allocation_analysis.py --json sizing_report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config


def _live_notional(equity: float, cash: float, sleeve_cap_pct: float, sleeve_value: float) -> float | None:
    """Mirror AlpacaExecutor._compute_capped_notional."""
    cap = round(equity * sleeve_cap_pct, 2)
    room = round(cap - sleeve_value, 2)
    if room < config.MIN_NOTIONAL:
        return None
    per_trade = round(equity * config.RISK_PER_TRADE, 2)
    raw = min(room, per_trade, config.MAX_NOTIONAL_PER_ORDER, round(cash * 0.95, 2))
    if raw < config.MIN_NOTIONAL:
        return None
    return raw


def _backtest_notional(cash: float) -> float:
    """BacktestPortfolio.trade default when notional=None."""
    raw = min(cash * config.RISK_PER_TRADE, config.MAX_NOTIONAL_PER_ORDER, cash * 0.95)
    return round(max(config.MIN_NOTIONAL, raw), 2)


def simulate_sleeve_fill(
    equity: float,
    cap_pct: float,
    *,
    cycles_per_day: float = 96.0,
    cofire_sleeves: int = 1,
    start_all_cash: bool = True,
) -> dict:
    """Simulate incremental buys until sleeve cap reached (live rules)."""
    alloc = config.fund_allocation_pct()
    cash = equity if start_all_cash else equity * alloc["cash_buffer"]
    sleeve_value = 0.0
    cap = equity * cap_pct
    trades = 0
    cycles = 0
    notionals = []
    while sleeve_value < cap - config.MIN_NOTIONAL and cycles < 5000:
        cycles += 1
        for _ in range(cofire_sleeves):
            n = _live_notional(equity, cash, cap_pct, sleeve_value)
            if n is None:
                break
            sleeve_value = round(sleeve_value + n, 2)
            cash = round(cash - n, 2)
            trades += 1
            notionals.append(n)
            if sleeve_value >= cap - config.MIN_NOTIONAL:
                break
        if cycles > 1 and not notionals:
            break
    pct_filled = round(100 * sleeve_value / cap, 2) if cap > 0 else 0
    return {
        "equity": equity,
        "cap_pct": cap_pct,
        "cap_usd": round(cap, 2),
        "start_all_cash": start_all_cash,
        "cofire_sleeves_per_cycle": cofire_sleeves,
        "trades_to_fill": trades,
        "cycles_to_fill": cycles,
        "hours_at_15m_cycles": round(cycles * 0.25, 1),
        "days_at_15m_if_unique": round(cycles / cycles_per_day, 1),
        "final_sleeve_value": sleeve_value,
        "pct_cap_filled": pct_filled,
        "avg_notional": round(sum(notionals) / len(notionals), 2) if notionals else 0,
        "first_notional": notionals[0] if notionals else None,
        "last_notional": notionals[-1] if notionals else None,
    }


def simulate_full_fund_deploy(
    equity: float,
    *,
    cofire: tuple[str, ...] = ("spy", "crypto", "nyse"),
) -> dict:
    """Deploy all sleeves from 100% cash until each hits cap or cash exhausted."""
    alloc = config.fund_allocation_pct()
    cash = equity
    sleeves = {k: 0.0 for k in cofire}
    trades = 0
    cycles = 0
    while cycles < 5000:
        cycles += 1
        any_buy = False
        for name in cofire:
            cap_pct = alloc[name]
            n = _live_notional(equity, cash, cap_pct, sleeves[name])
            if n is None:
                continue
            sleeves[name] = round(sleeves[name] + n, 2)
            cash = round(cash - n, 2)
            trades += 1
            any_buy = True
        if not any_buy:
            break
        at_cap = all(
            sleeves[k] >= equity * alloc[k] - config.MIN_NOTIONAL for k in cofire
        )
        if at_cap:
            break
    deployed = sum(sleeves.values())
    target = sum(equity * alloc[k] for k in cofire)
    return {
        "equity": equity,
        "cofire_sleeves": list(cofire),
        "trades": trades,
        "cycles": cycles,
        "hours_at_15m": round(cycles * 0.25, 1),
        "deployed_usd": round(deployed, 2),
        "target_long_usd": round(target, 2),
        "pct_target_reached": round(100 * deployed / target, 2) if target else 0,
        "remaining_cash_usd": round(cash, 2),
        "remaining_cash_pct": round(100 * cash / equity, 2),
        "sleeve_values": {k: round(v, 2) for k, v in sleeves.items()},
    }


def triple_cofire_one_cycle(equity: float) -> dict:
    alloc = config.fund_allocation_pct()
    cash = equity * alloc["cash_buffer"]
    caps = {k: equity * alloc[k] for k in ("spy", "crypto", "nyse")}
    orders = []
    for sleeve, cap_pct in [
        ("spy", alloc["spy"]),
        ("crypto", alloc["crypto"]),
        ("nyse", alloc["nyse"]),
    ]:
        n = _live_notional(equity, cash, cap_pct, 0.0)
        if n:
            orders.append({"sleeve": sleeve, "notional": n})
            cash -= n
    combined_cap = sum(caps.values())
    deployed = sum(o["notional"] for o in orders)
    return {
        "equity": equity,
        "combined_long_cap_usd": round(combined_cap, 2),
        "one_cycle_deploy_usd": round(deployed, 2),
        "pct_of_combined_cap_in_one_cycle": round(100 * deployed / combined_cap, 2)
        if combined_cap
        else 0,
        "orders": orders,
        "cash_after": round(cash, 2),
    }


def live_vs_backtest_gap(equity: float) -> dict:
    alloc = config.fund_allocation_pct()
    cash = equity * alloc["cash_buffer"]
    live_spy = _live_notional(equity, cash, alloc["spy"], 0.0)
    bt = _backtest_notional(cash)
    return {
        "equity": equity,
        "cash_buffer_usd": round(cash, 2),
        "live_spy_first_buy": live_spy,
        "backtest_default_buy": bt,
        "live_vs_backtest_ratio": round(live_spy / bt, 2) if live_spy and bt else None,
        "note": "Live uses equity*RISK_PER_TRADE; backtest uses cash*RISK_PER_TRADE when notional=None",
    }


def main() -> dict:
    alloc = config.fund_allocation_pct()
    report = {
        "config": {
            "RISK_PER_TRADE": config.RISK_PER_TRADE,
            "MAX_NOTIONAL_PER_ORDER": config.MAX_NOTIONAL_PER_ORDER,
            "MIN_NOTIONAL": config.MIN_NOTIONAL,
            "GAME_PLAN_ENABLED": config.GAME_PLAN_ENABLED,
            "fund_allocation_pct": alloc,
            "base_caps": {
                "SPY_SLEEVE_CAP_PCT": config.SPY_SLEEVE_CAP_PCT,
                "CRYPTO_SLEEVE_CAP_PCT": config.CRYPTO_SLEEVE_CAP_PCT,
                "NYSE_SLEEVE_CAP_PCT": config.NYSE_SLEEVE_CAP_PCT,
                "METAL_SLEEVE_CAP_PCT": config.METAL_SLEEVE_CAP_PCT,
                "long_fund_scale": config.long_fund_scale(),
            },
        },
        "sleeve_overlap_reference": {
            "pct_co_fire_spy_nyse": 80.3,
            "pct_co_fire_any_two": 82.1,
            "pct_days_spy_wants": 80.3,
            "pct_days_nyse_wants": 98.8,
            "source": "scripts/analysis/sleeve_overlap_report.json",
        },
        "fill_simulation_spy_from_all_cash": [
            simulate_sleeve_fill(eq, alloc["spy"], cofire_sleeves=1, start_all_cash=True)
            for eq in (50_000, 100_000, 250_000, 500_000, 1_000_000)
        ],
        "full_fund_deploy_spy_nyse_crypto": [
            simulate_full_fund_deploy(eq) for eq in (100_000, 250_000, 500_000)
        ],
        "triple_cofire_one_cycle": [triple_cofire_one_cycle(eq) for eq in (100_000, 250_000, 500_000)],
        "live_vs_backtest_notional": [live_vs_backtest_gap(eq) for eq in (50_000, 100_000, 500_000)],
        "interpretation": {
            "slow_fill": "~20 solo trades to reach SPY cap from all-cash at 2%/trade (40.5% / 2% ≈ 20)",
            "full_deploy": "All three long sleeves from 100% cash: ~40-45 trades / ~10-12h at 15m cycles",
            "cash_limited_cofire": "At equilibrium cash (13.5%), triple co-fire deploys ~7.8% of combined long cap per cycle",
            "backtest_gaps": [
                "BacktestExecutor lacks compute_*_notional → no sleeve caps, crypto buys skipped",
                "Backtest default sizes on cash not equity → diverges as portfolio deploys",
            ],
        },
    }
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", help="Write report JSON path")
    args = parser.parse_args()
    out = main()
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(f"Wrote {args.json}")
    print(json.dumps(out, indent=2))
