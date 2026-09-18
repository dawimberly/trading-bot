"""A/B: Medium SoT hold-through-weekend vs Friday flatten (research only).

Same NYSE MA70 + 15 names + 30-bar + 2x ATR walk as `one_r_hit_backtest.py`.
Friday flatten sells leftovers at Friday close and skips new Friday entries.
Monday can re-enter (hold clock resets). No costs in the walk; a 10 bps
round-trip haircut is printed as a sensitivity.

Does not change Medium SoT or Lab. No `.env`.

Usage (from stock-bot/):
  python scripts/analysis/friday_flatten_backtest.py --days 365
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
sys.path.insert(0, str(ROOT / "scripts" / "analysis"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

import config  # noqa: E402
from modules.data_loader import load_close_matrix  # noqa: E402
from one_r_hit_backtest import _universe, run_backtest  # noqa: E402

OUT_JSON = Path(__file__).with_name("friday_flatten_backtest_last.json")
SLEEVE = 0.67
MAX_ACTIVE = 15
RT_COST_BPS = 10.0


def _weekdays(data: pd.DataFrame) -> pd.DataFrame:
    idx = pd.DatetimeIndex(data.index)
    return data.loc[idx.weekday < 5].copy()


def _gap_study(data: pd.DataFrame) -> dict:
    """Equal-weight Fri close → next session close (usually Monday)."""
    symbols = [c for c in _universe(data.columns) if c in data.columns]
    frame = data[symbols].apply(pd.to_numeric, errors="coerce")
    idx = pd.DatetimeIndex(frame.index)
    rets: list[float] = []
    spy_rets: list[float] = []
    spy = None
    if "SPY" in data.columns:
        spy = pd.to_numeric(data["SPY"], errors="coerce")
    for i in range(len(idx) - 1):
        if int(idx[i].weekday()) != 4:
            continue
        j = i + 1
        while j < len(idx) and int(idx[j].weekday()) >= 5:
            j += 1
        if j >= len(idx):
            continue
        row_f = frame.iloc[i]
        row_n = frame.iloc[j]
        pair = []
        for sym in symbols:
            a = float(row_f.get(sym, np.nan))
            b = float(row_n.get(sym, np.nan))
            if np.isfinite(a) and np.isfinite(b) and a > 0:
                pair.append(b / a - 1.0)
        if not pair:
            continue
        rets.append(float(np.mean(pair)))
        if spy is not None:
            a = float(spy.iloc[i])
            b = float(spy.iloc[j])
            if np.isfinite(a) and np.isfinite(b) and a > 0:
                spy_rets.append(b / a - 1.0)
    arr = np.array(rets, dtype=float) if rets else np.array([])
    spy_arr = np.array(spy_rets, dtype=float) if spy_rets else np.array([])
    out = {
        "weekends": int(len(arr)),
        "ew_mean_pct": round(float(arr.mean()) * 100.0, 3) if len(arr) else None,
        "ew_median_pct": round(float(np.median(arr)) * 100.0, 3) if len(arr) else None,
        "ew_neg_share": round(float((arr < 0).mean()) * 100.0, 1) if len(arr) else None,
        "ew_worst_pct": round(float(arr.min()) * 100.0, 3) if len(arr) else None,
        "ew_best_pct": round(float(arr.max()) * 100.0, 3) if len(arr) else None,
        "spy_mean_pct": round(float(spy_arr.mean()) * 100.0, 3) if len(spy_arr) else None,
        "spy_neg_share": round(float((spy_arr < 0).mean()) * 100.0, 1)
        if len(spy_arr)
        else None,
    }
    return out


def _costed_book_pct(row: dict) -> float:
    """10 bps round-trip on each trade's ~4.5% sleeve clip."""
    n = int(row.get("trades") or 0)
    clip = SLEEVE / MAX_ACTIVE
    haircut = n * (RT_COST_BPS / 10_000.0) * clip * 100.0
    return round(float(row["book_ret_pct"]) - haircut, 2)


def _run_window(data: pd.DataFrame, *, days: int, ma_win: int) -> dict:
    warmup = ma_win + 15
    common = dict(
        ma_win=ma_win,
        max_active=MAX_ACTIVE,
        warmup=warmup,
        take_1r=False,
        stop_multiplier=None,
        max_hold=30,
    )
    sessions = _weekdays(data)
    hold = run_backtest(sessions, friday_flatten=False, **common)
    flat = run_backtest(sessions, friday_flatten=True, **common)
    return {
        "days": days,
        "start": hold["start"],
        "end": hold["end"],
        "bars": hold["bars"],
        "symbols": hold["symbols"],
        "weekend_gaps": _gap_study(sessions),
        "hold": {k: hold[k] for k in hold if k != "trades_sample"},
        "friday_flatten": {k: flat[k] for k in flat if k != "trades_sample"},
        "hold_costed_10bps_pct": _costed_book_pct(hold),
        "friday_costed_10bps_pct": _costed_book_pct(flat),
        "delta_book_pct": round(flat["book_ret_pct"] - hold["book_ret_pct"], 2),
        "delta_dd_pct": round(flat["max_dd_pct"] - hold["max_dd_pct"], 2),
    }


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
    windows = {}
    day_list = (365, 1000) if args.days == 365 else (args.days,)
    for days in day_list:
        frame = data
        if days != args.days:
            extra = load_close_matrix(interval="1d", days=days + ma_win + 40)
            if extra is None or extra.empty:
                continue
            frame = extra.tail(days + ma_win + 40)
        block = _run_window(frame, days=days, ma_win=ma_win)
        windows[str(days)] = block
        g = block["weekend_gaps"]
        print()
        print(f"{days}d: {block['start']} -> {block['end']}  ({block['bars']} bars)")
        print(
            f"  Fri-next EW mean {g['ew_mean_pct']}%  "
            f"neg {g['ew_neg_share']}%  SPY {g['spy_mean_pct']}%  "
            f"n={g['weekends']}"
        )
        print(f"{'policy':<16} {'trades':>7} {'win%':>7} {'book%':>8} {'maxDD%':>8} {'10bps%':>8}  exits")
        for name, key, costed in (
            ("hold", "hold", block["hold_costed_10bps_pct"]),
            ("friday_flatten", "friday_flatten", block["friday_costed_10bps_pct"]),
        ):
            r = block[key]
            er = r["exit_reasons"]
            exits = (
                f"stop={er.get('stop', 0)} time={er.get('time', 0)} "
                f"friday={er.get('friday', 0)}"
            )
            print(
                f"{name:<16} {r['trades']:7d} {r['win_rate']:6.1f} "
                f"{r['book_ret_pct']:7.2f} {r['max_dd_pct']:7.2f} "
                f"{costed:7.2f}  {exits}"
            )
        print(
            f"  delta flatten-hold book {block['delta_book_pct']:+.2f}pp  "
            f"DD {block['delta_dd_pct']:+.2f}pp"
        )
    payload = {
        "research_only": True,
        "pick": "15 names, 30d, 2x ATR, no 1R. A/B: hold weekend vs Friday flatten.",
        "note": "No .env / restart. Do not promote from this file alone.",
        "windows": windows,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
