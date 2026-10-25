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


def align_to_frame(aux: pd.DataFrame | None, frame: pd.DataFrame) -> pd.DataFrame | None:
    """Align an auxiliary OHLC frame to ``frame`` by date, not by row number.

    A 365d tail walk used to read ``open_gaps.iloc[i]`` against a full-history
    gap table that started years earlier, so Monday's fade used a 2022 gap.
    """
    if aux is None or aux.empty:
        return aux
    out = aux.copy()
    out.index = pd.DatetimeIndex(out.index).normalize()
    aligned = out.reindex(pd.DatetimeIndex(frame.index).normalize())
    aligned.index = frame.index
    return aligned


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


def _sharpe(equity_path: list[float]) -> float | None:
    if len(equity_path) < 5:
        return None
    curve = np.asarray(equity_path, dtype=float)
    prev = curve[:-1]
    ok = prev > 0
    rets = (curve[1:][ok] - prev[ok]) / prev[ok]
    rets = rets[np.isfinite(rets)]
    if rets.size < 5:
        return None
    std = float(rets.std(ddof=1))
    if std <= 1e-12:
        return None
    return round(float(rets.mean() / std) * np.sqrt(252.0), 2)


def _daily_return_stats(equity_path: list[float]) -> dict:
    """Per-bar Sharpe, skew and kurtosis of the marked book — inputs to the deflated Sharpe."""
    out = {"n_days": 0, "sharpe_daily": None, "daily_skew": None, "daily_kurt": None}
    if len(equity_path) < 5:
        return out
    curve = np.asarray(equity_path, dtype=float)
    prev = curve[:-1]
    ok = prev > 0
    rets = (curve[1:][ok] - prev[ok]) / prev[ok]
    rets = rets[np.isfinite(rets)]
    out["n_days"] = int(rets.size)
    if rets.size < 5:
        return out
    std = float(rets.std(ddof=1))
    if std <= 1e-12:
        return out
    z = (rets - rets.mean()) / std
    out["sharpe_daily"] = round(float(rets.mean() / std), 5)
    out["daily_skew"] = round(float(np.mean(z**3)), 4)
    out["daily_kurt"] = round(float(np.mean(z**4)), 4)  # raw (normal = 3)
    return out


