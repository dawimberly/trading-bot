"""Cash-ledger NYSE MA70 walk. Research only. Does not read or write market_data.db."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

INITIAL_CASH = 100_000.0
SLEEVE = 0.95
MAX_NAMES = 15
MA_WIN = 70
ATR_PERIOD = 14
ATR_MULT = 2.0
MAX_HOLD = 30
SLIP = 5.0 / 10_000.0  # one charge per side; not also a fee
VOL_LOOKBACK = 21
VOL_CAP = 0.20
WARMUP = MA_WIN + 15
EVAL_SESSIONS = 365


@dataclass
class Bar:
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Distribution:
    """Whole child shares for a parent held at the prior close, plus cash for the fraction.

    `ratio` is child shares per parent share. The child open on `session` is the
    regular-way price. Whole shares are a holding with cost zero. The fraction
    is cash at that open, less the book's sale slippage.
    """

    session: str
    parent: str
    child: str
    ratio: float


def usable(bar: Bar | None) -> bool:
    if bar is None:
        return False
    vals = (bar.open, bar.high, bar.low, bar.close)
    if not all(math.isfinite(v) and v > 0 for v in vals):
        return False
    if bar.high + 1e-8 < max(bar.open, bar.close, bar.low):
        return False
    if bar.low - 1e-8 > min(bar.open, bar.close, bar.high):
        return False
    return True


def buy_px(raw: float, slip: float = SLIP) -> float:
    return raw * (1.0 + slip)


def sell_px(raw: float, slip: float = SLIP) -> float:
    return raw * (1.0 - slip)


def ma_at(closes: np.ndarray, i: int, win: int) -> float | None:
    if i + 1 < win:
        return None
    window = closes[i - win + 1 : i + 1]
    if window.shape[0] != win or not np.isfinite(window).all() or np.any(window <= 0):
        return None
    return float(window.mean())


def atr_at(closes: np.ndarray, i: int, period: int) -> float | None:
    """Mean absolute close-to-close change over `period` steps ending at i."""
    if i < period:
        return None
    diffs = []
    for j in range(i - period + 1, i + 1):
        prev = float(closes[j - 1])
        cur = float(closes[j])
        if not math.isfinite(prev) or not math.isfinite(cur) or prev <= 0 or cur <= 0:
            return None
        diffs.append(abs(cur - prev))
    return float(sum(diffs) / period)


def realized_vol(spy: np.ndarray, i: int, lookback: int) -> float | None:
    """Annualized sample std of `lookback` returns ending at close i. No later closes."""
    if i < lookback:
        return None
    window = spy[i - lookback : i + 1]
    if window.shape[0] != lookback + 1 or not np.isfinite(window).all() or np.any(window <= 0):
        return None
    rets = window[1:] / window[:-1] - 1.0
    if rets.shape[0] != lookback:
        return None
    return float(rets.std(ddof=1) * math.sqrt(252.0))


def adjusted_dividend_rate(raw_rate: float, dividend_date: str, splits: list[tuple[str, float]]) -> float:
    """Scale a historical per-share dividend onto later split-adjusted shares.

    `splits` is (ex_date, new_rate/old_rate) for that symbol. Factors with an
    ex-date strictly after the dividend apply. A 10-for-1 later split divides
    the cash rate by 10.
    """
    factor = 1.0
    for ex_date, ratio in splits:
        if ex_date > dividend_date and ratio > 0:
            factor *= ratio
    if factor == 0:
        return 0.0
    return float(raw_rate) / factor


def _close_array(bars: list[Bar | None]) -> np.ndarray:
    out = np.full(len(bars), np.nan)
    for i, bar in enumerate(bars):
        if usable(bar):
            out[i] = bar.close
    return out


def simulate(
    bars: dict[str, list[Bar | None]],
    member: dict[str, list[bool]],
    dates: list[str],
    spy: list[float | None],
    *,
    arm: str,
    decision_start: int,
    dividends: dict[str, dict[str, float]] | None = None,
    ma_win: int = MA_WIN,
    atr_period: int = ATR_PERIOD,
    atr_mult: float = ATR_MULT,
    max_hold: int = MAX_HOLD,
    max_names: int = MAX_NAMES,
    sleeve: float = SLEEVE,
    slip: float = SLIP,
    initial_cash: float = INITIAL_CASH,
    vol_lookback: int = VOL_LOOKBACK,
    vol_cap: float = VOL_CAP,
    renames: dict[str, list[tuple[str, str]]] | None = None,
    distributions: list[Distribution] | None = None,
    journal: list | None = None,
) -> dict:
    """Run one window.

    `dates[decision_start]` is the first session that may generate an order.
    Orders fill on the following session. `arm` is 'A', 'B', 'C', 'R', or 'P'.
    'R' is A plus the replacement rule in ROTATION_SPEC.md.
    'P' is A plus one close-based trail: arm when a decision close is 5%
    above the filled entry, then sell at the next open after a later close
    is 3% below the highest decision close since the arm. The intraday high
    is not the peak, and the fill is not the 3% threshold.
    `renames` maps a session to (old symbol, new symbol) pairs applied after
    that morning's dividend and before the open.
    """
    if arm not in ("A", "B", "C", "R", "P"):
        raise ValueError(arm)
    n = len(dates)
    symbols = [s for s in bars if s in member]
    closes = {s: _close_array(bars[s]) for s in bars}
    spy_arr = np.array([np.nan if v is None else float(v) for v in spy], dtype=float)
    divs = dividends or {}

    def _challengers(i: int) -> list[tuple[float, str, float]]:
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

    def _held(i: int) -> list[tuple[float, str]]:
        scored = []
        for sym, pos in positions.items():
            if pos["entry_i"] > i:
                continue
            bar = bars[sym][i]
            if not usable(bar):
                continue
            ma = ma_at(closes[sym], i, ma_win)
            if ma is None or ma <= 0:
                continue
            scored.append((bar.close / ma - 1.0, sym))
        scored.sort(key=lambda row: (-row[0], row[1]))
        return scored

    cash = float(initial_cash)
    positions: dict[str, dict] = {}
    pending: list[dict] = []
    trades: list[dict] = []
    equity_path: list[float] = []
    exposure_path: list[float] = []
    buy_dollars = 0.0
    sell_dollars = 0.0
    cost_dollars = 0.0
    div_dollars = 0.0
    stale_terminals = 0
    vol_blocks = 0
    replacements = 0
    rotation_buys = 0
    rotation_preempted = 0
    distribution_unpriced = 0
    last_close: dict[str, float] = {}
    rotation: dict | None = None
    trail_due: set[str] = set()
    streak_pair: tuple[str, str] | None = None
    streak_count = 0
    rename_on = renames or {}
    dists_on: dict[str, list[Distribution]] = {}
    for dist in distributions or []:
        dists_on.setdefault(dist.session, []).append(dist)

    def mark_equity() -> float:
        total = cash
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
        # Slippage versus the raw execution price, charged once.
        cost_dollars += pos["shares"] * raw_px - proceeds
        if stale:
            stale_terminals += 1
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
                "pnl": proceeds - pos["cost"],
            }
        )
        if journal is not None:
            journal.append(
                {
                    "kind": "sell",
                    "session": dates[i],
                    "symbol": sym,
                    "reason": reason,
                    "shares": pos["shares"],
                    "proceeds": proceeds,
                    "cash_after": cash,
                }
            )

    for i in range(n):
        # 1. Dividends for shares held overnight. Rate is already split-adjusted.
        if i > 0:
            session = dates[i]
            for sym, pos in list(positions.items()):
                rate = float(divs.get(sym, {}).get(session, 0.0) or 0.0)
                if rate > 0 and pos["entry_i"] < i:
                    credit = pos["shares"] * rate
                    cash += credit
                    div_dollars += credit
                    if journal is not None:
                        journal.append(
                            {"kind": "dividend", "session": session, "symbol": sym, "amount": credit}
                        )

        for old, new in rename_on.get(dates[i], []):
            if old not in positions or new in positions or new not in bars:
                continue
            positions[new] = positions.pop(old)
            if old in last_close:
                last_close[new] = last_close.pop(old)
            if rotation is not None:
                if rotation["sell"] == old:
                    rotation["sell"] = new
                if rotation["buy"] == old:
                    rotation["buy"] = new
            for order in pending:
                if order["symbol"] == old:
                    order["symbol"] = new

        # Distributions for shares held at the prior close. Not a strategy entry.
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
                cost_dollars += fraction * raw_px - proceeds
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
                    }
                )
            if whole >= 1.0:
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
                    }
                last_close[dist.child] = bar.close

        # 2. Overnight positions: a queued close-trail sells at this open.
        # The protective stop still wins when the open gaps through it.
        if arm == "P":
            for sym in list(trail_due):
                pos = positions.get(sym)
                if pos is None or pos["entry_i"] >= i:
                    trail_due.discard(sym)
                    continue
                bar = bars[sym][i]
                if not usable(bar):
                    continue
                if pos["stop"] is not None and bar.open <= pos["stop"]:
                    sell(sym, i, bar.open, "stop_gap")
                else:
                    sell(sym, i, bar.open, "trail_open")
                trail_due.discard(sym)

        # Overnight positions: gap through stop, or through target on C.
        due = rotation
        rotation = None
        due_sell = due["sell"] if due else None
        for sym, pos in list(positions.items()):
            if pos["entry_i"] >= i:
                continue
            bar = bars[sym][i]
            if not usable(bar):
                continue
            if pos["stop"] is not None and bar.open <= pos["stop"]:
                sell(sym, i, bar.open, "stop_gap")
                continue
            if arm == "C" and pos.get("target") is not None and bar.open >= pos["target"]:
                sell(sym, i, bar.open, "target_gap")

        # 3. Fill yesterday's reserved buys. Do not grow them with this morning's cash.
        reserved_symbols = [order["symbol"] for order in pending]
        if pending and i > 0:
            queued = pending
            pending = []
            queued.sort(key=lambda row: (-row["score"], row["symbol"]))
            for order in queued:
                sym = order["symbol"]
                if sym in positions:
                    continue
                if not member[sym][i]:
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
                cost_dollars += dollars - shares * bar.open
                stop = max(0.01, fill - atr_mult * float(order["atr"]))
                positions[sym] = {
                    "shares": shares,
                    "cost": dollars,
                    "entry_fill": fill,
                    "entry_i": i,
                    "signal_i": int(order["signal_i"]),
                    "stop": stop,
                    "target": fill * 1.05 if arm == "C" else None,
                }
                last_close[sym] = bar.close
                if journal is not None:
                    journal.append(
                        {
                            "kind": "buy",
                            "session": dates[i],
                            "symbol": sym,
                            "reason": "ranker",
                            "dollars": dollars,
                            "cash_after": cash,
                        }
                    )

        # Replacement queued at the prior close. Reserved buys above are not enlarged.
        if due:
            cash_before_rotation = cash
            removed = due_sell not in positions
            sale_proceeds = 0.0
            if removed:
                rotation_preempted += 1
                sell_outcome = "already_out"
            else:
                pos = positions[due_sell]
                bar = bars[due_sell][i]
                if pos["entry_i"] < i and usable(bar):
                    sell(due_sell, i, bar.open, "rotation")
                    replacements += 1
                    removed = True
                    sale_proceeds = cash - cash_before_rotation
                    sell_outcome = "sold"
                elif pos["entry_i"] >= i:
                    sell_outcome = "entered_this_session"
                else:
                    sell_outcome = "no_usable_open"
            buy_sym = due["buy"]
            buy_dollars_used = 0.0
            dollars_checked = None
            if removed and buy_sym not in positions and buy_sym in member and member[buy_sym][i]:
                bar = bars[buy_sym][i]
                if usable(bar):
                    dollars = min(float(due["clip"]), cash)
                    dollars_checked = dollars
                    if dollars > 1.0:
                        fill = buy_px(bar.open, slip)
                        shares = dollars / fill
                        cash -= dollars
                        buy_dollars += dollars
                        cost_dollars += dollars - shares * bar.open
                        stop = max(0.01, fill - atr_mult * float(due["atr"]))
                        positions[buy_sym] = {
                            "shares": shares,
                            "cost": dollars,
                            "entry_fill": fill,
                            "entry_i": i,
                            "signal_i": int(due["signal_i"]),
                            "stop": stop,
                            "target": None,
                        }
                        last_close[buy_sym] = bar.close
                        rotation_buys += 1
                        buy_dollars_used = dollars
                        buy_outcome = "filled"
                        if journal is not None:
                            journal.append(
                                {
                                    "kind": "buy",
                                    "session": dates[i],
                                    "symbol": buy_sym,
                                    "reason": "rotation",
                                    "dollars": dollars,
                                    "cash_after": cash,
                                }
                            )
                    else:
                        buy_outcome = "dollars_at_or_below_1"
                else:
                    buy_outcome = "no_usable_open"
            elif not removed:
                buy_outcome = "not_attempted"
            elif buy_sym in positions:
                held = positions[buy_sym]
                if buy_sym in reserved_symbols and held["entry_i"] == i:
                    buy_outcome = "ranker_already_bought"
                else:
                    buy_outcome = "already_held"
            elif buy_sym not in member:
                buy_outcome = "not_in_panel"
            elif not member[buy_sym][i]:
                buy_outcome = "not_a_member"
            else:
                buy_outcome = "not_attempted"
            if journal is not None:
                held_now = positions.get(buy_sym)
                journal.append(
                    {
                        "kind": "rotation",
                        "decision": dates[int(due["signal_i"])],
                        "session": dates[i],
                        "sold": due_sell,
                        "challenger": buy_sym,
                        "challenger_score": due["score"],
                        "clip": float(due["clip"]),
                        "cash_before": cash_before_rotation,
                        "sale_proceeds": sale_proceeds,
                        "cash_after": cash,
                        "sell_outcome": sell_outcome,
                        "buy_outcome": buy_outcome,
                        "dollars_checked": dollars_checked,
                        "buy_dollars": buy_dollars_used,
                        "challenger_was_reserved": buy_sym in reserved_symbols,
                        "challenger_entry": None if held_now is None else dates[int(held_now["entry_i"])],
                        "challenger_cost": None if held_now is None else held_now["cost"],
                        "member": bool(buy_sym in member and member[buy_sym][i]),
                    }
                )

        # 4–5. Same-bar range, then the 30-session time stop. New buys can stop out
        # after the open, and that cash cannot fund another buy today.
        for sym, pos in list(positions.items()):
            bar = bars[sym][i]
            if not usable(bar):
                continue
            last_close[sym] = bar.close
            if pos["entry_i"] > i:
                continue
            hit_stop = pos["stop"] is not None and bar.low <= pos["stop"]
            hit_target = arm == "C" and pos["target"] is not None and bar.high >= pos["target"]
            if hit_stop and hit_target:
                sell(sym, i, pos["stop"], "stop_same_bar")
                continue
            if hit_stop:
                sell(sym, i, pos["stop"], "stop")
                continue
            if hit_target:
                sell(sym, i, pos["target"], "target")
                continue
            if i - pos["entry_i"] >= max_hold:
                sell(sym, i, bar.close, "time")

        # Last print: a held name with no usable bar today, whose tape has ended.
        if i < n - 1:
            for sym, pos in list(positions.items()):
                if usable(bars[sym][i]):
                    continue
                future = bars[sym][i + 1 :]
                if any(usable(b) for b in future):
                    continue
                if last_close.get(sym):
                    sell(sym, i, last_close[sym], "last_print", stale=True)

        # 6. Window end.
        if i == n - 1:
            for sym, pos in list(positions.items()):
                bar = bars[sym][i]
                if usable(bar):
                    sell(sym, i, bar.close, "final")
                elif last_close.get(sym):
                    sell(sym, i, last_close[sym], "final_stale", stale=True)

        equity = mark_equity()
        if journal is not None and i >= decision_start:
            journal.append({"kind": "mark", "session": dates[i], "cash": cash, "equity": equity})
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
            pos["stop"] = max(0.01, pos["entry_fill"] - atr_mult * atr)

        # Close trail. The peak is this decision close, never the bar's high.
        # The arming close cannot be its own exit. A queued exit waits for the
        # next usable open; the last session has none, so it is not queued.
        if arm == "P" and i < n - 1:
            for sym, pos in positions.items():
                if pos["entry_i"] > i or sym in trail_due:
                    continue
                bar = bars[sym][i]
                if not usable(bar):
                    continue
                entry = float(pos["entry_fill"])
                close = float(bar.close)
                if entry <= 0 or close <= 0:
                    continue
                if not pos.get("trail_armed"):
                    if close >= entry * 1.05:
                        pos["trail_armed"] = True
                        pos["trail_peak"] = close
                    continue
                peak = float(pos["trail_peak"])
                if close > peak:
                    peak = close
                    pos["trail_peak"] = close
                if peak > 0 and (peak - close) / peak >= 0.03:
                    trail_due.add(sym)

        # 7. Queue next-open buys from this close only.
        pending = []
        if i >= n - 1:
            continue
        challengers = _challengers(i)
        if arm == "R":
            held_ranked = _held(i)
            if challengers and held_ranked:
                pair = (challengers[0][1], held_ranked[-1][1])
                if challengers[0][0] > held_ranked[-1][0]:
                    if streak_pair == pair:
                        streak_count += 1
                    else:
                        streak_pair = pair
                        streak_count = 1
                else:
                    streak_pair = None
                    streak_count = 0
            else:
                streak_pair = None
                streak_count = 0
            if i >= decision_start and streak_count >= 3 and streak_pair is not None:
                equity_now = mark_equity()
                clip = equity_now * sleeve / max_names if max_names else 0.0
                rotation = {
                    "sell": held_ranked[-1][1],
                    "buy": challengers[0][1],
                    "clip": clip,
                    "atr": challengers[0][2],
                    "score": challengers[0][0],
                    "signal_i": i,
                }
        if i < decision_start:
            continue
        blocked = False
        if arm == "B":
            rv = realized_vol(spy_arr, i, vol_lookback)
            if rv is None or rv > vol_cap:
                blocked = True
                vol_blocks += 1
        if blocked or len(positions) >= max_names:
            continue
        slots = max_names - len(positions)
        equity_now = mark_equity()
        clip = equity_now * sleeve / max_names if max_names else 0.0
        room = cash
        for score, sym, atr in challengers[:slots]:
            dollars = min(clip, room)
            if dollars <= 1.0:
                break
            pending.append(
                {"symbol": sym, "dollars": dollars, "atr": atr, "score": score, "signal_i": i}
            )
            room -= dollars

    end_equity = mark_equity()
    peak = equity_path[0] if equity_path else initial_cash
    max_dd = 0.0
    for value in equity_path:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    rets = []
    for prev, cur in zip(equity_path, equity_path[1:]):
        if prev > 0 and math.isfinite(prev) and math.isfinite(cur):
            rets.append(cur / prev - 1.0)
    sharpe = None
    if len(rets) >= 5:
        arr = np.asarray(rets, dtype=float)
        std = float(arr.std(ddof=1))
        if std > 1e-12:
            sharpe = float(arr.mean() / std * math.sqrt(252.0))
    pnl_sum = sum(t["pnl"] for t in trades) + div_dollars
    return {
        "arm": arm,
        "start": dates[decision_start],
        "end": dates[-1],
        "sessions": n - decision_start,
        "net_return": end_equity / initial_cash - 1.0,
        "ending_equity": end_equity,
        "ending_cash": cash,
        "max_drawdown": max_dd,
        "avg_exposure": float(np.mean(exposure_path)) if exposure_path else 0.0,
        "turnover": (buy_dollars + sell_dollars) / (2.0 * initial_cash),
        "trades": len(trades),
        "buy_dollars": buy_dollars,
        "sell_dollars": sell_dollars,
        "cost_dollars": cost_dollars,
        "dividend_dollars": div_dollars,
        "vol_blocks": vol_blocks,
        "replacements": replacements,
        "rotation_buys": rotation_buys,
        "rotation_preempted": rotation_preempted,
        "distribution_unpriced": distribution_unpriced,
        "stale_terminals": stale_terminals,
        "sharpe": sharpe,
        "reconciled": abs((initial_cash + pnl_sum) - cash) < 0.05 and abs(cash - end_equity) < 0.05,
        "reasons": {k: sum(1 for t in trades if t["reason"] == k) for k in sorted({t["reason"] for t in trades})},
        "trade_log": trades,
    }
