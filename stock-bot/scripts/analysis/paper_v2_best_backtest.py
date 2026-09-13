"""Backtest the v2 book I would actually run.

Pick: 15 NYSE names, MA70, 2x ATR stop, 30-bar hold, no 1R, no micro-trims.
33/67 = NYSE walk (67% sleeve + idle cash) with idle 33% swapped to VTI.

Comparators: 8 names, 25 names, hold 35, hold 45, 3x ATR, take-1R.

Usage (from stock-bot/):
  python scripts/analysis/paper_v2_best_backtest.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "analysis"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

import config  # noqa: E402
from modules.data_loader import load_close_matrix  # noqa: E402
from one_r_hit_backtest import run_backtest  # noqa: E402

OUT_JSON = Path(__file__).with_name("paper_v2_best_backtest_last.json")
VTI_W = 0.33

POLICIES = [
    ("pick_15_30_2x", dict(max_active=15, max_hold=30, take_1r=False, stop_multiplier=None)),
    ("names_8_30", dict(max_active=8, max_hold=30, take_1r=False, stop_multiplier=None)),
    ("names_25_30", dict(max_active=25, max_hold=30, take_1r=False, stop_multiplier=None)),
    ("hold_35", dict(max_active=15, max_hold=35, take_1r=False, stop_multiplier=None)),
    ("hold_45", dict(max_active=15, max_hold=45, take_1r=False, stop_multiplier=None)),
    ("atr_3x", dict(max_active=15, max_hold=30, take_1r=False, stop_multiplier=3.0)),
    ("take_1r", dict(max_active=15, max_hold=30, take_1r=True, stop_multiplier=None)),
]


def _bh_pct(data: pd.DataFrame, col: str, start: str, end: str) -> float | None:
    if col not in data.columns:
        return None
    s = pd.to_numeric(data[col], errors="coerce")
    idx = data.index
    mask = (idx.date >= pd.to_datetime(start).date()) & (
        idx.date <= pd.to_datetime(end).date()
    )
    ser = s.loc[mask].dropna()
    if len(ser) < 2:
        return None
    return round(float(ser.iloc[-1] / ser.iloc[0] - 1.0) * 100.0, 2)


def _blend(nyse_book_pct: float, vti_pct: float | None) -> float | None:
    if vti_pct is None:
        return None
    return round(nyse_book_pct + VTI_W * vti_pct, 2)


def main() -> int:
    ma_win = int(getattr(config, "PAPER_NYSE_MA_WINDOW", 70))
    warmup = ma_win + 15
    need = 1000 + ma_win + 40
    print(f"Loading daily closes (~{need} rows, MA{ma_win})...")
    data = load_close_matrix(interval="1d", days=need)
    if data is None or data.empty or len(data) < ma_win + 30:
        print(f"Not enough daily history: {0 if data is None else len(data)} bars")
        return 1
    full = data.tail(need)
    payload = {
        "research_only": True,
        "pick": "15 names, 30d hold, 2x ATR, no 1R, no micro-trims, 33/67 VTI overlay",
        "windows": {},
    }
    for days in (365, 1000):
        frame = full.tail(days + ma_win + 40)
        rows = {}
        first = None
        for name, kw in POLICIES:
            r = run_backtest(frame, ma_win=ma_win, warmup=warmup, **kw)
            if first is None:
                first = r
            rows[name] = r
        vti = _bh_pct(frame, "VTI", first["start"], first["end"])
        spy = _bh_pct(frame, "SPY", first["start"], first["end"])
        block = {
            "start": first["start"],
            "end": first["end"],
            "bars": first["bars"],
            "symbols": first["symbols"],
            "vti_bh_pct": vti,
            "spy_bh_pct": spy,
            "policies": {},
        }
        print()
        print(f"{days}d: {first['start']} -> {first['end']}  ({first['bars']} bars)")
        print(
            f"{'policy':<16} {'trades':>7} {'win%':>7} {'NYSE%':>8} "
            f"{'maxDD%':>8} {'33/67%':>8}"
        )
        for name, _kw in POLICIES:
            r = rows[name]
            blend = _blend(r["book_ret_pct"], vti)
            er = r["exit_reasons"]
            block["policies"][name] = {
                "trades": r["trades"],
                "win_rate": r["win_rate"],
                "book_ret_pct": r["book_ret_pct"],
                "max_dd_pct": r["max_dd_pct"],
                "avg_ret_pct": r["avg_ret_pct"],
                "exit_reasons": er,
                "book_33_67_pct": blend,
            }
            blend_s = f"{blend:7.2f}" if blend is not None else "    n/a"
            print(
                f"{name:<16} {r['trades']:7d} {r['win_rate']:6.1f} "
                f"{r['book_ret_pct']:7.2f} {r['max_dd_pct']:7.2f} {blend_s}"
            )
        print(f"VTI {vti}  SPY {spy}")
        payload["windows"][str(days)] = block
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
