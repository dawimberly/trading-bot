"""Fold slices and the SPY total-return book."""

from __future__ import annotations

import math
import sys
from pathlib import Path

ABC = Path(__file__).resolve().parent.parent / "nyse_cash_abc"
sys.path.insert(0, str(ABC))

from engine import INITIAL_CASH, buy_px, sell_px  # noqa: E402


def ann_vol(path: list[float]) -> float:
    rets = [cur / prev - 1.0 for prev, cur in zip(path, path[1:]) if prev > 0]
    if len(rets) < 2:
        return float("nan")
    var = sum((r - (sum(rets) / len(rets))) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252.0)


def max_drawdown(path: list[float]) -> float:
    peak = path[0]
    worst = 0.0
    for value in path:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def fold_metrics(result: dict, fold_dates: list[str], initial_cash: float) -> dict:
    dates = result["equity_dates"]
    equity = result["equity_path"]
    exposure = result["exposure_path"]
    by_day = {day: i for i, day in enumerate(dates)}
    first = by_day[fold_dates[0]]
    entering = initial_cash if first == 0 else equity[first - 1]
    marks = [entering] + [equity[by_day[day]] for day in fold_dates]
    exposures = [exposure[by_day[day]] for day in fold_dates]
    bought = 0.0
    sold = 0.0
    costs = 0.0
    allowed = set(fold_dates)
    for event in result["executions"]:
        if event["session"] not in allowed:
            continue
        if event["kind"] == "buy":
            bought += event["dollars"]
        else:
            sold += event["dollars"]
        costs += event["cost"]
    return {
        "start": fold_dates[0],
        "end": fold_dates[-1],
        "sessions": len(fold_dates),
        "entering_equity": entering,
        "ending_equity": marks[-1],
        "net_return": marks[-1] / entering - 1.0,
        "max_drawdown": max_drawdown(marks),
        "ann_vol": ann_vol(marks),
        "turnover": (bought + sold) / (2.0 * entering) if entering else None,
        "cost_dollars": costs,
        "avg_exposure": sum(exposures) / len(exposures),
    }


def spy_total_return(dates: list[str], bars: dict, dividends: dict[str, float], slip: float, initial_cash: float = INITIAL_CASH) -> dict:
    """Buy at the second session's open. Reinvest each cash dividend at that open. Sell at the final close."""
    if not dividends:
        raise RuntimeError("SPY cash dividends are missing; refusing a price-only total return")
    cash = float(initial_cash)
    shares = 0.0
    dividend_cash = 0.0
    events = []
    path = []
    exposure = []
    executions = []
    buy_dollars = 0.0
    sell_dollars = 0.0
    cost_dollars = 0.0
    last = len(dates) - 1
    for i, day in enumerate(dates):
        bar = bars[day]
        if i > 0 and shares > 0:
            rate = float(dividends.get(day, 0.0) or 0.0)
            if rate > 0:
                credited = shares * rate
                cash += credited
                dividend_cash += credited
                fill = buy_px(bar.open, slip)
                spent = cash
                new_shares = spent / fill
                slip_cost = spent - new_shares * bar.open
                cash = 0.0
                shares += new_shares
                buy_dollars += spent
                cost_dollars += slip_cost
                executions.append({"session": day, "kind": "buy", "dollars": spent, "cost": slip_cost})
                events.append({"session": day, "rate": rate, "cash": credited})
        if i == 1:
            fill = buy_px(bar.open, slip)
            spent = cash
            new_shares = spent / fill
            slip_cost = spent - new_shares * bar.open
            shares += new_shares
            cash = 0.0
            buy_dollars += spent
            cost_dollars += slip_cost
            executions.append({"session": day, "kind": "buy", "dollars": spent, "cost": slip_cost})
        if i == last and shares > 0:
            fill = sell_px(bar.close, slip)
            proceeds = shares * fill
            slip_cost = shares * bar.close - proceeds
            cash += proceeds
            sell_dollars += proceeds
            cost_dollars += slip_cost
            executions.append({"session": day, "kind": "sell", "dollars": proceeds, "cost": slip_cost})
            shares = 0.0
        equity = cash + shares * bar.close
        path.append(equity)
        exposure.append((shares * bar.close) / equity if equity > 0 else 0.0)
    end = path[-1]
    return {
        "net_return": end / initial_cash - 1.0,
        "ending_equity": end,
        "ending_cash": end,
        "max_drawdown": max_drawdown(path),
        "ann_vol": ann_vol(path),
        "avg_exposure": sum(exposure) / len(exposure),
        "turnover": (buy_dollars + sell_dollars) / (2.0 * initial_cash),
        "cost_dollars": cost_dollars,
        "dividend_cash": dividend_cash,
        "dividend_events": len(events),
        "dividends": events,
        "equity_path": path,
        "equity_dates": list(dates),
        "exposure_path": exposure,
        "executions": executions,
    }


