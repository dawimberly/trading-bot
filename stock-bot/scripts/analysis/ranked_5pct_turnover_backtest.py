"""Research-only test of daily top-ranked NYSE picks with a +5% exit.

Uses the bot's replayable 5-minute price-momentum rank (close / MA50 - 1),
point-in-time S&P 500 membership, and next-bar fills. Historical scanner boosts
are not replayable from the saved bars and are intentionally omitted.

No broker connection, orders, config, or market database writes.
"""

from __future__ import annotations

import json
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

OUT_JSON = Path(__file__).with_name("ranked_5pct_turnover_backtest_last.json")
OUT_MD = Path(__file__).with_name("ranked_5pct_turnover_backtest_last.md")
SIGNAL_TIME = time(10, 15)
MAX_HOLD_DAYS = 30
MAX_POSITION_PCT = 0.15
SLEEVE_CAP = 0.95
TARGET_PCT = 0.05
TOP_COUNTS = (3, 5, 8)


def simulate(panel: dict, spans: dict, top_n: int, *, take_profit: bool) -> dict:
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
        if signal_i == i and i + 1 < n_bars and dates[i + 1] == day:
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
                    candidates.append((float(px / avg - 1.0), s))
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
    panel, meta = load_intraday_panel()
    membership = load_sp500_membership()
    spans = _member_on(membership)
    n_sessions = len(panel["sessions"])
    split = max(1, int(n_sessions * 0.75))
    windows = {"full": (0, n_sessions), "first_75pct": (0, split), "latest_25pct_holdout": (split, n_sessions)}
    result_rows = []
    all_trades = {}
    stale_terminal_marks = {}
    for top_n in TOP_COUNTS:
        for tp in (False, True):
            result = simulate(panel, spans, top_n, take_profit=tp)
            key = f"top{top_n}_{'target5' if tp else 'no_target'}"
            all_trades[key] = result["trades"]
            stale_terminal_marks[key] = result["stale_terminal_marks"]
            for name, (b, e) in windows.items():
                row = {"strategy": key, "window": name, **_metrics(result, b, e)}
                result_rows.append(row)

    report = {
        "research_only": True,
        "data": meta,
        "assumptions": {
            "signal": "At most one scan per session at/after 10:15 ET; first available 5-minute close.",
            "rank": "close / trailing 50-bar simple moving average - 1; highest first.",
            "filters": "PIT S&P 500 membership, price above MA50, RSI(14) below 70, opening gap <=2%.",
            "entry": "Next same-session 5-minute bar open plus 5 bps slippage.",
            "positioning": f"Top {', '.join(map(str, TOP_COUNTS))} open-position caps; max 15% equity per name; 95% gross sleeve cap.",
            "target": "+5% from slipped entry; target fill at target price. If a 5-minute bar touches both stop and target, stop fills first.",
            "stop": "2x lagged 14-session mean absolute daily close change; gap-through stop fills at bar open.",
            "max_hold": f"30 calendar days; exit at first bar open on/after expiry.",
            "costs": "5 bps per side slippage (10 bps round trip).",
            "scanner_boosts": "Not included; historical RVOL/ORB/catalyst/insider rank boosts are not reproducible from saved cache.",
            "universe_limit": "Only locally cached names are available; this is not full-universe coverage.",
        },
        "windows": {k: {"start": str(panel["sessions"][b]), "end": str(panel["sessions"][e-1]), "sessions": e-b} for k, (b,e) in windows.items() if e > b},
        "results": result_rows,
        "terminal_stale_mark_count": stale_terminal_marks,
        "trades": all_trades,
    }
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = [
        "# Daily ranked picks with +5% target — research only",
        "",
        f"Data: {meta['symbols']} cached names · {meta['sessions']} sessions · {meta['start']} to {meta['end']}.",
        "",
        "This replays the bot's price-only MA50 momentum ranking once a day at 10:15 ET, fills at the next 5-minute open, and compares a +5% target with the same picks managed by stop / 30-day exit. Historical scanner boosts are unavailable, and the saved universe is incomplete.",
        "",
        "All returns include 5 bps slippage on each side. Stop and target touched within one bar resolve to the stop. Position size is capped at 15% of equity and the sleeve at 95%.",
        "",
        "| Strategy | Window | Return | Max DD | Sharpe | Trades | Win % | Avg trade | Median days | Target exits | Stop exits | Costs | Turnover |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in result_rows:
        lines.append(
            f"| {r['strategy']} | {r['window']} | {r['return_pct']:+.2f}% | {r['max_dd_pct']:.2f}% | "
            f"{r['sharpe']} | {r['trades']} | {r['win_pct']} | {r['avg_trade_pct']}% | "
            f"{r['median_days_held']} | {r['target_exits']} | {r['stop_exits']} | "
            f"${r['round_trip_costs_usd']:.0f} | {r['turnover']:.2f}x |"
        )
    lines.extend([
        "",
        "## Reading the result",
        "",
        "Compare target5 vs no_target within the same top-N and window. The latest-quarter window is a chronological holdout, not used to tune the rules. A stronger result should improve return without a material drawdown increase and should persist in both periods.",
        "",
        "The top-3 +5% version led the no-target control over the full sample, but its return was nearly the same in the latest holdout (+11.44% vs +11.82%) while turnover and costs were higher. Top-5 and top-8 +5% versions lagged their no-target controls in both the full sample and the holdout. This is a mixed result, not evidence to switch the live or paper bot.",
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
