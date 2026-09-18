"""Daily-bar A/B of Lab add policy (research only).

Not full `backtester.py` (no VTI / shorts / crypto). Same NYSE MA70 rank
entry as `one_r_hit_backtest.py`. Current Lab stack:

  - 8 names, 15% of equity each
  - disaster -10% close
  - 30-bar time stop
  - half at +20% close, trail 8% off high once up +10% from entry
  - no same-day rebuy

A/B (one knob):

  - room:  add only leftover under the 15% name cap
  - fresh: size a re-signal as if the name were flat, hard cap 30%

Usage (from stock-bot/):
  python scripts/analysis/paper_lab_concentrated_backtest.py --days 365
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

import config  # noqa: E402
from modules.data_loader import load_close_matrix  # noqa: E402
from modules.one_r_hit_test import plan_from_entry, walk_path  # noqa: E402
from modules.risk_management import calculate_atr  # noqa: E402

OUT_JSON = Path(__file__).with_name("paper_lab_concentrated_backtest_last.json")
SKIP = frozenset({"VTI", "VOO", "SPY", "QQQ", "BND", "AGG"})


def _universe(columns) -> list[str]:
    cols = config.nyse_momentum_universe(columns)
    out = []
    for c in cols:
        n = config.normalize_symbol(c)
        if not n or n in SKIP or config.is_crypto(n):
            continue
        out.append(n)
    return out


def _atr_at(data: pd.DataFrame, symbol: str, i: int) -> float | None:
    window = data[symbol].iloc[: i + 1].to_frame(name=symbol)
    return calculate_atr(window, symbol)


def _summarize(trades: list[dict], equity: float, max_dd: float, start, end, bars, n_sym) -> dict:
    n = len(trades)
    rets = [t["ret_pct"] for t in trades]
    win = sum(1 for r in rets if r > 0)
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    return {
        "start": start,
        "end": end,
        "bars": bars,
        "symbols": n_sym,
        "trades": n,
        "win_rate": round(100.0 * win / n, 1) if n else 0.0,
        "avg_ret_pct": round(float(np.mean(rets)), 3) if rets else 0.0,
        "med_ret_pct": round(float(np.median(rets)), 3) if rets else 0.0,
        "equity_end": round(equity, 2),
        "book_ret_pct": round((equity / 100_000.0 - 1.0) * 100.0, 2),
        "max_dd_pct": round(max_dd * 100.0, 2),
        "exit_reasons": reasons,
        "trades_sample": trades[:12],
    }


def run_lab(
    data: pd.DataFrame,
    *,
    ma_win: int,
    warmup: int,
    max_names: int = 8,
    name_pct: float = 0.15,
    disaster: float = 0.10,
    half_gain: float = 0.20,
    trail_arm: float = 0.10,
    trail_pct: float = 0.08,
    max_hold: int = 30,
    add_policy: str = "room",
    add_max_mult: float = 2.0,
) -> tuple[dict, list[dict]]:
    symbols = [c for c in _universe(data.columns) if c in data.columns]
    frame = data[symbols].apply(pd.to_numeric, errors="coerce")
    ma = frame.rolling(ma_win, min_periods=ma_win).mean()
    mom = frame / ma - 1.0
    above = frame > ma

    open_pos: dict[str, dict] = {}
    trades: list[dict] = []
    curve: list[dict] = []
    equity = 100_000.0
    peak_eq = equity
    max_dd = 0.0
    add_count = 0
    start_i = max(warmup, ma_win + 15)

    def mark_trade(sym, pos, exit_i, exit_px, reason, frac=1.0):
        nonlocal equity
        qty = pos["qty"] * frac
        ret = exit_px / pos["entry"] - 1.0
        pnl = qty * (exit_px - pos["entry"])
        equity += pnl
        trades.append(
            {
                "symbol": sym,
                "entry_date": str(frame.index[pos["entry_i"]].date()),
                "exit_date": str(frame.index[exit_i].date()),
                "bars": int(exit_i - pos["entry_i"]),
                "entry": round(pos["entry"], 4),
                "exit": round(exit_px, 4),
                "ret_pct": round(ret * 100.0, 3),
                "pnl": round(pnl, 2),
                "frac": frac,
                "reason": reason,
            }
        )

    for i in range(start_i, len(frame)):
        px_row = frame.iloc[i]
        closed_today: list[str] = []
        for sym, pos in list(open_pos.items()):
            px = float(px_row.get(sym, np.nan))
            if not np.isfinite(px) or px <= 0:
                continue
            pos["peak"] = max(float(pos["peak"]), px)
            pnl_pct = px / pos["entry"] - 1.0
            held = i - pos["entry_i"]
            if pnl_pct <= -disaster:
                mark_trade(sym, pos, i, px, "lab_disaster")
                closed_today.append(sym)
                continue
            if held >= max_hold:
                mark_trade(sym, pos, i, px, "lab_time")
                closed_today.append(sym)
                continue
            peak_gain = pos["peak"] / pos["entry"] - 1.0
            if peak_gain >= trail_arm:
                pos["trail_armed"] = True
            if not pos["half_taken"] and pnl_pct >= half_gain:
                mark_trade(sym, pos, i, px, "lab_half", frac=0.5)
                pos["qty"] *= 0.5
                pos["half_taken"] = True
            if (pos["trail_armed"] or pos["half_taken"]) and px <= pos["peak"] * (
                1.0 - trail_pct
            ):
                mark_trade(sym, pos, i, px, "lab_trail")
                closed_today.append(sym)
        for sym in closed_today:
            open_pos.pop(sym, None)

        peak_eq = max(peak_eq, equity)
        dd = (peak_eq - equity) / peak_eq if peak_eq > 0 else 0.0
        max_dd = max(max_dd, dd)
        curve.append(
            {
                "date": str(frame.index[i].date()),
                "equity": round(equity, 2),
                "open": len(open_pos),
            }
        )

        if i >= len(frame) - 1:
            break
        scores = []
        row_above = above.iloc[i]
        row_mom = mom.iloc[i]
        for sym in symbols:
            if sym in closed_today:
                continue
            if not bool(row_above.get(sym, False)):
                continue
            m = row_mom.get(sym)
            if m is None or not np.isfinite(m) or m <= 0:
                continue
            scores.append((float(m), sym))
        scores.sort(reverse=True)
        clip = equity * name_pct
        cap_val = equity * name_pct
        hard_val = cap_val * add_max_mult
        min_add = clip * 0.25
        deployed = 0.0
        for held_sym, held_pos in open_pos.items():
            held_px = float(px_row.get(held_sym, np.nan))
            if np.isfinite(held_px) and held_px > 0:
                deployed += held_pos["qty"] * held_px
        cash_room = max(0.0, equity - deployed)
        for _m, sym in scores:
            px = float(frame[sym].iloc[i])
            if not np.isfinite(px) or px <= 0:
                continue
            pos = open_pos.get(sym)
            if pos is None:
                if len(open_pos) >= max_names:
                    continue
                ticket = min(clip, cash_room)
                if ticket < min_add:
                    continue
                open_pos[sym] = {
                    "entry_i": i,
                    "entry": px,
                    "qty": ticket / px,
                    "peak": px,
                    "half_taken": False,
                    "trail_armed": False,
                    "adds": 0,
                }
                cash_room -= ticket
                continue
            mv = pos["qty"] * px
            if add_policy == "fresh":
                room = min(clip, hard_val - mv, cash_room)
            else:
                room = min(cap_val - mv, cash_room)
            if room < min_add:
                continue
            add_qty = room / px
            old_cost = pos["qty"] * pos["entry"]
            pos["qty"] += add_qty
            pos["entry"] = (old_cost + room) / pos["qty"]
            pos["peak"] = max(float(pos["peak"]), px)
            pos["adds"] = int(pos.get("adds") or 0) + 1
            add_count += 1
            cash_room -= room

    last_i = len(frame) - 1
    for sym, pos in list(open_pos.items()):
        px = float(frame[sym].iloc[last_i])
        if not np.isfinite(px) or px <= 0:
            continue
        mark_trade(sym, pos, last_i, px, "eod")

    summary = _summarize(
        trades,
        equity,
        max_dd,
        str(frame.index[start_i].date()),
        str(frame.index[-1].date()),
        int(len(frame) - start_i),
        len(symbols),
    )
    summary["add_policy"] = add_policy
    summary["adds"] = add_count
    summary["curve"] = curve[:: max(1, len(curve) // 80)]
    summary["curve_len"] = len(curve)
    return summary, trades


def run_atr_hold(
    data: pd.DataFrame,
    *,
    ma_win: int,
    max_hold: int,
    max_active: int,
    warmup: int,
    name_pct: float | None = None,
) -> dict:
    """Same entries as one_r_hit hold (ATR stop, 30d), optional 4-name 25% sizing."""
    symbols = [c for c in _universe(data.columns) if c in data.columns]
    frame = data[symbols].apply(pd.to_numeric, errors="coerce")
    ma = frame.rolling(ma_win, min_periods=ma_win).mean()
    mom = frame / ma - 1.0
    above = frame > ma
    open_pos: dict[str, dict] = {}
    trades: list[dict] = []
    equity = 100_000.0
    peak = equity
    max_dd = 0.0
    start_i = max(warmup, ma_win + 15)
    sleeve = 0.67

    for i in range(start_i, len(frame) - 1):
        closed_today: list[str] = []
        for sym, pos in open_pos.items():
            series = frame[sym].to_numpy(dtype=float)
            exit_i, exit_px, reason, _touched = walk_path(
                series,
                pos["entry_i"],
                pos["stop"],
                pos["target"],
                max_hold,
                take_1r=False,
            )
            if exit_i > i:
                continue
            ret = exit_px / pos["entry"] - 1.0
            equity += pos["notional"] * ret
            trades.append(
                {
                    "symbol": sym,
                    "entry_date": str(frame.index[pos["entry_i"]].date()),
                    "exit_date": str(frame.index[exit_i].date()),
                    "bars": int(exit_i - pos["entry_i"]),
                    "entry": round(pos["entry"], 4),
                    "exit": round(exit_px, 4),
                    "ret_pct": round(ret * 100.0, 3),
                    "reason": reason,
                }
            )
            closed_today.append(sym)
        for sym in closed_today:
            open_pos.pop(sym, None)
        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        if len(open_pos) >= max_active:
            continue
        scores = []
        row_above = above.iloc[i]
        row_mom = mom.iloc[i]
        for sym in symbols:
            if sym in open_pos:
                continue
            if not bool(row_above.get(sym, False)):
                continue
            m = row_mom.get(sym)
            if m is None or not np.isfinite(m) or m <= 0:
                continue
            scores.append((float(m), sym))
        scores.sort(reverse=True)
        slots = max_active - len(open_pos)
        clip = (
            equity * name_pct
            if name_pct is not None
            else (equity * sleeve) / max_active
        )
        for _m, sym in scores[:slots]:
            px = float(frame[sym].iloc[i])
            if not np.isfinite(px) or px <= 0:
                continue
            atr = _atr_at(frame, sym, i)
            if atr is None or atr <= 0:
                continue
            plan = plan_from_entry(px, atr, rr=1.0)
            open_pos[sym] = {
                "entry_i": i,
                "entry": px,
                "stop": plan["stop"],
                "target": plan["target"],
                "notional": clip,
            }

    last_i = len(frame) - 1
    for sym, pos in list(open_pos.items()):
        px = float(frame[sym].iloc[last_i])
        if not np.isfinite(px) or px <= 0:
            continue
        ret = px / pos["entry"] - 1.0
        equity += pos["notional"] * ret
        trades.append(
            {
                "symbol": sym,
                "ret_pct": round(ret * 100.0, 3),
                "reason": "eod",
            }
        )
    return _summarize(
        trades,
        equity,
        max_dd,
        str(frame.index[start_i].date()),
        str(frame.index[-1].date()),
        int(len(frame) - start_i),
        len(symbols),
    )


def _spy_bh(data: pd.DataFrame, start: str, end: str) -> float | None:
    if "SPY" not in data.columns:
        return None
    s = pd.to_numeric(data["SPY"], errors="coerce")
    idx = data.index
    mask = (idx.date >= pd.to_datetime(start).date()) & (
        idx.date <= pd.to_datetime(end).date()
    )
    ser = s.loc[mask].dropna()
    if len(ser) < 2:
        return None
    return round(float(ser.iloc[-1] / ser.iloc[0] - 1.0) * 100.0, 2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=365)
    args = ap.parse_args()
    ma_win = int(getattr(config, "PAPER_NYSE_MA_WINDOW", 70))
    need = args.days + ma_win + 40
    print(f"Loading daily closes (~{need} rows, MA{ma_win})...")
    data = load_close_matrix(interval="1d", days=need)
    if data is None or data.empty or len(data) < ma_win + 30:
        print(f"Not enough daily history: {0 if data is None else len(data)} bars")
        return 1
    data = data.tail(need)
    warmup = ma_win + 15
    lab_room, _ = run_lab(data, ma_win=ma_win, warmup=warmup, add_policy="room")
    lab_fresh, _ = run_lab(data, ma_win=ma_win, warmup=warmup, add_policy="fresh")
    atr15 = run_atr_hold(
        data, ma_win=ma_win, max_hold=30, max_active=15, warmup=warmup
    )
    spy = _spy_bh(data, lab_room["start"], lab_room["end"])
    payload = {
        "research_only": True,
        "note": (
            "Isolated NYSE sleeve on $100k, close-to-close. "
            "A/B is Lab leftover-room vs Lab fresh-ticket (2× cap). "
            "Not v2/live."
        ),
        "lab_room": lab_room,
        "lab_fresh": lab_fresh,
        "atr_hold_15": atr15,
        "spy_bh_pct": spy,
    }
    # shrink curve in json for file size
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print()
    print(
        f"Window: {lab_room['start']} -> {lab_room['end']}  "
        f"({lab_room['bars']} bars, {lab_room['symbols']} names)"
    )
    print(
        f"{'policy':<16} {'trades':>7} {'adds':>6} {'win%':>7} "
        f"{'avg%':>8} {'book%':>8} {'maxDD%':>8}  exits"
    )
    for label, r in (
        ("lab_room", lab_room),
        ("lab_fresh", lab_fresh),
        ("atr_hold_15", atr15),
    ):
        er = r["exit_reasons"]
        exits = " ".join(f"{k}={v}" for k, v in sorted(er.items()))
        print(
            f"{label:<16} {r['trades']:7d} {r.get('adds', 0):6d} {r['win_rate']:6.1f} "
            f"{r['avg_ret_pct']:7.2f} {r['book_ret_pct']:7.2f} {r['max_dd_pct']:7.2f}  {exits}"
        )
    if spy is not None:
        print(f"SPY buy-hold same window: {spy:.2f}%")
    print(f"Wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
