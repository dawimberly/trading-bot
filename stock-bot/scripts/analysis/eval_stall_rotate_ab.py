"""A/B: hold a +5% name that has stalled vs rotate into a stronger MA signal.

Same ruler for both sides: distance above the NYSE moving average
(close / MA - 1), which is how a new buy is ranked.

Stall: up at least 5% from entry, and the close is not higher than it was
3 sessions ago. Rotate only when the book is full and the best free name
sits further above its MA than that stalled name.

Research walk only (NYSE hold, 15 names, 2x ATR, 30-bar max hold, 10bps
round trip). Not backtester.py. Does not write .env.

Usage (from stock-bot/):
  python scripts/analysis/eval_stall_rotate_ab.py
  python scripts/analysis/eval_stall_rotate_ab.py --days 365
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "analysis"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

import config  # noqa: E402
from modules.data_loader import load_close_matrix  # noqa: E402
from one_r_hit_backtest import run_backtest  # noqa: E402

OUT_JSON = Path(__file__).with_name("eval_stall_rotate_ab_last.json")
OUT_MD = Path(__file__).with_name("eval_stall_rotate_ab_last.md")
COST_RT_BPS = 10.0
MAX_DD_WORSE_PP = 2.0
MAX_ACTIVE = 15
ARM_PCT = 0.05
FLAT_BARS = 3


def _fmt(val, digits: int = 2) -> str:
    if val is None:
        return "n/a"
    return f"{val:.{digits}f}"


def _verdict(base: dict, treat: dict) -> str:
    d_ret = treat["book_ret_pct"] - base["book_ret_pct"]
    d_dd = treat["max_dd_pct"] - base["max_dd_pct"]
    beat = treat.get("stall_fwd10_chall_beat_pct")
    held = treat.get("stall_fwd10_held_pct")
    chall = treat.get("stall_fwd10_chall_pct")
    book_ok = d_ret > 0 and d_dd <= MAX_DD_WORSE_PP
    pair_count_ok = beat is not None and beat > 55.0
    pair_avg_ok = (
        held is not None and chall is not None and float(chall) > float(held)
    )
    if book_ok and pair_count_ok and pair_avg_ok:
        label = "PASS"
    elif book_ok and pair_avg_ok:
        label = "MIXED"
    else:
        label = "FAIL"
    return (
        f"{label} — book {d_ret:+.2f}pp, max DD {d_dd:+.2f}pp. "
        f"Next 10 sessions the challenger averaged {_fmt(chall)}% vs "
        f"{_fmt(held)}% for the stalled name, and won the matchup "
        f"{_fmt(beat, 1)}% of the time."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=365)
    args = ap.parse_args()
    days = max(60, int(args.days))
    ma_win = int(getattr(config, "PAPER_NYSE_MA_WINDOW", 70))
    max_hold = int(getattr(config, "PAPER_POSITION_MAX_HOLD_BARS", 30))
    need = days + ma_win + 40
    print(f"Loading daily closes (~{need} rows, MA{ma_win})...", flush=True)
    data = load_close_matrix(interval="1d", days=need)
    if data is None or data.empty or len(data) < ma_win + 30:
        print(f"Not enough daily history: {0 if data is None else len(data)}")
        return 1
    data = data.tail(need)
    common = dict(
        ma_win=ma_win,
        max_hold=max_hold,
        max_active=MAX_ACTIVE,
        warmup=ma_win + 15,
        take_1r=False,
        stop_multiplier=None,
        cost_rt_bps=COST_RT_BPS,
    )
    arms = [i / 100.0 for i in range(1, 11)]
    print("Baseline: hold...", flush=True)
    base = run_backtest(data, stall_rotate=False, **common)
    grid = []
    for arm in arms:
        print(f"Treatment: stall arm {arm:.0%}...", flush=True)
        treat_arm = run_backtest(
            data,
            stall_rotate=True,
            stall_arm_pct=arm,
            stall_flat_bars=FLAT_BARS,
            stall_mom_edge=0.0,
            **common,
        )
        grid.append(treat_arm)
    treat = next(row for arm, row in zip(arms, grid) if abs(arm - ARM_PCT) < 1e-9)
    verdict = _verdict(base, treat)
    payload = {
        "research_only": True,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "days": days,
        "ma_win": ma_win,
        "max_hold": max_hold,
        "max_active": MAX_ACTIVE,
        "cost_rt_bps": COST_RT_BPS,
        "rule": (
            f"Full book only. Sell the weakest name that is up >={ARM_PCT:.0%} "
            f"and flat/down over {FLAT_BARS} sessions when a free name is further above its MA."
        ),
        "verdict": verdict,
        "arm_grid": [
            {
                "arm_pct": round(arm * 100.0, 1),
                "rotations": row["stall_rotations"],
                "book_ret_pct": row["book_ret_pct"],
                "d_book_pp": round(row["book_ret_pct"] - base["book_ret_pct"], 2),
                "max_dd_pct": row["max_dd_pct"],
                "d_dd_pp": round(row["max_dd_pct"] - base["max_dd_pct"], 2),
                "fwd10_held_pct": row["stall_fwd10_held_pct"],
                "fwd10_chall_pct": row["stall_fwd10_chall_pct"],
                "fwd10_beat_pct": row["stall_fwd10_chall_beat_pct"],
            }
            for arm, row in zip(arms, grid)
        ],
        "baseline": {k: base[k] for k in (
            "start", "end", "trades", "win_rate", "book_ret_pct", "max_dd_pct", "exit_reasons"
        )},
        "treatment": {k: treat[k] for k in (
            "start", "end", "trades", "win_rate", "book_ret_pct", "max_dd_pct",
            "exit_reasons", "stall_rotations",
            "stall_fwd5_held_pct", "stall_fwd5_chall_pct", "stall_fwd5_chall_beat_pct",
            "stall_fwd10_held_pct", "stall_fwd10_chall_pct", "stall_fwd10_chall_beat_pct",
        )},
    }
    d_ret = treat["book_ret_pct"] - base["book_ret_pct"]
    d_dd = treat["max_dd_pct"] - base["max_dd_pct"]
    lines = [
        "# Stall arm cutoff vs stronger MA signal",
        "",
        "_NYSE hold walk, 15 names, 10bps round trip. Research only. Does not write `.env`._",
        "",
        f"**{verdict}**",
        "",
        "A name is stalled when it is up at least the arm percent from entry and its close is not higher than 3 sessions ago. A new name is more promising when it sits further above its moving average than that stalled name. The swap happens only when all 15 slots are full.",
        "",
        f"Hold book return {base['book_ret_pct']:.2f}%, max DD {base['max_dd_pct']:.2f}%.",
        "",
        "| Arm | Rotations | Book Δ | DD Δ | Kept 10d | New 10d | New won |",
        "|----:|----------:|-------:|-----:|---------:|--------:|--------:|",
    ]
    for arm, row in zip(arms, grid):
        lines.append(
            f"| {arm * 100:.0f}% | {row['stall_rotations']} | "
            f"{row['book_ret_pct'] - base['book_ret_pct']:+.2f} pp | "
            f"{row['max_dd_pct'] - base['max_dd_pct']:+.2f} pp | "
            f"{_fmt(row['stall_fwd10_held_pct'])}% | "
            f"{_fmt(row['stall_fwd10_chall_pct'])}% | "
            f"{_fmt(row['stall_fwd10_chall_beat_pct'], 1)}% |"
        )
    lines.extend(
        [
            "",
            f"Window {base['start']} → {base['end']}. MA{ma_win}, max hold {max_hold} bars. "
            f"5% arm book {d_ret:+.2f} pp, max DD {d_dd:+.2f} pp.",
            "",
        ]
    )
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(verdict, flush=True)
    print(f"Wrote {OUT_MD.name}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
