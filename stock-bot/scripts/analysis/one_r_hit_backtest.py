"""Daily-bar backtest of NYSE MA momentum exit policies (research only).

Not a full `backtester.py` book (no VTI/stat-arb/regime). Same entry idea as
paper NYSE: price above MA70, rank by distance-to-MA, max 15 names, default
30-bar hold, stop = max(2.0× daily ATR, 1% of entry).

Policies
  hold:       exit at stop or max-hold (baseline)
  take_1r:    exit at first close >= 1R target, else same as hold
  wide_stop:  3.0× ATR stop (same hold, no early 1R)
  long_hold:  45-bar max hold (2.0× ATR, no early 1R)

Usage (from stock-bot/):
  python scripts/analysis/one_r_hit_backtest.py --days 365
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

OUT_JSON = Path(__file__).with_name("one_r_hit_backtest_last.json")
OUT_MD = Path(__file__).with_name("one_r_hit_backtest_last.md")
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


def run_backtest(
    data: pd.DataFrame,
    *,
    ma_win: int,
    max_hold: int,
    max_active: int,
    warmup: int,
    take_1r: bool,
    stop_multiplier: float | None = None,
    friday_flatten: bool = False,
    opens: pd.DataFrame | None = None,
    open_gaps: pd.DataFrame | None = None,
    gain_exit_pct: float | None = None,
    gain_exit_mode: str | None = None,
    fat_loser: bool = False,
    fat_loser_open_pct: float = -0.01,
    fat_loser_target_pct: float = 0.005,
    cost_rt_bps: float = 0.0,
    stall_rotate: bool = False,
    stall_arm_pct: float = 0.05,
    stall_flat_bars: int = 3,
    stall_mom_edge: float = 0.0,
    stall_mode: str = "flat",
    stall_band: float = 0.02,
) -> dict:
    symbols = [c for c in _universe(data.columns) if c in data.columns]
    frame = data[symbols].apply(pd.to_numeric, errors="coerce")
    ma = frame.rolling(ma_win, min_periods=ma_win).mean()
    mom = frame / ma - 1.0
    above = frame > ma

    open_pos: dict[str, dict] = {}
    trades: list[dict] = []
    equity = 100_000.0
    sleeve = 0.67
    peak = equity
    max_dd = 0.0
    start_i = max(warmup, ma_win + 15)
    side_cost = max(0.0, float(cost_rt_bps)) / 2.0 / 10_000.0
    rotate_pairs: list[dict] = []
    flat_n = max(1, int(stall_flat_bars))

    def _fwd(sym: str, i: int, horizon: int) -> float | None:
        j = i + horizon
        if j >= len(frame):
            return None
        a = float(frame[sym].iloc[i])
        b = float(frame[sym].iloc[j])
        if not np.isfinite(a) or not np.isfinite(b) or a <= 0 or b <= 0:
            return None
        return b / a - 1.0

    for i in range(start_i, len(frame) - 1):
        closed_today: list[str] = []
        fade_skip: set[str] = set()
        is_friday = int(frame.index[i].weekday()) == 4
        if gain_exit_pct and gain_exit_mode and i >= 2:
            wd = int(frame.index[i].weekday())
            apply_fade = (
                gain_exit_mode == "all_days"
                or (gain_exit_mode == "skip_monday" and wd != 0)
                or (gain_exit_mode == "thu_only" and wd == 3)
            )
            if apply_fade:
                thr = float(gain_exit_pct) / 100.0
                for sym, pos in list(open_pos.items()):
                    if i <= int(pos["entry_i"]):
                        continue
                    yest = float(frame[sym].iloc[i - 1])
                    prev = float(frame[sym].iloc[i - 2])
                    if not np.isfinite(yest) or not np.isfinite(prev) or prev <= 0:
                        continue
                    if (yest / prev - 1.0) < thr:
                        continue
                    exit_px = float("nan")
                    if open_gaps is not None and sym in open_gaps.columns:
                        g = float(open_gaps[sym].iloc[i])
                        if np.isfinite(g) and 0.5 < g < 1.5:
                            exit_px = yest * g
                    elif opens is not None and sym in opens.columns:
                        ox = float(opens[sym].iloc[i])
                        if np.isfinite(ox) and ox > 0:
                            exit_px = ox
                    if not np.isfinite(exit_px):
                        continue
                    ret = exit_px / pos["entry"] - 1.0
                    equity += pos["notional"] * ret
                    equity -= pos["notional"] * side_cost
                    trades.append(
                        {
                            "symbol": sym,
                            "entry_date": str(frame.index[pos["entry_i"]].date()),
                            "exit_date": str(frame.index[i].date()),
                            "bars": int(i - pos["entry_i"]),
                            "entry": round(pos["entry"], 4),
                            "exit": round(exit_px, 4),
                            "target_pct": round(pos["target_pct"] * 100.0, 3),
                            "ret_pct": round(ret * 100.0, 3),
                            "reason": "gain_fade",
                            "touched_1r": exit_px >= pos["target"],
                        }
                    )
                    closed_today.append(sym)
                    fade_skip.add(sym)
                for sym in closed_today:
                    open_pos.pop(sym, None)
        if fat_loser and open_gaps is not None and i >= 1:
            thr = float(fat_loser_open_pct)
            target_notional = max(0.0, equity * float(fat_loser_target_pct))
            for sym, pos in list(open_pos.items()):
                if i <= int(pos["entry_i"]):
                    continue
                yest = float(frame[sym].iloc[i - 1])
                if not np.isfinite(yest) or yest <= 0:
                    continue
                g = float(open_gaps[sym].iloc[i]) if sym in open_gaps.columns else float("nan")
                if not np.isfinite(g) or g <= 0.5 or g >= 1.5:
                    continue
                gap_ret = g - 1.0
                if gap_ret > thr:
                    continue
                exit_px = yest * g
                sold = float(pos["notional"]) - target_notional
                if sold <= 0:
                    continue
                ret = exit_px / pos["entry"] - 1.0
                equity += sold * ret
                equity -= sold * side_cost
                pos["notional"] = target_notional
                trades.append(
                    {
                        "symbol": sym,
                        "entry_date": str(frame.index[pos["entry_i"]].date()),
                        "exit_date": str(frame.index[i].date()),
                        "bars": int(i - pos["entry_i"]),
                        "entry": round(pos["entry"], 4),
                        "exit": round(exit_px, 4),
                        "target_pct": round(pos["target_pct"] * 100.0, 3),
                        "ret_pct": round(ret * 100.0, 3),
                        "reason": "fat_loser",
                        "touched_1r": exit_px >= pos["target"],
                    }
                )
        for sym, pos in open_pos.items():
            series = frame[sym].to_numpy(dtype=float)
            exit_i, exit_px, reason, touched = walk_path(
                series,
                pos["entry_i"],
                pos["stop"],
                pos["target"],
                max_hold,
                take_1r=take_1r,
            )
            if exit_i > i:
                continue
            ret = exit_px / pos["entry"] - 1.0
            notional = pos["notional"]
            equity += notional * ret
            equity -= notional * side_cost
            trades.append(
                {
                    "symbol": sym,
                    "entry_date": str(frame.index[pos["entry_i"]].date()),
                    "exit_date": str(frame.index[exit_i].date()),
                    "bars": int(exit_i - pos["entry_i"]),
                    "entry": round(pos["entry"], 4),
                    "exit": round(exit_px, 4),
                    "target_pct": round(pos["target_pct"] * 100.0, 3),
                    "ret_pct": round(ret * 100.0, 3),
                    "reason": reason,
                    "touched_1r": bool(touched),
                }
            )
            closed_today.append(sym)
        for sym in closed_today:
            open_pos.pop(sym, None)

        if stall_rotate and len(open_pos) >= max_active and i >= flat_n:
            row_mom_now = mom.iloc[i]
            row_above_now = above.iloc[i]
            stalled: list[tuple[float, str]] = []
            for sym, pos in open_pos.items():
                if i - int(pos["entry_i"]) < flat_n:
                    continue
                px = float(frame[sym].iloc[i])
                if not np.isfinite(px) or px <= 0:
                    continue
                if px / float(pos["entry"]) - 1.0 < float(stall_arm_pct):
                    continue
                if stall_mode == "near_peak":
                    hist = frame[sym].iloc[int(pos["entry_i"]) : i + 1].to_numpy(dtype=float)
                    window = hist[-flat_n:]
                    peak_px = float(np.nanmax(hist)) if hist.size else float("nan")
                    if (
                        window.size < flat_n
                        or not np.isfinite(window).all()
                        or not np.isfinite(peak_px)
                        or peak_px <= 0
                        or float(np.min(window)) < peak_px * (1.0 - float(stall_band))
                    ):
                        continue
                elif stall_mode == "near_avg":
                    window = frame[sym].iloc[i - flat_n + 1 : i + 1].to_numpy(dtype=float)
                    if window.size < flat_n or not np.isfinite(window).all():
                        continue
                    avg = float(np.mean(window))
                    if avg <= 0 or abs(px / avg - 1.0) > float(stall_band):
                        continue
                else:
                    prev = float(frame[sym].iloc[i - flat_n])
                    if not np.isfinite(prev) or prev <= 0 or px > prev:
                        continue
                held_m = row_mom_now.get(sym)
                if held_m is None or not np.isfinite(held_m):
                    continue
                stalled.append((float(held_m), sym))
            if stalled:
                stalled.sort()
                held_m, held_sym = stalled[0]
                best_m = None
                best_sym = None
                for sym in symbols:
                    if sym in open_pos:
                        continue
                    if not bool(row_above_now.get(sym, False)):
                        continue
                    m = row_mom_now.get(sym)
                    if m is None or not np.isfinite(m) or float(m) <= 0:
                        continue
                    if best_m is None or float(m) > best_m:
                        best_m = float(m)
                        best_sym = sym
                if (
                    best_sym is not None
                    and best_m is not None
                    and best_m > held_m + float(stall_mom_edge)
                ):
                    pos = open_pos[held_sym]
                    px = float(frame[held_sym].iloc[i])
                    ret = px / pos["entry"] - 1.0
                    equity += pos["notional"] * ret
                    equity -= pos["notional"] * side_cost
                    rotate_pairs.append(
                        {
                            "date": str(frame.index[i].date()),
                            "held": held_sym,
                            "challenger": best_sym,
                            "held_mom": round(held_m, 4),
                            "chall_mom": round(best_m, 4),
                            "held_gain": round(ret, 4),
                            "fwd5_held": _fwd(held_sym, i, 5),
                            "fwd5_chall": _fwd(best_sym, i, 5),
                            "fwd10_held": _fwd(held_sym, i, 10),
                            "fwd10_chall": _fwd(best_sym, i, 10),
                        }
                    )
                    trades.append(
                        {
                            "symbol": held_sym,
                            "entry_date": str(frame.index[pos["entry_i"]].date()),
                            "exit_date": str(frame.index[i].date()),
                            "bars": int(i - pos["entry_i"]),
                            "entry": round(pos["entry"], 4),
                            "exit": round(px, 4),
                            "target_pct": round(pos["target_pct"] * 100.0, 3),
                            "ret_pct": round(ret * 100.0, 3),
                            "reason": "stall_rotate",
                            "touched_1r": px >= pos["target"],
                        }
                    )
                    open_pos.pop(held_sym, None)

        if friday_flatten and is_friday and open_pos:
            for sym, pos in list(open_pos.items()):
                px = float(frame[sym].iloc[i])
                if not np.isfinite(px) or px <= 0:
                    continue
                ret = px / pos["entry"] - 1.0
                equity += pos["notional"] * ret
                equity -= pos["notional"] * side_cost
                trades.append(
                    {
                        "symbol": sym,
                        "entry_date": str(frame.index[pos["entry_i"]].date()),
                        "exit_date": str(frame.index[i].date()),
                        "bars": int(i - pos["entry_i"]),
                        "entry": round(pos["entry"], 4),
                        "exit": round(px, 4),
                        "target_pct": round(pos["target_pct"] * 100.0, 3),
                        "ret_pct": round(ret * 100.0, 3),
                        "reason": "friday",
                        "touched_1r": px >= pos["target"],
                    }
                )
            open_pos.clear()

        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

        if friday_flatten and int(frame.index[i].weekday()) >= 4:
            continue
        if len(open_pos) >= max_active:
            continue
        scores = []
        row_above = above.iloc[i]
        row_mom = mom.iloc[i]
        for sym in symbols:
            if sym in open_pos or sym in fade_skip:
                continue
            if not bool(row_above.get(sym, False)):
                continue
            m = row_mom.get(sym)
            if m is None or not np.isfinite(m) or m <= 0:
                continue
            scores.append((float(m), sym))
        scores.sort(reverse=True)
        slots = max_active - len(open_pos)
        clip = (equity * sleeve) / max_active if max_active else 0.0
        for _mom, sym in scores[:slots]:
            px = float(frame[sym].iloc[i])
            if not np.isfinite(px) or px <= 0:
                continue
            atr = _atr_at(frame, sym, i)
            if atr is None or atr <= 0:
                continue
            plan = plan_from_entry(px, atr, rr=1.0, stop_multiplier=stop_multiplier)
            open_pos[sym] = {
                "entry_i": i,
                "entry": px,
                "stop": plan["stop"],
                "target": plan["target"],
                "target_pct": plan["target_pct"],
                "notional": clip,
            }
            equity -= clip * side_cost

    last_i = len(frame) - 1
    for sym, pos in list(open_pos.items()):
        px = float(frame[sym].iloc[last_i])
        if not np.isfinite(px) or px <= 0:
            continue
        ret = px / pos["entry"] - 1.0
        equity += pos["notional"] * ret
        equity -= pos["notional"] * side_cost
        trades.append(
            {
                "symbol": sym,
                "entry_date": str(frame.index[pos["entry_i"]].date()),
                "exit_date": str(frame.index[last_i].date()),
                "bars": int(last_i - pos["entry_i"]),
                "entry": round(pos["entry"], 4),
                "exit": round(px, 4),
                "target_pct": round(pos["target_pct"] * 100.0, 3),
                "ret_pct": round(ret * 100.0, 3),
                "reason": "eod",
                "touched_1r": px >= pos["target"],
            }
        )

    def _pair_avg(key_h: str, key_c: str) -> tuple[float | None, float | None, float | None]:
        pairs = [
            (p[key_h], p[key_c])
            for p in rotate_pairs
            if p.get(key_h) is not None and p.get(key_c) is not None
        ]
        if not pairs:
            return None, None, None
        held_avg = float(np.mean([a for a, _b in pairs]))
        chall_avg = float(np.mean([b for _a, b in pairs]))
        beat = 100.0 * sum(1 for a, b in pairs if b > a) / len(pairs)
        return round(held_avg * 100.0, 2), round(chall_avg * 100.0, 2), round(beat, 1)

    fwd5_held, fwd5_chall, fwd5_beat = _pair_avg("fwd5_held", "fwd5_chall")
    fwd10_held, fwd10_chall, fwd10_beat = _pair_avg("fwd10_held", "fwd10_chall")

    n = len(trades)
    hits = sum(1 for t in trades if t["touched_1r"])
    rets = [t["ret_pct"] for t in trades]
    win = sum(1 for r in rets if r > 0)
    return {
        "take_1r": take_1r,
        "stop_multiplier": stop_multiplier,
        "max_hold": max_hold,
        "friday_flatten": bool(friday_flatten),
        "fat_loser": bool(fat_loser),
        "gain_exit_pct": gain_exit_pct,
        "gain_exit_mode": gain_exit_mode,
        "start": str(frame.index[start_i].date()),
        "end": str(frame.index[-1].date()),
        "bars": int(len(frame) - start_i),
        "symbols": len(symbols),
        "trades": n,
        "hit_1r": hits,
        "hit_rate": round(100.0 * hits / n, 1) if n else 0.0,
        "win_rate": round(100.0 * win / n, 1) if n else 0.0,
        "avg_ret_pct": round(float(np.mean(rets)), 3) if rets else 0.0,
        "med_ret_pct": round(float(np.median(rets)), 3) if rets else 0.0,
        "avg_target_pct": round(float(np.mean([t["target_pct"] for t in trades])), 2) if trades else 0.0,
        "equity_end": round(equity, 2),
        "book_ret_pct": round((equity / 100_000.0 - 1.0) * 100.0, 2),
        "max_dd_pct": round(max_dd * 100.0, 2),
        "exit_reasons": {
            k: sum(1 for t in trades if t["reason"] == k)
            for k in ("1r", "stop", "time", "friday", "gain_fade", "fat_loser", "stall_rotate", "eod")
        },
        "stall_rotations": len(rotate_pairs),
        "stall_fwd5_held_pct": fwd5_held,
        "stall_fwd5_chall_pct": fwd5_chall,
        "stall_fwd5_chall_beat_pct": fwd5_beat,
        "stall_fwd10_held_pct": fwd10_held,
        "stall_fwd10_chall_pct": fwd10_chall,
        "stall_fwd10_chall_beat_pct": fwd10_beat,
        "fade_by_dow": {
            d: sum(
                1
                for t in trades
                if t["reason"] == "gain_fade"
                and pd.Timestamp(t["exit_date"]).weekday() == i
            )
            for i, d in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri"))
        },
        "trades_sample": trades[:8],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=365)
    args = ap.parse_args()
    ma_win = int(getattr(config, "PAPER_NYSE_MA_WINDOW", 70))
    max_hold = int(getattr(config, "PAPER_POSITION_MAX_HOLD_BARS", 30))
    need = args.days + ma_win + 40
    print(f"Loading daily closes (~{need} rows, MA{ma_win}, hold {max_hold})...")
    data = load_close_matrix(interval="1d", days=need)
    if data is None or data.empty or len(data) < ma_win + 30:
        print(f"Not enough daily history: {0 if data is None else len(data)} bars")
        return 1
    data = data.tail(need)
    warmup = ma_win + 15
    common = dict(ma_win=ma_win, max_active=15, warmup=warmup)
    hold = run_backtest(
        data, max_hold=max_hold, take_1r=False, stop_multiplier=None, **common
    )
    take = run_backtest(
        data, max_hold=max_hold, take_1r=True, stop_multiplier=None, **common
    )
    wide = run_backtest(
        data, max_hold=max_hold, take_1r=False, stop_multiplier=3.0, **common
    )
    long_h = run_backtest(
        data, max_hold=45, take_1r=False, stop_multiplier=None, **common
    )
    payload = {
        "research_only": True,
        "hold": hold,
        "take_1r": take,
        "wide_stop_3x": wide,
        "long_hold_45": long_h,
        "note": "No .env / restart. Do not promote from this file alone.",
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines = [
        "# Exit policy A/B (NYSE MA70 daily, research only)",
        "",
        "No orders, no `.env`, no restart.",
        "",
        f"**Window:** {hold['start']} → {hold['end']} ({hold['bars']} bars, {hold['symbols']} names)",
        f"**Baseline hold:** {max_hold} bars · stop default ~2×ATR / 1% floor",
        "",
        "| policy | trades | 1R hit% | win% | avg% | book% | maxDD% | exits |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    print()
    print(f"Window: {hold['start']} -> {hold['end']}  ({hold['bars']} bars, {hold['symbols']} names)")
    print(f"{'policy':<14} {'trades':>7} {'1R hit':>8} {'win%':>7} {'avg%':>8} {'book%':>8} {'maxDD%':>8}  exits")
    for label, r in (
        ("hold", hold),
        ("take_1r", take),
        ("wide_stop_3x", wide),
        ("long_hold_45", long_h),
    ):
        er = r["exit_reasons"]
        exits = f"1r={er.get('1r', 0)} stop={er.get('stop', 0)} time={er.get('time', 0)}"
        print(
            f"{label:<14} {r['trades']:7d} {r['hit_rate']:6.1f}% {r['win_rate']:6.1f} "
            f"{r['avg_ret_pct']:7.2f} {r['book_ret_pct']:7.2f} {r['max_dd_pct']:7.2f}  {exits}"
        )
        lines.append(
            f"| {label} | {r['trades']} | {r['hit_rate']:.1f}% | {r['win_rate']:.1f} | "
            f"{r['avg_ret_pct']:.2f} | {r['book_ret_pct']:.2f} | {r['max_dd_pct']:.2f} | {exits} |"
        )
    lines.extend(
        [
            "",
            f"Median/avg 1R target (hold): {hold['avg_target_pct']:.2f}% of entry",
            "",
            "## Verdict rule (human)",
            "",
            "- Promote candidate only if book% improves vs `hold` **and** maxDD does not worsen by >2pp.",
            "- One knob at a time if wiring to paper later.",
            "",
        ]
    )
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"Median 1R target: {hold['avg_target_pct']:.2f}% of entry")
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
