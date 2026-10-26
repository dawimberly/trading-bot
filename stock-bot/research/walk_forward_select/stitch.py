"""Continuous arm-A book. Positions keep the parameters from their own entry.

This follows the validated engine's arm A clock. A constant parameter schedule
must match `engine.simulate` on the same inputs. It does not add a filter,
a target, or a rotation rule.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ABC = Path(__file__).resolve().parent.parent / "nyse_cash_abc"
sys.path.insert(0, str(ABC))

from engine import (  # noqa: E402
    ATR_PERIOD,
    INITIAL_CASH,
    MAX_NAMES,
    SLEEVE,
    SLIP,
    Bar,
    Distribution,
    _close_array,
    atr_at,
    buy_px,
    ma_at,
    sell_px,
    usable,
)


@dataclass(frozen=True)
class Settlement:
    """Contractual merger consideration. Cash is the stated rate, not a market sale."""

    symbol: str
    cash_per_share: float = 0.0
    acquirer: str | None = None
    share_ratio: float = 0.0
    residual_name: str | None = None
    residual_per_share: float = 0.0
    residual_low: float | None = None
    residual_high: float | None = None
    residual_terminal: str = "bounded"


@dataclass(frozen=True)
class Grant:
    """Units received with no priced tape. Fractions stay on the entitlement."""

    session: str
    parent: str
    name: str
    ratio: float
    value_low: float | None = None
    value_high: float | None = None
    terminal: str = "unpriced_path"


def _terminal_bounds(entitlements: list[dict], listed_equity: float) -> tuple[float | None, float | None, bool]:
    """Listed equity plus contractual residuals. An unpriced path has no terminal range."""
    if not entitlements:
        return listed_equity, listed_equity, False
    if any(
        row["terminal"] != "bounded" or row["value_low"] is None or row["value_high"] is None
        for row in entitlements
    ):
        return None, None, True
    low = listed_equity
    high = listed_equity
    for row in entitlements:
        low += float(row["units"]) * float(row["value_low"])
        high += float(row["units"]) * float(row["value_high"])
    return low, high, False


def simulate_continuous(
    bars: dict[str, list[Bar | None]],
    member: dict[str, list[bool]],
    dates: list[str],
    spy: list[float | None],
    schedule: list[dict | None],
    *,
    decision_start: int,
    dividends: dict[str, dict[str, float]] | None = None,
    atr_period: int = ATR_PERIOD,
    max_names: int = MAX_NAMES,
    sleeve: float = SLEEVE,
    slip: float = SLIP,
    initial_cash: float = INITIAL_CASH,
    renames: dict[str, list[tuple[str, str]]] | None = None,
    distributions: list[Distribution] | None = None,
    settlements: dict[str, list[Settlement]] | None = None,
    grants: list[Grant] | None = None,
    strict_actions: bool = False,
    dividend_lots: list[dict] | None = None,
    liquidate_terminal: bool = True,
    open_book: bool = False,
) -> dict:
    """`schedule[i]` is the parameter set used for a new decision on session i.

    None queues no new buy. Entries store that session's stop multiplier and
    holding limit. The book liquidates on the last session when
    `liquidate_terminal` is true. The shadow runner turns that off until the
    fixed horizon.
    """
    if len(schedule) != len(dates):
        raise RuntimeError("schedule length")
    n = len(dates)
    symbols = [s for s in bars if s in member]
    closes = {s: _close_array(bars[s]) for s in bars}
    divs = dividends or {}
    cash = float(initial_cash)
    positions: dict[str, dict] = {}
    pending: list[dict] = []
    trades: list[dict] = []
    executions: list[dict] = []
    equity_path: list[float] = []
    exposure_path: list[float] = []
    buy_dollars = 0.0
    sell_dollars = 0.0
    cost_dollars = 0.0
    div_dollars = 0.0
    div_received = 0.0
    receivables: list[dict] = []
    lots_on: dict[str, list[dict]] = {}
    for lot in dividend_lots or []:
        lots_on.setdefault(str(lot["ex_session"]), []).append(lot)
    stale_terminals = 0
    distribution_unpriced = 0
    unresolved_holdings = 0
    unresolved_symbols: set[str] = set()
    last_close: dict[str, float] = {}
    entitlements: list[dict] = []
    rename_on = renames or {}
    settle_on = settlements or {}
    dists_on: dict[str, list[Distribution]] = {}
    for dist in distributions or []:
        dists_on.setdefault(dist.session, []).append(dist)
    grants_on: dict[str, list[Grant]] = {}
    for grant in grants or []:
        grants_on.setdefault(grant.session, []).append(grant)

    def unpaid_receivable() -> float:
        return sum(row["amount"] for row in receivables if not row["paid"])

    def mark_equity() -> float:
        total = cash + unpaid_receivable()
        for sym, pos in positions.items():
            px = last_close.get(sym)
            if px is not None and px > 0:
                total += pos["shares"] * px
            else:
                total += pos["cost"]
        return total

    def sell(sym: str, i: int, raw: float, reason: str, *, stale: bool = False) -> None:
        nonlocal cash, sell_dollars, cost_dollars, stale_terminals
        pos = positions.pop(sym)
        raw_px = float(raw)
        fill = sell_px(raw_px, slip)
        proceeds = pos["shares"] * fill
        cash += proceeds
        sell_dollars += proceeds
        slip_cost = pos["shares"] * raw_px - proceeds
        cost_dollars += slip_cost
        if stale:
            stale_terminals += 1
        pnl = proceeds - pos["cost"]
        trades.append(
            {
                "symbol": sym,
                "signal_date": dates[pos["signal_i"]],
                "entry_date": dates[pos["entry_i"]],
                "exit_date": dates[i],
                "bars_held": int(i - pos["entry_i"]),
                "entry_fill": pos["entry_fill"],
                "exit_raw": raw_px,
                "exit_fill": fill,
                "shares": pos["shares"],
                "ret_on_fill": fill / pos["entry_fill"] - 1.0,
                "reason": reason,
                "pnl": pnl,
                "entry_ma_win": pos["ma_win"],
                "entry_max_hold": pos["max_hold"],
                "entry_atr_mult": pos["atr_mult"],
            }
        )
        executions.append({"session": dates[i], "kind": "sell", "dollars": proceeds, "cost": slip_cost})

    def challengers(i: int, ma_win: int) -> list[tuple[float, str, float]]:
        scored = []
        for sym in symbols:
            if sym in positions or not member[sym][i]:
                continue
            bar = bars[sym][i]
            if not usable(bar):
                continue
            ma = ma_at(closes[sym], i, ma_win)
            atr = atr_at(closes[sym], i, atr_period)
            if ma is None or atr is None or atr <= 0 or not (bar.close > ma):
                continue
            mom = bar.close / ma - 1.0
            if mom <= 0:
                continue
            scored.append((mom, sym, atr))
        scored.sort(key=lambda row: (-row[0], row[1]))
        return scored

    for i in range(n):
        if i > 0:
            session = dates[i]
            if dividend_lots is None:
                for sym, pos in list(positions.items()):
                    rate = float(divs.get(sym, {}).get(session, 0.0) or 0.0)
                    if rate > 0 and pos["entry_i"] < i:
                        credit = pos["shares"] * rate
                        cash += credit
                        div_dollars += credit
                        div_received += credit
            else:
                for lot in lots_on.get(session, []):
                    pos = positions.get(lot["symbol"])
                    if pos is None or pos["entry_i"] >= i:
                        continue
                    credit = pos["shares"] * float(lot["rate"])
                    if credit <= 0:
                        continue
                    receivables.append(
                        {
                            "symbol": lot["symbol"],
                            "ex_session": session,
                            "pay_session": lot["pay_session"],
                            "amount": credit,
                            "paid": False,
                        }
                    )
                    div_dollars += credit
                for rec in receivables:
                    if rec["paid"] or rec["pay_session"] != session:
                        continue
                    cash += rec["amount"]
                    rec["paid"] = True
                    div_received += rec["amount"]

        for old, new in rename_on.get(dates[i], []):
            if old not in positions:
                continue
            successor = bars.get(new)
            successor_bar = successor[i] if successor is not None and i < len(successor) else None
            if new in positions or not usable(successor_bar):
                if strict_actions:
                    unresolved_holdings += 1
                    unresolved_symbols.add(old)
                continue
            positions[new] = positions.pop(old)
            if old in last_close:
                last_close[new] = last_close.pop(old)
            for order in pending:
                if order["symbol"] == old:
                    order["symbol"] = new

        for dist in dists_on.get(dates[i], []):
            pos = positions.get(dist.parent)
            if pos is None or pos["entry_i"] >= i or dist.ratio <= 0:
                continue
            series = bars.get(dist.child)
            bar = series[i] if series is not None and i < len(series) else None
            if not usable(bar):
                distribution_unpriced += 1
                continue
            entitled = pos["shares"] * dist.ratio
            whole = float(math.floor(entitled + 1e-8))
            fraction = max(0.0, entitled - whole)
            if fraction >= 1e-8:
                raw_px = bar.open
                fill = sell_px(raw_px, slip)
                proceeds = fraction * fill
                cash += proceeds
                sell_dollars += proceeds
                slip_cost = fraction * raw_px - proceeds
                cost_dollars += slip_cost
                trades.append(
                    {
                        "symbol": dist.child,
                        "signal_date": dates[i],
                        "entry_date": dates[i],
                        "exit_date": dates[i],
                        "bars_held": 0,
                        "entry_fill": raw_px,
                        "exit_raw": raw_px,
                        "exit_fill": fill,
                        "shares": fraction,
                        "ret_on_fill": fill / raw_px - 1.0,
                        "reason": "distribution_fraction",
                        "pnl": proceeds,
                        "entry_ma_win": None,
                        "entry_max_hold": None,
                        "entry_atr_mult": None,
                    }
                )
                executions.append({"session": dates[i], "kind": "sell", "dollars": proceeds, "cost": slip_cost})
            if whole >= 1.0:
                spec = schedule[i]
                hold = int(spec["max_hold"]) if spec else int(pos["max_hold"])
                mult = float(spec["atr_mult"]) if spec else float(pos["atr_mult"])
                ma = int(spec["ma_win"]) if spec else int(pos["ma_win"])
                if dist.child in positions:
                    positions[dist.child]["shares"] += whole
                else:
                    positions[dist.child] = {
                        "shares": whole,
                        "cost": 0.0,
                        "entry_fill": bar.open,
                        "entry_i": i,
                        "signal_i": i,
                        "stop": None,
                        "target": None,
                        "distributed": True,
                        "ma_win": ma,
                        "max_hold": hold,
                        "atr_mult": mult,
                    }
                last_close[dist.child] = bar.close

        for grant in grants_on.get(dates[i], []):
            pos = positions.get(grant.parent)
            if pos is None or pos["entry_i"] >= i:
                continue
            units = float(pos["shares"]) * float(grant.ratio)
            if units <= 0:
                continue
            bounded = grant.terminal == "bounded"
            entitlements.append(
                {
                    "name": grant.name,
                    "parent": grant.parent,
                    "session": dates[i],
                    "units": units,
                    "terminal": grant.terminal,
                    "grant_value_low": grant.value_low,
                    "grant_value_high": grant.value_high,
                    "value_low": grant.value_low if bounded else None,
                    "value_high": grant.value_high if bounded else None,
                }
            )

        for deal in settle_on.get(dates[i], []):
            pos = positions.get(deal.symbol)
            if pos is None or pos["entry_i"] >= i or deal.symbol in unresolved_symbols:
                continue
            acquirer_bar = None
            if deal.share_ratio > 0:
                series = bars.get(deal.acquirer) if deal.acquirer else None
                acquirer_bar = series[i] if series is not None and i < len(series) else None
                if not usable(acquirer_bar):
                    unresolved_holdings += 1
                    unresolved_symbols.add(deal.symbol)
                    continue
            shares = float(pos["shares"])
            cash_proceeds = shares * float(deal.cash_per_share)
            cash += cash_proceeds
            sell_dollars += cash_proceeds
            cost_basis = float(pos["cost"])
            trades.append(
                {
                    "symbol": deal.symbol,
                    "signal_date": dates[pos["signal_i"]],
                    "entry_date": dates[pos["entry_i"]],
                    "exit_date": dates[i],
                    "bars_held": int(i - pos["entry_i"]),
                    "entry_fill": pos["entry_fill"],
                    "exit_raw": float(deal.cash_per_share),
                    "exit_fill": float(deal.cash_per_share),
                    "shares": shares,
                    "ret_on_fill": 0.0,
                    "reason": "merger_cash_leg" if deal.residual_name else "merger_consideration",
                    "pnl": cash_proceeds - cost_basis,
                    "entry_ma_win": pos["ma_win"],
                    "entry_max_hold": pos["max_hold"],
                    "entry_atr_mult": pos["atr_mult"],
                }
            )
            executions.append({"session": dates[i], "kind": "sell", "dollars": cash_proceeds, "cost": 0.0})
            positions.pop(deal.symbol)
            if deal.residual_name and deal.residual_per_share > 0:
                units = shares * float(deal.residual_per_share)
                bounded = deal.residual_terminal == "bounded" and deal.residual_low is not None and deal.residual_high is not None
                entitlements.append(
                    {
                        "name": deal.residual_name,
                        "parent": deal.symbol,
                        "session": dates[i],
                        "units": units,
                        "terminal": "bounded" if bounded else "unpriced_path",
                        "grant_value_low": deal.residual_low,
                        "grant_value_high": deal.residual_high,
                        "value_low": deal.residual_low if bounded else None,
                        "value_high": deal.residual_high if bounded else None,
                    }
                )
            if deal.share_ratio <= 0:
                continue
            entitled = shares * float(deal.share_ratio)
            whole = float(math.floor(entitled + 1e-8))
            fraction = max(0.0, entitled - whole)
            if fraction >= 1e-8:
                raw_px = acquirer_bar.open
                fill = sell_px(raw_px, slip)
                proceeds = fraction * fill
                cash += proceeds
                sell_dollars += proceeds
                slip_cost = fraction * raw_px - proceeds
                cost_dollars += slip_cost
                trades.append(
                    {
                        "symbol": deal.acquirer,
                        "signal_date": dates[i],
                        "entry_date": dates[i],
                        "exit_date": dates[i],
                        "bars_held": 0,
                        "entry_fill": raw_px,
                        "exit_raw": raw_px,
                        "exit_fill": fill,
                        "shares": fraction,
                        "ret_on_fill": fill / raw_px - 1.0,
                        "reason": "merger_fraction",
                        "pnl": proceeds,
                        "entry_ma_win": None,
                        "entry_max_hold": None,
                        "entry_atr_mult": None,
                    }
                )
                executions.append({"session": dates[i], "kind": "sell", "dollars": proceeds, "cost": slip_cost})
            if whole >= 1.0:
                spec = schedule[i]
                if deal.acquirer in positions:
                    positions[deal.acquirer]["shares"] += whole
                else:
                    positions[deal.acquirer] = {
                        "shares": whole,
                        "cost": 0.0,
                        "entry_fill": acquirer_bar.open,
                        "entry_i": i,
                        "signal_i": i,
                        "stop": None,
                        "target": None,
                        "distributed": True,
                        "ma_win": int(spec["ma_win"]) if spec else int(pos["ma_win"]),
                        "max_hold": int(spec["max_hold"]) if spec else int(pos["max_hold"]),
                        "atr_mult": float(spec["atr_mult"]) if spec else float(pos["atr_mult"]),
                    }
                last_close[deal.acquirer] = acquirer_bar.close

        for sym, pos in list(positions.items()):
            if pos["entry_i"] >= i:
                continue
            bar = bars[sym][i]
            if not usable(bar):
                continue
            if pos["stop"] is not None and bar.open <= pos["stop"]:
                sell(sym, i, bar.open, "stop_gap")

        if pending and i > 0:
            queued = pending
            pending = []
            queued.sort(key=lambda row: (-row["score"], row["symbol"]))
            for order in queued:
                sym = order["symbol"]
                if sym in positions or not member[sym][i]:
                    continue
                bar = bars[sym][i]
                if not usable(bar):
                    continue
                dollars = min(float(order["dollars"]), cash)
                if dollars <= 1.0:
                    continue
                fill = buy_px(bar.open, slip)
                shares = dollars / fill
                cash -= dollars
                buy_dollars += dollars
                slip_cost = dollars - shares * bar.open
                cost_dollars += slip_cost
                stop = max(0.01, fill - float(order["atr_mult"]) * float(order["atr"]))
                positions[sym] = {
                    "shares": shares,
                    "cost": dollars,
                    "entry_fill": fill,
                    "entry_i": i,
                    "signal_i": int(order["signal_i"]),
                    "stop": stop,
                    "target": None,
                    "ma_win": int(order["ma_win"]),
                    "max_hold": int(order["max_hold"]),
                    "atr_mult": float(order["atr_mult"]),
                }
                last_close[sym] = bar.close
                executions.append({"session": dates[i], "kind": "buy", "dollars": dollars, "cost": slip_cost})

        for sym, pos in list(positions.items()):
            bar = bars[sym][i]
            if not usable(bar):
                continue
            last_close[sym] = bar.close
            if pos["entry_i"] > i:
                continue
            if pos["stop"] is not None and bar.low <= pos["stop"]:
                sell(sym, i, pos["stop"], "stop")
                continue
            if i - pos["entry_i"] >= int(pos["max_hold"]):
                sell(sym, i, bar.close, "time")

        if i < n - 1:
            for sym, pos in list(positions.items()):
                if sym in unresolved_symbols:
                    continue
                if any(
                    deal.symbol == sym and later > dates[i]
                    for later, deals in settle_on.items()
                    for deal in deals
                ):
                    continue
                if usable(bars[sym][i]):
                    continue
                future = bars[sym][i + 1 :]
                if any(usable(b) for b in future):
                    continue
                if last_close.get(sym):
                    sell(sym, i, last_close[sym], "last_print", stale=True)

        elif liquidate_terminal:
            for sym, pos in list(positions.items()):
                if sym in unresolved_symbols:
                    continue
                bar = bars[sym][i]
                if usable(bar):
                    sell(sym, i, bar.close, "final")
                elif last_close.get(sym):
                    sell(sym, i, last_close[sym], "final_stale", stale=True)

        equity = mark_equity()
        if i >= decision_start:
            equity_path.append(equity)
            gross = 0.0
            for sym, pos in positions.items():
                px = last_close.get(sym)
                if px:
                    gross += pos["shares"] * px
            exposure_path.append(gross / equity if equity > 0 else 0.0)

        for sym, pos in positions.items():
            if pos.get("stop") is not None or not pos.get("distributed"):
                continue
            atr = atr_at(closes.get(sym, np.array([])), i, atr_period) if sym in closes else None
            if atr is None or atr <= 0:
                continue
            pos["stop"] = max(0.01, pos["entry_fill"] - float(pos["atr_mult"]) * atr)

        pending = []
        if i < decision_start or (liquidate_terminal and i >= n - 1):
            continue
        spec = schedule[i]
        if spec is None:
            continue
        ranked = challengers(i, int(spec["ma_win"]))
        if len(positions) >= max_names:
            continue
        slots = max_names - len(positions)
        equity_now = mark_equity()
        clip = equity_now * sleeve / max_names if max_names else 0.0
        room = cash
        for score, sym, atr in ranked[:slots]:
            dollars = min(clip, room)
            if dollars <= 1.0:
                break
            pending.append(
                {
                    "symbol": sym,
                    "dollars": dollars,
                    "atr": atr,
                    "score": score,
                    "signal_i": i,
                    "ma_win": int(spec["ma_win"]),
                    "max_hold": int(spec["max_hold"]),
                    "atr_mult": float(spec["atr_mult"]),
                }
            )
            room -= dollars

    end_equity = mark_equity()
    peak = equity_path[0] if equity_path else initial_cash
    max_dd = 0.0
    for value in equity_path:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    rets = [cur / prev - 1.0 for prev, cur in zip(equity_path, equity_path[1:]) if prev > 0]
    vol = float("nan")
    if len(rets) >= 2:
        arr = np.asarray(rets, dtype=float)
        vol = float(arr.std(ddof=1) * math.sqrt(252.0))
    trade_pnl = sum(t["pnl"] for t in trades)
    unpaid = unpaid_receivable()
    result = {
        "start": dates[decision_start],
        "end": dates[-1],
        "sessions": n - decision_start,
        "net_return": end_equity / initial_cash - 1.0,
        "ending_equity": end_equity,
        "ending_cash": cash,
        "max_drawdown": max_dd,
        "ann_vol": vol,
        "avg_exposure": float(np.mean(exposure_path)) if exposure_path else 0.0,
        "turnover": (buy_dollars + sell_dollars) / (2.0 * initial_cash),
        "trades": len(trades),
        "cost_dollars": cost_dollars,
        "dividend_dollars": div_dollars,
        "dividend_received": div_received,
        "dividend_unpaid": unpaid,
        "distribution_unpriced": distribution_unpriced,
        "unresolved_holdings": unresolved_holdings,
        "stale_terminals": stale_terminals,
        "reconciled": abs((initial_cash + trade_pnl + div_received) - cash) < 0.05
        and abs((cash + unpaid) - end_equity) < 0.05,
        "equity_path": equity_path,
        "exposure_path": exposure_path,
        "equity_dates": dates[decision_start:],
        "executions": executions,
        "trade_log": trades,
        "warmup_mark": equity_path[0] if equity_path else None,
    }
    equity_low, equity_high, valuation_inconclusive = _terminal_bounds(entitlements, end_equity)
    result["listed_equity"] = end_equity
    result["equity_low"] = equity_low
    result["equity_high"] = equity_high
    result["entitlements"] = entitlements
    result["valuation_inconclusive"] = valuation_inconclusive
    if open_book:
        result["cash"] = cash
        result["positions"] = [
            {
                "symbol": sym,
                "shares": pos["shares"],
                "cost": pos["cost"],
                "entry_fill": pos["entry_fill"],
                "entry_session": dates[pos["entry_i"]],
                "stop": pos["stop"],
                "ma_win": pos["ma_win"],
                "max_hold": pos["max_hold"],
                "atr_mult": pos["atr_mult"],
            }
            for sym, pos in sorted(positions.items())
        ]
        result["pending"] = [
            {
                "symbol": order["symbol"],
                "dollars": order["dollars"],
                "atr": order["atr"],
                "score": order["score"],
                "signal_session": dates[int(order["signal_i"])],
                "ma_win": order["ma_win"],
                "max_hold": order["max_hold"],
                "atr_mult": order["atr_mult"],
            }
            for order in pending
        ]
        result["receivables"] = list(receivables)
    return result
