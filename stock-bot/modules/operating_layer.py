"""Operating Layer orchestration: WisdomAdvisor -> StrategicRebalancer -> execute."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import config
from modules.rebalancer import RebalanceOrder, StrategicRebalancer
from modules.wisdom_layer import WisdomAdvisor, format_wisdom_banner


def _rolling_sharpe_from_curve(equity_curve: list[float], window: int = 63) -> float | None:
    if len(equity_curve) < max(21, window // 2):
        return None
    import numpy as np

    series = np.asarray(equity_curve[-window:], dtype=float)
    if len(series) < 2 or series[0] <= 0:
        return None
    rets = np.diff(series) / series[:-1]
    rets = rets[np.isfinite(rets)]
    if len(rets) < 10:
        return None
    vol = float(rets.std())
    if vol < 1e-12:
        return None
    return float((rets.mean() / vol) * (252**0.5))


def format_operating_layer_banner() -> str | None:
    if not config.REBALANCE_ENABLED:
        return "Operating Layer OFF (REBALANCE_ENABLED=false)"
    wisdom_line = format_wisdom_banner()
    return (
        f"{wisdom_line} | core target {config.REBALANCE_CORE_TARGET:.0%} "
        f"band {config.REBALANCE_BAND_WIDTH:.0%} | "
        f"wisdom threshold {config.WISDOM_CONVICTION_THRESHOLD:.2f}"
    )


def _execute_live_orders(
    executor,
    orders: list[RebalanceOrder],
    *,
    market_open: bool,
) -> list[dict[str, Any]]:
    if not market_open:
        return [{"skipped": True, "reason": "equity session closed"}]
    results: list[dict[str, Any]] = []
    for order in orders:
        if order.sleeve != "core":
            continue
        if order.action == "buy":
            placed = executor.execute_order(order.symbol, "buy", notional=order.notional)
        else:
            placed = executor.execute_reduce_notional(order.symbol, order.notional)
        results.append(
            {
                "sleeve": order.sleeve,
                "action": order.action,
                "symbol": order.symbol,
                "notional": order.notional,
                "reason": order.reason,
                "ok": placed is not None,
            }
        )
    return results


def _execute_backtest_core_rebalance(portfolio, prices, core_pct: float) -> bool:
    import numpy as np

    core_sym = config.VTI_CORE_SYMBOL
    if core_pct <= 0 or core_sym not in prices.index:
        return False
    price = prices.get(core_sym)
    if price is None or not np.isfinite(price) or float(price) <= 0:
        return False
    price = float(price)
    eq = portfolio.equity(prices)
    target = round(eq * core_pct, 2)
    qty = portfolio.positions.get(core_sym, 0)
    current = float(qty) * price
    delta = round(target - current, 2)
    min_n = config.effective_min_notional(eq)
    if abs(delta) < min_n:
        return False
    if delta > 0:
        portfolio.trade(core_sym, "buy", price, tx_cost=0.0, notional=delta)
    else:
        portfolio.trade(core_sym, "sell", price, tx_cost=0.0, notional=-delta)
    return True


def _execute_backtest_orders(
    portfolio,
    prices,
    orders: list[RebalanceOrder],
    *,
    core_target: float,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if not orders:
        return results
    before = float(portfolio.positions.get(config.VTI_CORE_SYMBOL, 0.0))
    if _execute_backtest_core_rebalance(portfolio, prices, core_target):
        after = float(portfolio.positions.get(config.VTI_CORE_SYMBOL, 0.0))
        order = orders[0]
        results.append(
            {
                "sleeve": order.sleeve,
                "action": order.action,
                "symbol": order.symbol,
                "notional": order.notional,
                "reason": order.reason,
                "ok": abs(after - before) > 1e-9,
            }
        )
    return results


def run_operating_cycle_live(
    executor,
    *,
    regime: str,
    vol: str,
    macro_stress: bool,
    vol_score: float | None,
    bar_date: date | datetime,
    market_open: bool,
    equity_curve: list[float] | None = None,
    rebalancer: StrategicRebalancer | None = None,
) -> dict[str, Any] | None:
    if not config.REBALANCE_ENABLED:
        return None

    advisor = WisdomAdvisor()
    rebalancer = rebalancer or StrategicRebalancer()
    rolling_sharpe = _rolling_sharpe_from_curve(equity_curve or [])
    rec = advisor.recommend(
        regime=regime,
        vol=vol,
        macro_stress=macro_stress,
        rolling_sharpe=rolling_sharpe,
        vol_score=vol_score,
    )
    snapshot = rebalancer.snapshot_from_executor(executor)
    drift = rebalancer.check_drift(snapshot)
    core_target = advisor.apply_core_shift(config.REBALANCE_CORE_TARGET, rec)
    core_target = rebalancer.clamp_core_target(core_target)

    if not rebalancer.needs_rebalance(snapshot, bar_date, drift):
        return {
            "skipped": True,
            "drift": drift,
            "wisdom": rec.to_dict(),
            "core_target": core_target,
        }

    min_n = config.effective_min_notional(snapshot.equity)
    orders = rebalancer.generate_rebalance_orders(
        snapshot, core_target, min_notional=min_n
    )
    fills = _execute_live_orders(executor, orders, market_open=market_open)
    if fills and not all(r.get("skipped") for r in fills):
        rebalancer.mark_rebalanced(bar_date)

    return {
        "skipped": False,
        "drift": drift,
        "wisdom": rec.to_dict(),
        "core_target": core_target,
        "orders": [order.__dict__ for order in orders],
        "fills": fills,
    }


def run_operating_cycle_backtest(
    portfolio,
    prices,
    *,
    regime: str,
    vol: str,
    macro_stress: bool,
    vol_score: float | None,
    bar_date: date | datetime,
    equity_curve: list[float],
    rebalancer: StrategicRebalancer | None = None,
    prev_bar_date: date | datetime | None = None,
) -> dict[str, Any] | None:
    if not config.REBALANCE_ENABLED:
        return None

    advisor = WisdomAdvisor()
    rebalancer = rebalancer or StrategicRebalancer()
    rolling_sharpe = _rolling_sharpe_from_curve(equity_curve)
    rec = advisor.recommend(
        regime=regime,
        vol=vol,
        macro_stress=macro_stress,
        rolling_sharpe=rolling_sharpe,
        vol_score=vol_score,
    )
    snapshot = rebalancer.snapshot_from_portfolio(portfolio, prices)
    drift = rebalancer.check_drift(snapshot)
    core_target = advisor.apply_core_shift(config.REBALANCE_CORE_TARGET, rec)
    core_target = rebalancer.clamp_core_target(core_target)

    if not rebalancer.needs_rebalance(
        snapshot, bar_date, drift, prev_bar_date=prev_bar_date
    ):
        return {
            "skipped": True,
            "drift": drift,
            "wisdom": rec.to_dict(),
            "core_target": core_target,
        }

    eq = float(portfolio.equity(prices))
    min_n = config.effective_min_notional(eq)
    orders = rebalancer.generate_rebalance_orders(
        snapshot, core_target, min_notional=min_n
    )
    fills = _execute_backtest_orders(
        portfolio, prices, orders, core_target=core_target
    )
    if fills:
        rebalancer.mark_rebalanced(bar_date)

    return {
        "skipped": not bool(fills),
        "drift": drift,
        "wisdom": rec.to_dict(),
        "core_target": core_target,
        "orders": [order.__dict__ for order in orders],
        "fills": fills,
    }