def spy_total_return_paid(
    dates: list[str],
    bars: dict,
    lots: list[dict],
    slip: float,
    initial_cash: float = INITIAL_CASH,
    *,
    liquidate_terminal: bool = True,
    require_lots: bool = True,
    open_book: bool = False,
) -> dict:
    """Buy at the second open. Reinvest a dividend at the payable session's open, not on the ex-date.

    A payable date after the last session remains a receivable in terminal equity.
    """
    if require_lots and not lots:
        raise RuntimeError("SPY cash dividends are missing; refusing a price-only total return")
    cash = float(initial_cash)
    shares = 0.0
    receivables: list[dict] = []
    events = []
    path = []
    exposure = []
    executions = []
    buy_dollars = 0.0
    sell_dollars = 0.0
    cost_dollars = 0.0
    booked = 0.0
    received = 0.0
    last = len(dates) - 1
    by_ex: dict[str, list[dict]] = {}
    for lot in lots:
        by_ex.setdefault(lot["ex_session"], []).append(lot)
    for i, day in enumerate(dates):
        bar = bars[day]
        if i > 0 and shares > 0:
            for lot in by_ex.get(day, []):
                amount = shares * float(lot["rate"])
                receivables.append({"pay": lot["pay_session"], "amount": amount, "paid": False, "rate": lot["rate"], "ex_session": day})
                booked += amount
        due = [rec for rec in receivables if not rec["paid"] and rec["pay"] == day]
        for rec in due:
            cash += rec["amount"]
            rec["paid"] = True
            received += rec["amount"]
            fill = buy_px(bar.open, slip)
            spent = rec["amount"]
            if spent > 1.0 and cash + 1e-9 >= spent:
                new_shares = spent / fill
                slip_cost = spent - new_shares * bar.open
                cash -= spent
                shares += new_shares
                buy_dollars += spent
                cost_dollars += slip_cost
                executions.append({"session": day, "kind": "buy", "dollars": spent, "cost": slip_cost})
                events.append({"session": day, "ex_cash": rec["amount"], "rate": rec["rate"], "reinvested": True})
        if i == 1:
            fill = buy_px(bar.open, slip)
            spent = cash
            new_shares = spent / fill
            slip_cost = spent - new_shares * bar.open
            shares += new_shares
            cash = 0.0
            buy_dollars += spent
            cost_dollars += slip_cost
            executions.append({"session": day, "kind": "buy", "dollars": spent, "cost": slip_cost})
        if liquidate_terminal and i == last and shares > 0:
            fill = sell_px(bar.close, slip)
            proceeds = shares * fill
            slip_cost = shares * bar.close - proceeds
            cash += proceeds
            sell_dollars += proceeds
            cost_dollars += slip_cost
            executions.append({"session": day, "kind": "sell", "dollars": proceeds, "cost": slip_cost})
            shares = 0.0
        unpaid = sum(rec["amount"] for rec in receivables if not rec["paid"])
        equity = cash + unpaid + shares * bar.close
        path.append(equity)
        exposure.append((shares * bar.close) / equity if equity > 0 else 0.0)
    end = path[-1]
    unpaid = sum(rec["amount"] for rec in receivables if not rec["paid"])
    out = {
        "net_return": end / initial_cash - 1.0,
        "ending_equity": end,
        "ending_cash": end - unpaid,
        "max_drawdown": max_drawdown(path),
        "ann_vol": ann_vol(path),
        "avg_exposure": sum(exposure) / len(exposure),
        "turnover": (buy_dollars + sell_dollars) / (2.0 * initial_cash),
        "cost_dollars": cost_dollars,
        "dividend_cash": received,
        "dividend_booked": booked,
        "dividend_unpaid": unpaid,
        "dividend_events": len(events),
        "dividends": events,
        "equity_path": path,
        "equity_dates": list(dates),
        "exposure_path": exposure,
        "executions": executions,
        "receivables": receivables,
    }
    if open_book:
        out["shares"] = shares
        out["cash"] = cash
    return out