def fractional_kelly_sleeve(
    returns: list[float],
    *,
    fraction: float,
    floor: float,
    cap: float,
    min_n: int,
) -> float:
    """Fraction of equity for the whole sleeve.

    Uses only closed-trade returns already in hand. Until ``min_n`` closes,
    stays at ``cap`` (the baseline sleeve). Never levers above ``cap``.
    """
    cap_f = float(cap)
    floor_f = float(floor)
    if floor_f > cap_f:
        floor_f = cap_f
    if len(returns) < int(min_n):
        return cap_f
    arr = np.asarray(returns, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size < int(min_n):
        return cap_f
    mu = float(arr.mean())
    var = float(arr.var(ddof=1))
    if not np.isfinite(mu) or not np.isfinite(var) or var <= 1e-12:
        return cap_f
    raw = float(fraction) * mu / var
    if not np.isfinite(raw):
        return cap_f
    return float(min(cap_f, max(floor_f, raw)))


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
    kelly_fraction: float | None = None,
    kelly_min_trades: int = 40,
    kelly_floor: float = 0.0,
    record_equity: bool = False,
    sleeve: float = 0.67,
    universe: list[str] | None = None,
    eligible: pd.DataFrame | None = None,
    rank_mode: str = "raw",
    rank_vol_lookback: int = 20,
    vol_target: float | None = None,
    vol_target_lookback: int = 126,
) -> dict:
    """Daily walk of the NYSE MA rank sleeve.

    ``universe`` replaces the runtime candidate list; ``eligible`` (bool frame,
    dates x symbols) gates entries per date (point-in-time membership).
    ``rank_mode="risk_adj"`` ranks by distance-to-MA over trailing daily vol.
    ``vol_target`` (annualised, e.g. 0.12) scales the sleeve by
    ``min(1, target / realised book vol over vol_target_lookback bars)``.
    Drawdown and Sharpe are marked to market every bar; booked-only drawdown
    is kept as ``max_dd_booked_pct`` for comparison with older runs.
    """
    from modules.pit_universe import session_bars

    data = session_bars(data)
    base = universe if universe is not None else _universe(data.columns)
    symbols = [c for c in dict.fromkeys(base) if c in data.columns]
    frame = data[symbols].apply(pd.to_numeric, errors="coerce")
    open_gaps = align_to_frame(open_gaps, frame)
    opens = align_to_frame(opens, frame)
    ma = frame.rolling(ma_win, min_periods=ma_win).mean()
    mom = frame / ma - 1.0
    above = frame > ma
    if rank_mode == "risk_adj":
        vol = frame.pct_change().rolling(int(rank_vol_lookback)).std()
        rank_score = mom / vol.where(vol > 1e-12)
    elif rank_mode == "raw":
        rank_score = mom
    else:
        raise ValueError(f"unknown rank_mode {rank_mode!r}")
    elig = None
    if eligible is not None:
        elig = (
            eligible.reindex(index=frame.index, columns=symbols)
            .ffill()
            .fillna(False)
            .astype(bool)
        )

    open_pos: dict[str, dict] = {}
    trades: list[dict] = []
    equity = 100_000.0
    sleeve = min(1.0, max(0.0, float(sleeve)))
    peak = equity
    max_dd = 0.0
    peak_booked = equity
    max_dd_booked = 0.0
    start_i = max(warmup, ma_win + 15)
    side_cost = max(0.0, float(cost_rt_bps)) / 2.0 / 10_000.0
    rotate_pairs: list[dict] = []
    flat_n = max(1, int(stall_flat_bars))
    kelly_sleeves: list[float] = []
    vol_scales: list[float] = []
    equity_path: list[float] = []
    gap_skips = 0

    def _mark_to_market(i: int) -> float:
        mtm = equity
        for sym, pos in open_pos.items():
            px = float(frame[sym].iloc[i])
            if np.isfinite(px) and px > 0:
                mtm += pos["notional"] * (px / pos["entry"] - 1.0)
        return mtm

    def _vol_scale() -> float:
        if vol_target is None:
            return 1.0
        n = int(vol_target_lookback)
        if len(equity_path) < n + 1:
            return 1.0
        curve = np.asarray(equity_path[-(n + 1) :], dtype=float)
        rets = curve[1:] / curve[:-1] - 1.0
        rets = rets[np.isfinite(rets)]
        if rets.size < n:
            return 1.0
        realised = float(rets.std(ddof=1)) * np.sqrt(252.0)
        if realised <= 1e-12:
            return 1.0
        return float(min(1.0, float(vol_target) / realised))

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
                        gap_skips += 1
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
            if not np.isfinite(exit_px) or exit_px <= 0 or not np.isfinite(pos["entry"]) or pos["entry"] <= 0:
                closed_today.append(sym)
                continue
            ret = exit_px / pos["entry"] - 1.0
            if not np.isfinite(ret):
                closed_today.append(sym)
                continue
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

        peak_booked = max(peak_booked, equity)
        max_dd_booked = max(
            max_dd_booked, (peak_booked - equity) / peak_booked if peak_booked > 0 else 0.0
        )
        mtm = _mark_to_market(i)
        peak = max(peak, mtm)
        dd = (peak - mtm) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        equity_path.append(mtm)

        if friday_flatten and int(frame.index[i].weekday()) >= 4:
            continue
        if len(open_pos) >= max_active:
            continue
        scores = []
        row_above = above.iloc[i]
        row_mom = mom.iloc[i]
        row_score = rank_score.iloc[i]
        row_elig = elig.iloc[i] if elig is not None else None
        for sym in symbols:
            if sym in open_pos or sym in fade_skip:
                continue
            if row_elig is not None and not bool(row_elig.get(sym, False)):
                continue
            if not bool(row_above.get(sym, False)):
                continue
            m = row_mom.get(sym)
            if m is None or not np.isfinite(m) or m <= 0:
                continue
            s = row_score.get(sym)
            if s is None or not np.isfinite(s):
                continue
            scores.append((float(s), sym))
        scores.sort(reverse=True)
        slots = max_active - len(open_pos)
        sleeve_now = sleeve
        if vol_target is not None:
            scale = _vol_scale()
            vol_scales.append(scale)
            sleeve_now = sleeve * scale
        if kelly_fraction is not None:
            sleeve_now = fractional_kelly_sleeve(
                [t["ret_pct"] / 100.0 for t in trades],
                fraction=float(kelly_fraction),
                floor=float(kelly_floor),
                cap=sleeve_now,
                min_n=int(kelly_min_trades),
            )
            kelly_sleeves.append(sleeve_now)
        clip = (equity * sleeve_now) / max_active if max_active else 0.0
        if clip <= 0:
            continue
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
    out = {
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
        "max_dd_booked_pct": round(max_dd_booked * 100.0, 2),
        "rank_mode": rank_mode,
        "vol_target": vol_target,
        "universe_mode": "custom" if universe is not None else "runtime",
        "pit_gated": eligible is not None,
        "cost_rt_bps": float(cost_rt_bps),
        "gap_skips": gap_skips,
        "weekend_fades": sum(
            1
            for t in trades
            if t["reason"] == "gain_fade" and pd.Timestamp(t["exit_date"]).weekday() >= 5
        ),
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
    out["sharpe"] = _sharpe(equity_path)
    out.update(_daily_return_stats(equity_path))
    if record_equity:
        out["equity_path"] = [round(v, 2) for v in equity_path]
    if vol_target is not None:
        out["vol_scale_avg"] = round(float(np.mean(vol_scales)), 4) if vol_scales else None
        out["vol_days_scaled_pct"] = (
            round(100.0 * sum(1 for s in vol_scales if s < 1.0 - 1e-9) / len(vol_scales), 1)
            if vol_scales
            else None
        )
    if kelly_fraction is not None:
        out["kelly_fraction"] = float(kelly_fraction)
        out["kelly_sleeve_avg"] = (
            round(float(np.mean(kelly_sleeves)), 4) if kelly_sleeves else None
        )
        out["kelly_days_at_cap_pct"] = (
            round(
                100.0
                * sum(1 for s in kelly_sleeves if s >= sleeve - 1e-9)
                / len(kelly_sleeves),
                1,
            )
            if kelly_sleeves
            else None
        )
        out["kelly_days_at_floor_pct"] = (
            round(
                100.0
                * sum(1 for s in kelly_sleeves if s <= float(kelly_floor) + 1e-9)
                / len(kelly_sleeves),
                1,
            )
            if kelly_sleeves
            else None
        )
    return out


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
