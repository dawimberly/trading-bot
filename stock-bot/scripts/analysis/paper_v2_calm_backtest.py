"""A/B: paper v2 calm (45d hold, no micro-trims) vs old 30d hold.

Same NYSE MA70 + 2xATR stop walk as one_r_hit_backtest.py (no fat-loser /
concentration drip in this engine). 33/67 overlay: replace idle ~33% cash
with VTI buy-hold.

Usage (from stock-bot/):
  python scripts/analysis/paper_v2_calm_backtest.py --days 365
  python scripts/analysis/paper_v2_calm_backtest.py --days 1000
"""

from __future__ import annotations

import argparse
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

OUT_JSON = Path(__file__).with_name("paper_v2_calm_backtest_last.json")
VTI_W = 0.33


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
    # one_r leaves ~33% cash; swap that sleeve for VTI.
    return round(nyse_book_pct + VTI_W * vti_pct, 2)


def _run_window(data: pd.DataFrame, *, days: int, ma_win: int) -> dict:
    warmup = ma_win + 15
    common = dict(ma_win=ma_win, max_active=15, warmup=warmup, take_1r=False)
    old = run_backtest(data, max_hold=30, stop_multiplier=None, **common)
    calm = run_backtest(data, max_hold=45, stop_multiplier=None, **common)
    vti = _bh_pct(data, "VTI", old["start"], old["end"])
    spy = _bh_pct(data, "SPY", old["start"], old["end"])
    return {
        "days": days,
        "start": old["start"],
        "end": old["end"],
        "bars": old["bars"],
        "symbols": old["symbols"],
        "hold_30": old,
        "hold_45": calm,
        "vti_bh_pct": vti,
        "spy_bh_pct": spy,
        "book_33_67_hold_30_pct": _blend(old["book_ret_pct"], vti),
        "book_33_67_hold_45_pct": _blend(calm["book_ret_pct"], vti),
    }


def _print_block(label: str, block: dict) -> None:
    print()
    print(
        f"{label}: {block['start']} -> {block['end']}  "
        f"({block['bars']} bars, {block['symbols']} names)"
    )
    print(
        f"{'policy':<16} {'trades':>7} {'win%':>7} {'avg%':>8} "
        f"{'NYSE%':>8} {'maxDD%':>8}  33/67%"
    )
    for name, key in (("hold_30 old", "hold_30"), ("hold_45 calm", "hold_45")):
        r = block[key]
        er = r["exit_reasons"]
        exits = f"stop={er.get('stop', 0)} time={er.get('time', 0)}"
        blend = block[f"book_33_67_{key}_pct"]
        blend_s = f"{blend:7.2f}" if blend is not None else "    n/a"
        print(
            f"{name:<16} {r['trades']:7d} {r['win_rate']:6.1f} "
            f"{r['avg_ret_pct']:7.2f} {r['book_ret_pct']:7.2f} "
            f"{r['max_dd_pct']:7.2f}  {blend_s}  {exits}"
        )
    if block["vti_bh_pct"] is not None:
        print(f"VTI buy-hold: {block['vti_bh_pct']:.2f}%   SPY: {block['spy_bh_pct']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=0, help="0 = run 365 and 1000")
    args = ap.parse_args()
    ma_win = int(getattr(config, "PAPER_NYSE_MA_WINDOW", 70))
    windows = [args.days] if args.days > 0 else [365, 1000]
    need = max(windows) + ma_win + 40
    print(f"Loading daily closes (~{need} rows, MA{ma_win})...")
    data = load_close_matrix(interval="1d", days=need)
    if data is None or data.empty or len(data) < ma_win + 30:
        print(f"Not enough daily history: {0 if data is None else len(data)} bars")
        return 1
    full = data.tail(need)
    out = {
        "research_only": True,
        "note": (
            "alpaca_paper_v2 calm vs old hold. ATR stop kept. "
            "No fat-loser/concentration drip in this walk. Not lab 4x25."
        ),
        "windows": {},
    }
    for d in windows:
        slice_n = d + ma_win + 40
        block = _run_window(full.tail(slice_n), days=d, ma_win=ma_win)
        out["windows"][str(d)] = block
        _print_block(f"{d}d", block)
    OUT_JSON.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"Wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
