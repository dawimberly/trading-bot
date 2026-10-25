"""Research-only replay of the 120 weight directions in a 600-cell.

Tests 120 four-coordinate weight directions over momentum, market order,
synchronization, and information flow. Nothing is wired to trading.

No broker connection, orders, config, or market database writes.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from modules.pit_universe import load_sp500_membership  # noqa: E402
from nyse_momentum_walkforward import (  # noqa: E402
    ATR_MULT,
    ATR_PERIOD,
    INITIAL,
    MA_MIN,
    MA_WINDOW,
    RSI_MAX,
    SLIP,
    _is_member,
    _member_on,
    load_intraday_panel,
)

OUT_JSON = Path(__file__).with_name("hypersphere_600cell_backtest_last.json")
OUT_MD = Path(__file__).with_name("hypersphere_600cell_backtest_last.md")
OUT_CHECKPOINT = Path(__file__).with_name("hypersphere_600cell_backtest_checkpoint.jsonl")
SIGNAL_TIME = time(10, 15)
MAX_HOLD_DAYS = 30
MAX_POSITION_PCT = 0.15
SLEEVE_CAP = 0.95
TARGET_PCT = 0.05  # kept for simulator compatibility; take_profit=False throughout
TOP_COUNTS = (5,)
CELL_WEIGHTS: list[tuple[float, float, float, float]] = []


def build_600_cell_vertices() -> list[tuple[float, float, float, float]]:
    """Return the 120 unit vertices of the regular 600-cell in R4."""
    from itertools import permutations, product

    points: set[tuple[float, float, float, float]] = set()
    for i in range(4):
        for sign in (-1.0, 1.0):
            v = [0.0] * 4
            v[i] = sign
            points.add(tuple(v))
    points.update(tuple(0.5 * s for s in signs) for signs in product((-1.0, 1.0), repeat=4))
    phi = (1.0 + math.sqrt(5.0)) / 2.0
    base = (0.0, 0.5, phi / 2.0, 1.0 / (2.0 * phi))
    for p in permutations(range(4)):
        inversions = sum(p[i] > p[j] for i in range(4) for j in range(i + 1, 4))
        if inversions % 2:
            continue
        permuted = tuple(base[i] for i in p)
        nonzero = [i for i, x in enumerate(permuted) if x != 0]
        for signs in product((-1.0, 1.0), repeat=3):
            v = list(permuted)
            for i, sign in zip(nonzero, signs):
                v[i] *= sign
            points.add(tuple(v))
    vertices = sorted(points)
    if len(vertices) != 120:
        raise RuntimeError(f"600-cell should have 120 vertices; got {len(vertices)}")
    norms = [sum(x * x for x in v) for v in vertices]
    if not np.allclose(norms, 1.0, atol=1e-12):
        raise RuntimeError("600-cell coordinates are not on the unit 3-sphere")
    return vertices


def _permutation_entropy(values: np.ndarray, order: int = 3) -> float | None:
    """Normalized permutation entropy, calculated from past returns only."""
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 20:
        return None
    patterns: dict[tuple[int, ...], int] = {}
    for i in range(len(x) - order + 1):
        key = tuple(np.argsort(x[i : i + order], kind="stable"))
        patterns[key] = patterns.get(key, 0) + 1
    counts = np.asarray(list(patterns.values()), dtype=float)
    p = counts / counts.sum()
    h = -float(np.sum(p * np.log(p)))
    return float(np.clip(h / np.log(math.factorial(order)), 0.0, 1.0))


def _conditional_entropy_bits(y: np.ndarray, y_prev: np.ndarray, x_prev: np.ndarray | None = None) -> float:
    """Small discrete conditional entropy helper for binary return signs."""
    total = 0
    entropy = 0.0
    x_states = (0, 1) if x_prev is not None else (None,)
    for yp in (0, 1):
        for xp in x_states:
            mask = (y_prev == yp)
            if x_prev is not None:
                mask &= x_prev == xp
            n = int(mask.sum())
            if not n:
                continue
            p_up = float(y[mask].mean())
            h = 0.0
            for p in (p_up, 1.0 - p_up):
                if p > 0:
                    h -= p * np.log2(p)
            entropy += n * h
            total += n
    return entropy / total if total else 0.0


def _nanmean_axis0(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    valid = np.isfinite(values)
    count = valid.sum(axis=0)
    total = np.nansum(values, axis=0)
    return np.divide(total, count, out=np.full(total.shape, np.nan, dtype=float), where=count > 0)


def build_four_d_state(panel: dict, spans: dict) -> dict:
    """Build lagged [momentum, order, synchronization, information-flow] axes."""
    symbols = panel["symbols"]
    sessions = list(panel["sessions"])
    n_sym, n_days = len(symbols), len(sessions)
    daily_close = np.full((n_sym, n_days), np.nan)
    for d, day in enumerate(sessions):
        bars = np.flatnonzero(panel["dates"] == day)
        for s in range(n_sym):
            px = panel["close"][s, bars]
            good = np.flatnonzero(np.isfinite(px) & (px > 0))
            if good.size:
                daily_close[s, d] = px[int(good[-1])]
    daily_ret = np.full_like(daily_close, np.nan)
    daily_ret[:, 1:] = daily_close[:, 1:] / daily_close[:, :-1] - 1.0
    # Point-in-time, equal-weight proxy made only from locally cached S&P names.
    members = np.array([
        [_is_member(spans, sym, day) for day in sessions]
        for sym in symbols
    ], dtype=bool)
    masked = np.where(members, daily_ret, np.nan)
    market_ret = _nanmean_axis0(masked)
    market_ret = np.where(np.isfinite(market_ret), market_ret, np.nan)

    # Static GICS-lite tags from the bot; unknown names use the cached-market proxy.
    try:
        from modules.dynamic_universe import EQUITY_SECTOR_MAP
        from modules.pipeline_strategies import NYSE_SECTOR_MAP
        sector_map = {**EQUITY_SECTOR_MAP, **NYSE_SECTOR_MAP}
    except Exception:
        sector_map = {}
    sectors = [sector_map.get(sym, "Other") for sym in symbols]
    sector_ret: dict[str, np.ndarray] = {}
    sector_indices: dict[str, np.ndarray] = {}
    for sector in set(sectors):
        idx = np.array([j for j, value in enumerate(sectors) if value == sector], dtype=int)
        sector_indices[sector] = idx
        values = _nanmean_axis0(daily_ret[idx]) if idx.size else np.full(n_days, np.nan)
        sector_ret[sector] = values

    order_score = np.full(n_days, np.nan)
    for d in range(1, n_days):
        history = market_ret[max(1, d - 30) : d]  # through yesterday only
        ent = _permutation_entropy(history, order=3)
        if ent is not None:
            order_score[d] = 1.0 - 2.0 * ent  # +1 ordered, -1 high-entropy

    sync = np.full((n_sym, n_days), np.nan)
    flow = np.full((n_sym, n_days), np.nan)
    for s, sym in enumerate(symbols):
        peers = sector_indices.get(sectors[s], np.array([], dtype=int))
        peers = peers[peers != s]
        if len(peers) >= 2 and sectors[s] != "Other":
            src = _nanmean_axis0(masked[peers])
        else:
            market_peers = np.flatnonzero(np.arange(n_sym) != s)
            src = _nanmean_axis0(masked[market_peers])
        for d in range(61, n_days):
            # Synchronization with sector, signed by the sector's recent direction.
            lo = max(1, d - 21)
            a = daily_ret[s, lo:d]
            b = src[lo:d]
            valid = np.isfinite(a) & np.isfinite(b)
            if valid.sum() >= 12:
                corr = float(np.corrcoef(a[valid], b[valid])[0, 1])
                sector_trend = float(np.nansum(src[max(1, d - 5) : d]))
                if np.isfinite(corr):
                    sync[s, d] = float(np.clip(corr * np.sign(sector_trend), -1.0, 1.0))

            # Transfer entropy from source sector direction to stock direction.
            # The feature signs the TE magnitude by incremental up-probability
            # for today's known source/stock state; all outcomes end yesterday.
            start = max(2, d - 60)
            if not np.isfinite(daily_ret[s, d - 1]) or not np.isfinite(src[d - 1]):
                continue
            y = np.sign(daily_ret[s, start:d])
            yp = np.sign(daily_ret[s, start - 1 : d - 1])
            xp = np.sign(src[start - 1 : d - 1])
            valid = np.isfinite(y) & np.isfinite(yp) & np.isfinite(xp)
            y, yp, xp = y[valid], yp[valid], xp[valid]
            if len(y) < 35:
                continue
            # Map negative/zero vs positive to binary states.
            yb, ypb, xpb = (y > 0).astype(int), (yp > 0).astype(int), (xp > 0).astype(int)
            te = max(0.0, _conditional_entropy_bits(yb, ypb) - _conditional_entropy_bits(yb, ypb, xpb))
            last_y_prev = int(np.sign(daily_ret[s, d - 1]) > 0)
            last_x_prev = int(np.sign(src[d - 1]) > 0) if np.isfinite(src[d - 1]) else None
            own = ypb == last_y_prev
            p_own = (float(yb[own].sum()) + 1.0) / (int(own.sum()) + 2.0) if own.any() else 0.5
            joint = own & (xpb == last_x_prev) if last_x_prev is not None else np.zeros_like(own)
            p_joint = (float(yb[joint].sum()) + 1.0) / (int(joint.sum()) + 2.0) if joint.any() else p_own
            lift = p_joint - p_own
            flow[s, d] = float(np.sign(lift) * min(te / 0.15, 1.0))

    panel["daily_close"] = daily_close
    panel["session_index"] = {day: i for i, day in enumerate(sessions)}
    panel["order_score"] = order_score
    panel["sync_score"] = sync
    panel["flow_score"] = flow
    panel["daily_ret"] = daily_ret
    panel["warmup_idx"] = 61
    return {
        "order_valid_days": int(np.isfinite(order_score).sum()),
        "sync_valid_cells": int(np.isfinite(sync).sum()),
        "flow_valid_cells": int(np.isfinite(flow).sum()),
        "sector_tagged_symbols": int(sum(x != "Other" for x in sectors)),
        "sector_count": len(set(sectors) - {"Other"}),
    }


def _rank_score(panel: dict, mode: str, s: int, i: int, day: object) -> float | None:
    px = panel["close"][s, i]
    avg = panel["ma"][s, i]
    rsi_value = panel["rsi"][s, i]
    if mode == "ma50_momentum":
        return float(px / avg - 1.0)
    if mode == "five_day_momentum":
        day_i = panel["session_index"][day]
        old_i = day_i - 5
        if old_i < 0:
            return None
        old_px = panel["daily_close"][s, old_i]
        return float(px / old_px - 1.0) if np.isfinite(old_px) and old_px > 0 else None
    if mode == "vol_adjusted_momentum":
        atr = panel["daily_atr"][panel["symbols"][s]].get(day)
        if atr is None or atr <= 0:
            return None
        return float((px / avg - 1.0) / max(float(atr) / float(px), 0.005))
    if mode == "rsi_reversal":
        return float(50.0 - rsi_value) if np.isfinite(rsi_value) else None
    if mode in ("state_low_entropy", "state_high_entropy"):
        day_i = panel["session_index"][day]
        m = float(np.tanh((px / avg - 1.0) / 0.03))
        order = float(panel["order_score"][day_i])
        if mode == "state_high_entropy":
            order = -order
        c = float(panel["sync_score"][s, day_i])
        f = float(panel["flow_score"][s, day_i])
        if not all(np.isfinite(v) for v in (m, order, c, f)):
            return None
        state_score = (m + order + c + f) / 4.0
        # Neutral is the predeclared gate; below it the strategy holds cash.
        return state_score if state_score > 0.0 else None
    if mode.startswith("cell:"):
        day_i = panel["session_index"][day]
        weights = CELL_WEIGHTS[int(mode.split(":", 1)[1])]
        m = float(np.tanh((px / avg - 1.0) / 0.03))
        order = float(panel["order_score"][day_i])
        c = float(panel["sync_score"][s, day_i])
        f = float(panel["flow_score"][s, day_i])
        if not all(np.isfinite(v) for v in (m, order, c, f)):
            return None
        score = float(np.dot(np.asarray(weights), np.asarray((m, order, c, f))))
        # A common neutral gate across vertices; negative scores remain in cash.
        return score if score > 0.0 else None
    raise ValueError(f"unknown rank mode: {mode}")


def simulate(panel: dict, spans: dict, top_n: int, *, take_profit: bool, rank_mode: str) -> dict:
    symbols = panel["symbols"]
    loc = panel["loc"]
    times = panel["times"]
    dates = panel["dates"]
    sessions = panel["sessions"]
    opens = panel["open"]
    highs = panel["high"]
    lows = panel["low"]
    closes = panel["close"]
    ma = panel["ma"]
    rsi = panel["rsi"]
    atr_map = panel["daily_atr"]
    n_sym, n_bars = closes.shape

    # Cache PIT membership by session. Only saved names that were index members
    # on that date can be considered.
    member_by_day = panel.get("_member_by_day")
    if member_by_day is None:
        member_by_day = {
            day: np.array([_is_member(spans, sym, day) for sym in symbols], dtype=bool)
            for day in pd.unique(dates)
        }
        panel["_member_by_day"] = member_by_day
    session_index = {day: i for i, day in enumerate(sessions)}
    signal_bar_by_day: dict = {}
    for i, ts in enumerate(times):
        if ts.time() >= SIGNAL_TIME and dates[i] not in signal_bar_by_day:
            signal_bar_by_day[dates[i]] = i

    # First open and previous close per symbol/session for the existing 2% gap
    # filter. This uses today's first bar only as an entry filter, never ranking.
    gap_by_symbol_day = panel.get("_gap_by_symbol_day")
    if gap_by_symbol_day is None:
        starts = np.r_[0, np.flatnonzero(dates[1:] != dates[:-1]) + 1]
        ends = np.r_[starts[1:] - 1, n_bars - 1]
        unique_days = [dates[i] for i in starts]
        gap_by_symbol_day = {}
        for sym_i, sym in enumerate(symbols):
            gaps = {}
            prev_close = None
            for day, start_i, end_i in zip(unique_days, starts, ends):
                op_window = opens[sym_i, start_i : end_i + 1]
                cp_window = closes[sym_i, start_i : end_i + 1]
                valid_op = np.flatnonzero(np.isfinite(op_window) & (op_window > 0))
                valid_cp = np.flatnonzero(np.isfinite(cp_window) & (cp_window > 0))
                first_open = float(op_window[valid_op[0]]) if valid_op.size else None
                last_close = float(cp_window[valid_cp[-1]]) if valid_cp.size else None
                if first_open is not None and prev_close:
                    gaps[day] = first_open / prev_close - 1.0
                if last_close is not None:
                    prev_close = last_close
            gap_by_symbol_day[sym] = gaps
        panel["_gap_by_symbol_day"] = gap_by_symbol_day

    cash = INITIAL
    positions: dict[int, dict] = {}
    trades: list[dict] = []
    fills: list[dict] = []
    equity_by_day: dict = {}
    entered_today: set[tuple] = set()

    def mark(i: int) -> float:
        total = cash
        for s, pos in positions.items():
            px = closes[s, i]
            if np.isfinite(px) and px > 0:
                total += pos["shares"] * float(px)
            else:
                total += pos["shares"] * pos["entry"]
        return float(total)

    def close_pos(s: int, i: int, raw_px: float, reason: str) -> None:
        nonlocal cash
        pos = positions[s]
        px = max(0.01, float(raw_px))
        fill = max(0.01, px * (1.0 - SLIP))
        proceeds = pos["shares"] * fill
        cash += proceeds
        trades.append({
            "symbol": symbols[s],
            "entry_date": str(pos["entry_day"]),
            "exit_date": str(dates[i]),
            "days_held": int((pd.Timestamp(dates[i]) - pd.Timestamp(pos["entry_day"])).days),
            "entry_rank": pos["rank"],
            "ret_pct": (fill / pos["entry"] - 1.0) * 100.0,
            "pnl_usd": proceeds - pos["notional"],
            "reason": reason,
            "notional": pos["notional"],
        })
        fills.append({
            "session": session_index[dates[i]],
            "side": "sell",
            "notional": proceeds,
            "cost": pos["shares"] * px * SLIP,
        })
        del positions[s]

    for i in range(n_bars - 1):
        day = dates[i]
        if i == 0 or dates[i - 1] != day:
            entered_today = set()

        # Manage existing positions using bar open/high/low. If both stop and
        # target fall inside one bar, resolve against the strategy at the stop.
        for s, pos in list(positions.items()):
            if i < pos["entry_i"]:
                continue
            op, hi, lo = opens[s, i], highs[s, i], lows[s, i]
            if not all(np.isfinite(v) and v > 0 for v in (op, hi, lo)):
                continue
            if op <= pos["stop"]:
                close_pos(s, i, float(op), "stop_gap")
                continue
            if take_profit and op >= pos["target"]:
                close_pos(s, i, float(op), "target_gap")
                continue
            if lo <= pos["stop"] and (not take_profit or hi < pos["target"]):
                close_pos(s, i, float(pos["stop"]), "stop")
                continue
            if take_profit and hi >= pos["target"]:
                if lo <= pos["stop"]:
                    close_pos(s, i, float(pos["stop"]), "stop_same_bar")
                else:
                    close_pos(s, i, float(pos["target"]), "target")
                continue
            if pd.Timestamp(day).date() >= pos["expiry"]:
                close_pos(s, i, float(op), "time")

        # One ranking pass per session, at 10:15 ET; fill at the next 5-minute
        # bar open. If there is no exact 10:15 print, use the first later bar.
        signal_i = signal_bar_by_day.get(day)
        if (signal_i == i and i + 1 < n_bars and dates[i + 1] == day
                and session_index[day] >= panel.get("warmup_idx", 0)):
            free_slots = top_n - len(positions)
            if free_slots > 0:
                member = member_by_day[day]
                candidates = []
                for s, sym in enumerate(symbols):
                    if s in positions or (day, s) in entered_today or not member[s]:
                        continue
                    px = closes[s, i]
                    avg = ma[s, i]
                    if not np.isfinite(px) or not np.isfinite(avg) or avg <= 0 or px <= avg:
                        continue
                    rsi_value = rsi[s, i]
                    if np.isfinite(rsi_value) and rsi_value >= RSI_MAX:
                        continue
                    gap = gap_by_symbol_day[sym].get(day)
                    if gap is not None and gap > 0.02:
                        continue
                    score = _rank_score(panel, rank_mode, s, i, day)
                    if score is not None and np.isfinite(score):
                        candidates.append((score, s))
                candidates.sort(reverse=True)
                equity_now = mark(i)
                budget = min(equity_now * MAX_POSITION_PCT, equity_now * SLEEVE_CAP / top_n)
                bought = 0
                for rank, (score, s) in enumerate(candidates, start=1):
                    if bought >= free_slots:
                        break
                    raw = opens[s, i + 1]
                    if not np.isfinite(raw) or raw <= 0:
                        continue
                    room = min(budget, cash)
                    if room <= 1:
                        break
                    fill = float(raw) * (1.0 + SLIP)
                    shares = room / fill
                    cost = shares * float(raw) * SLIP
                    cash -= shares * fill
                    atr = atr_map[symbols[s]].get(day)
                    if atr is None or atr <= 0:
                        atr = fill * 0.02
                    stop = max(0.01, fill - ATR_MULT * float(atr))
                    target = fill * (1.0 + TARGET_PCT)
                    positions[s] = {
                        "shares": shares,
                        "entry": fill,
                        "stop": stop,
                        "target": target,
                        "expiry": (pd.Timestamp(day) + pd.Timedelta(days=MAX_HOLD_DAYS)).date(),
                        "entry_i": i + 1,
                        "entry_day": dates[i + 1],
                        "rank": rank,
                        "notional": room,
                    }
                    entered_today.add((day, s))
                    fills.append({
                        "session": session_index[day],
                        "side": "buy",
                        "notional": room,
                        "cost": cost,
                    })
                    bought += 1

        # One closing mark per session.
        if i + 1 == n_bars or dates[i + 1] != day:
            equity_by_day[day] = mark(i)

    # Liquidate at the final available close so all books have comparable end
    # equity, charging the same sell slippage. A cached name can stop printing
    # before the panel's final timestamp, so use its last observed close and
    # count those terminal marks explicitly.
    last = n_bars - 1
    stale_terminal_marks = 0
    for s, pos in list(positions.items()):
        px = closes[s, last]
        if not np.isfinite(px) or px <= 0:
            valid = np.flatnonzero(np.isfinite(closes[s, : last + 1]) & (closes[s, : last + 1] > 0))
            if valid.size:
                px = closes[s, int(valid[-1])]
                stale_terminal_marks += 1
            else:
                px = pos["entry"]
                stale_terminal_marks += 1
        close_pos(s, last, float(px), "end_of_data")
    if len(sessions):
        equity_by_day[sessions[-1]] = cash
    ledger_equity = INITIAL + sum(t["pnl_usd"] for t in trades)
    accounting_gap = cash - ledger_equity
    if abs(accounting_gap) > 0.05:
        raise RuntimeError(
            f"trade ledger does not reconcile to cash: cash={cash:.2f}, "
            f"ledger={ledger_equity:.2f}, gap={accounting_gap:.2f}"
        )
    equity = [float(equity_by_day.get(day, INITIAL)) for day in sessions]
    return {
        "equity": equity,
        "sessions": [str(d) for d in sessions],
        "trades": trades,
        "fills": fills,
        "stale_terminal_marks": stale_terminal_marks,
        "end_equity": float(cash),
    }


def _metrics(result: dict, begin: int, end: int) -> dict:
    eq = np.asarray(result["equity"][begin:end], dtype=float)
    if not eq.size:
        return {}
    anchor = float(result["equity"][begin - 1]) if begin > 0 else INITIAL
    path = np.concatenate(([anchor], eq))
    returns = np.diff(path) / np.maximum(path[:-1], 1e-12)
    peak = np.maximum.accumulate(path)
    dd = path / np.maximum(peak, 1e-12) - 1.0
    sharpe = None
    if len(returns) >= 5 and returns.std(ddof=1) > 1e-12:
        sharpe = round(float(returns.mean() / returns.std(ddof=1) * np.sqrt(252)), 2)
    date_to_session = result.get("_date_to_session")
    if date_to_session is None:
        date_to_session = {day: i for i, day in enumerate(result["sessions"])}
        result["_date_to_session"] = date_to_session
    trades = [t for t in result["trades"] if begin <= date_to_session.get(t["entry_date"], -1) < end]
    fills = [f for f in result["fills"] if begin <= f["session"] < end]
    costs = sum(f["cost"] for f in fills)
    buy_notional = sum(f["notional"] for f in fills if f["side"] == "buy")
    sell_notional = sum(f["notional"] for f in fills if f["side"] == "sell")
    avg_equity = float(np.mean(path))
    return {
        "return_pct": round((eq[-1] / anchor - 1.0) * 100, 2),
        "max_dd_pct": round(float(dd.min()) * 100, 2),
        "sharpe": sharpe,
        "trades": len(trades),
        "win_pct": round(100 * np.mean([t["ret_pct"] > 0 for t in trades]), 1) if trades else None,
        "avg_trade_pct": round(float(np.mean([t["ret_pct"] for t in trades])), 2) if trades else None,
        "median_days_held": round(float(np.median([t["days_held"] for t in trades])), 1) if trades else None,
        "target_exits": sum(t["reason"].startswith("target") for t in trades),
        "stop_exits": sum(t["reason"].startswith("stop") for t in trades),
        "time_exits": sum(t["reason"] == "time" for t in trades),
        "round_trip_costs_usd": round(float(costs), 2),
        "turnover": round((buy_notional + sell_notional) / avg_equity, 2) if avg_equity else 0,
        "start_equity": round(anchor, 2),
        "end_equity": round(float(eq[-1]), 2),
    }


def _session_of(result: dict, date: str) -> int:
    # Entry dates always exist in the saved session vector.
    try:
        return result["sessions"].index(date)
    except ValueError:
        return -1


def main() -> int:
    global CELL_WEIGHTS
    CELL_WEIGHTS = build_600_cell_vertices()
    panel, meta = load_intraday_panel()
    membership = load_sp500_membership()
    spans = _member_on(membership)
    feature_audit = build_four_d_state(panel, spans)
    n_sessions = len(panel["sessions"])
    split = max(1, int(n_sessions * 0.75))
    windows = {"full": (0, n_sessions), "first_75pct_train": (0, split), "latest_25pct_holdout": (split, n_sessions)}
    dots = np.asarray(CELL_WEIGHTS) @ np.asarray(CELL_WEIGHTS).T
    np.fill_diagonal(dots, -np.inf)
    edge_count = int(np.sum(np.isclose(dots, np.max(dots), atol=1e-12)) // 2)
    if edge_count != 720:
        raise RuntimeError(f"600-cell should have 720 edges; got {edge_count}")
    if OUT_CHECKPOINT.exists():
        raise FileExistsError(f"Refusing to overwrite prior checkpoint: {OUT_CHECKPOINT}")

    result_rows: list[dict] = []
    cell_rows: list[dict] = []
    strategies = [("ma50_momentum", None)] + [(f"cell:{i}", i) for i in range(len(CELL_WEIGHTS))]
    for run_i, (rank_mode, cell_i) in enumerate(strategies, start=1):
        result = simulate(panel, spans, 5, take_profit=False, rank_mode=rank_mode)
        key = "ma50_baseline_top5" if cell_i is None else f"600cell_{cell_i:03d}_top5"
        weight_values = None if cell_i is None else list(CELL_WEIGHTS[cell_i])
        metrics_by_window = {}
        for name, (b, e) in windows.items():
            metrics = _metrics(result, b, e)
            metrics_by_window[name] = metrics
            result_rows.append({"strategy": key, "cell_index": cell_i, "weights_MOCF": weight_values,
                                "window": name, **metrics})
        summary = {
            "strategy": key, "cell_index": cell_i, "weights_MOCF": weight_values,
            "train_return_pct": metrics_by_window["first_75pct_train"].get("return_pct"),
            "train_max_dd_pct": metrics_by_window["first_75pct_train"].get("max_dd_pct"),
            "holdout_return_pct": metrics_by_window["latest_25pct_holdout"].get("return_pct"),
            "holdout_max_dd_pct": metrics_by_window["latest_25pct_holdout"].get("max_dd_pct"),
            "full_return_pct": metrics_by_window["full"].get("return_pct"),
            "full_max_dd_pct": metrics_by_window["full"].get("max_dd_pct"),
            "stale_terminal_marks": result["stale_terminal_marks"],
        }
        if cell_i is not None:
            cell_rows.append(summary)
        with OUT_CHECKPOINT.open("a", encoding="utf-8") as checkpoint:
            checkpoint.write(json.dumps(summary, separators=(",", ":")) + "\n")
        if run_i == 1 or run_i % 10 == 0 or run_i == len(strategies):
            print(f"Completed {run_i}/{len(strategies)} replays; last={key}; train={summary['train_return_pct']}%", flush=True)

    ranked = sorted(cell_rows, key=lambda r: (r["train_return_pct"] is not None, r["train_return_pct"]), reverse=True)
    top_by_train = ranked[:5]
    baseline = next(r for r in result_rows if r["strategy"] == "ma50_baseline_top5" and r["window"] == "first_75pct_train")

    report = {
        "research_only": True,
        "data": meta,
        "assumptions": {
            "signal": "At most one scan per session at/after 10:15 ET; first available 5-minute close.",
            "geometry": {
                "name": "regular 600-cell (hexacosichoron)",
                "ambient_space": "R4; vertices lie on unit S3",
                "vertices": len(CELL_WEIGHTS),
                "edges": edge_count,
                "construction": "8 axis points + 16 half-coordinate points + 96 even-permutation golden-ratio points",
                "weight_order": ["M", "O", "C", "F"],
                "interpretation": "Each vertex is one unit-length direction for the four signal weights; not an exhaustive search of model orderings or algorithm settings.",
            },
            "four_coordinates": {
                "M": "tanh((10:15 close / trailing MA50 - 1) / 3%); stock trend direction/strength",
                "O": "1 - 2 * normalized order-3 permutation entropy of cached-member equal-weight daily returns; through prior close",
                "C": "20-session stock-vs-sector correlation, signed by prior 5-session sector direction; leave-one-stock-out sector proxy",
                "F": "signed transfer-entropy feature from prior 60 sessions: TE(Y_stock; X_sector | Y_stock_lag), signed by incremental conditional up-probability lift",
                "score": "vertex dot [M,O,C,F]; require score > 0; otherwise hold the slot in cash",
            },
            "feature_audit": feature_audit,
            "scope_note": "Research replay of lagged price-derived features. Sector labels are the bot's static GICS-lite map; names without a tag use a leave-one-out cached-member market proxy. It does not use ARIMA, HMM/Markov, full social archives, RVOL, catalysts, or insider snapshots.",
            "filters": "PIT S&P 500 membership, price above MA50, RSI(14) below 70, opening gap <=2%.",
            "entry": "Next same-session 5-minute bar open plus 5 bps slippage.",
            "positioning": "Top five open-position cap; max 15% equity per name; 95% gross sleeve cap. Position size is fixed by the existing simulator and does not vary with signal conviction.",
            "target": "Disabled; all variants use stop and 30-day maximum hold only.",
            "stop": "2x lagged 14-session mean absolute daily close change; gap-through stop fills at bar open.",
            "max_hold": f"30 calendar days; exit at first bar open on/after expiry.",
            "objective": "Rank 600-cell vertices by net return on the first 75% of history. Evaluate the chronological latest 25% as holdout; report drawdown and costs as diagnostics. No fixed take-profit target.",
            "costs": "5 bps per side slippage (10 bps round trip).",
            "scanner_boosts": "Not included; historical RVOL/ORB/catalyst/insider rank boosts are not reproducible from saved cache.",
            "universe_limit": "Only locally cached names are available; this is not full-universe coverage.",
        },
        "windows": {k: {"start": str(panel["sessions"][b]), "end": str(panel["sessions"][e-1]), "sessions": e-b} for k, (b,e) in windows.items() if e > b},
        "results": result_rows,
        "training_ranked_top5": top_by_train,
        "baseline_train": baseline,
        "checkpoint": str(OUT_CHECKPOINT),
    }
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = [
        "# 600-cell four-signal weight sweep — research only",
        "",
        f"Data: {meta['symbols']} cached names · {meta['sessions']} sessions · {meta['start']} to {meta['end']}.",
        "",
        "Each of the 120 regular 600-cell vertices defines unit weights over M/O/C/F = momentum, market order, synchronization, and information flow. The score is the vertex dot those four features; scores at or below zero stay in cash. This tests weight directions only, not every algorithm order or tuning.",
        "",
        "All returns include 5 bps slippage on each side. The take-profit is disabled; the current ATR stop and 30-day time exit remain. Position size is capped at 15% of equity and the sleeve at 95%.",
        "",
        "## MA50 baseline",
        "",
        "| Window | Return | Max DD | Sharpe | Trades | Costs |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for r in result_rows:
        if r["strategy"] != "ma50_baseline_top5":
            continue
        lines.append(
            f"| {r['window']} | {r['return_pct']:+.2f}% | {r['max_dd_pct']:.2f}% | "
            f"{r['sharpe']} | {r['trades']} | ${r['round_trip_costs_usd']:.0f} |"
        )
    lines.extend([
        "",
        "The JSON report contains every metric for all 120 vertices across all three windows.",
        "",
        "## Five leaders ranked on the first 75%",
        "",
        "| Cell | Weights [M,O,C,F] | Train return | Train DD | Holdout return | Holdout DD | Full return |",
        "|---:|---|---:|---:|---:|---:|---:|",
    ])
    for r in top_by_train:
        lines.append(
            f"| {r['cell_index']} | `{r['weights_MOCF']}` | {r['train_return_pct']:+.2f}% | "
            f"{r['train_max_dd_pct']:.2f}% | {r['holdout_return_pct']:+.2f}% | "
            f"{r['holdout_max_dd_pct']:.2f}% | {r['full_return_pct']:+.2f}% |"
        )
    lines.extend([
        "",
        f"MA50 baseline training return: {baseline['return_pct']:+.2f}% (max DD {baseline['max_dd_pct']:.2f}%).",
        "",
        "## Reading the result",
        "",
        "Feature construction uses prior-session data for entropy, synchronization, and information flow; stock momentum is observed at the 10:15 signal bar. The holdout has now been evaluated and should not be reused for another tuning pass.",
        "",
        "The transfer-entropy estimate is noisy on a 60-session window, the sector map is static, and the universe contains only locally cached symbols. A gain is a reason to validate and expand coverage, not evidence to switch live or paper settings.",
        "",
        "Coverage is limited: the cache has 104 names; the companion walk-forward audit found only 44 of 503 S&P members cached on a representative date (8.7%). Historical scanner boosts are excluded. A few positions at the final timestamp use their last available cached close; counts are in the JSON. Fills use flat slippage and are estimates.",
        "",
        "This is a historical research result, not a live setting change.",
        "",
    ])
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT_MD}")
    print(f"Wrote {OUT_JSON}")
    print("\n".join(lines[:lines.index("## Reading the result")]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
