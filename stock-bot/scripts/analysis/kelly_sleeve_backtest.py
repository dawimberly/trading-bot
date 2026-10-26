"""Single-factor test: half-Kelly sleeve on the NYSE MA70 book.

Same entries as pick_15 / 30-bar / 2x ATR. Sleeve cap is the employed
NYSE cap, 95% (VTI off). After 40 closed trades the sleeve scales to
fraction * mean(trade return) / variance(trade return), capped at 95% and
floored at a quarter of that so a cold start cannot shut entries off.
Half-Kelly (0.5) is the treatment.
Quarter-Kelly is a robustness check on the same factor, not a second change.

No costs, matching the other NYSE walks. Does not write .env.

Usage (from stock-bot/):
  python scripts/analysis/kelly_sleeve_backtest.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "analysis"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

import config  # noqa: E402
from modules.data_loader import load_close_matrix  # noqa: E402
from one_r_hit_backtest import run_backtest  # noqa: E402

OUT_JSON = Path(__file__).with_name("kelly_sleeve_backtest_last.json")
OUT_MD = Path(__file__).with_name("kelly_sleeve_backtest_last.md")
DD_GATE_PP = 2.0
SLEEVE = 0.95
KELLY_FLOOR = SLEEVE * 0.25

POLICIES = [
    ("equal_95", None),
    ("half_kelly", 0.5),
    ("quarter_kelly", 0.25),
]


def _verdict(base: dict, treat: dict) -> str:
    d_ret = treat["book_ret_pct"] - base["book_ret_pct"]
    d_dd = treat["max_dd_pct"] - base["max_dd_pct"]
    at_cap = treat.get("kelly_days_at_cap_pct")
    if at_cap is not None and at_cap >= 90.0 and abs(d_ret) < 0.5:
        return "INERT"
    if d_dd > DD_GATE_PP:
        return "HOLD"
    if d_ret > 0 and d_dd <= DD_GATE_PP:
        return "CANDIDATE"
    return "HOLD"


def _line(name: str, row: dict) -> str:
    sharpe = row.get("sharpe")
    sharpe_s = f"{sharpe:.2f}" if isinstance(sharpe, (int, float)) else "n/a"
    cap = row.get("kelly_days_at_cap_pct")
    cap_s = f"{cap:.0f}%" if isinstance(cap, (int, float)) else "n/a"
    return (
        f"| {name} | {row['trades']} | {row['win_rate']:.1f}% | "
        f"{row['book_ret_pct']:+.2f}% | {row['max_dd_pct']:.2f}% | {sharpe_s} | {cap_s} |"
    )


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
    windows: dict[str, dict] = {}
    lines = [
        "# Half-Kelly sleeve on NYSE MA70",
        "",
        "Research only. Same entries as 15 names / 30-bar / 2× ATR. "
        f"Employed NYSE cap {SLEEVE:.0%}, VTI off. "
        f"Sleeve = clip(fraction × mean trade return / variance, {KELLY_FLOOR:.2%}, {SLEEVE:.0%}) "
        "after 40 closes. Does not write `.env`.",
        "",
        "Half-Kelly is the treatment. Quarter-Kelly checks that the fraction is not a one-point fluke. "
        f"A candidate needs a higher book return and drawdown no worse than {DD_GATE_PP:.0f} pp.",
        "",
    ]
    for days in (365, 1000):
        frame = full.tail(days + ma_win + 40)
        rows = {}
        for name, frac in POLICIES:
            rows[name] = run_backtest(
                frame,
                ma_win=ma_win,
                warmup=warmup,
                max_active=15,
                max_hold=30,
                take_1r=False,
                stop_multiplier=None,
                kelly_fraction=frac,
                kelly_floor=KELLY_FLOOR,
                record_equity=True,
                sleeve=SLEEVE,
            )
        base = rows["equal_95"]
        half = rows["half_kelly"]
        verdict = _verdict(base, half)
        block = {
            "start": base["start"],
            "end": base["end"],
            "bars": base["bars"],
            "symbols": base["symbols"],
            "verdict_half_vs_equal": verdict,
            "policies": {
                name: {k: v for k, v in row.items() if k != "trades_sample"}
                for name, row in rows.items()
            },
        }
        windows[str(days)] = block
        lines.extend(
            [
                f"## {days}d  {base['start']} → {base['end']}",
                "",
                f"| Book | Trades | Win | Return | Max DD | Sharpe | Days at {SLEEVE:.0%} cap |",
                "|------|-------:|----:|-------:|-------:|-------:|----------------:|",
            ]
        )
        for name, _frac in POLICIES:
            lines.append(_line(name, rows[name]))
        d_ret = half["book_ret_pct"] - base["book_ret_pct"]
        d_dd = half["max_dd_pct"] - base["max_dd_pct"]
        lines.extend(
            [
                "",
                f"Half-Kelly vs equal: return {d_ret:+.2f} pp, max DD {d_dd:+.2f} pp. "
                f"**{verdict}**.",
                "",
            ]
        )
        print(
            f"{days}d {verdict}: equal {base['book_ret_pct']:+.2f}% DD {base['max_dd_pct']:.2f} | "
            f"half {half['book_ret_pct']:+.2f}% DD {half['max_dd_pct']:.2f} "
            f"cap {half.get('kelly_days_at_cap_pct')}%"
        )
    half_verdicts = [windows[k]["verdict_half_vs_equal"] for k in ("365", "1000")]
    if half_verdicts == ["CANDIDATE", "CANDIDATE"]:
        overall = "CANDIDATE on both windows. Still paper-only; do not write .env."
    elif "CANDIDATE" in half_verdicts:
        overall = "HOLD. Only one window is a candidate."
    else:
        overall = "HOLD. Half-Kelly does not clear both windows."
    lines.extend(["## Read", "", overall, ""])
    payload = {
        "research_only": True,
        "factor": "half-Kelly sleeve cap 0.95, floor 0.2375, min 40 closes",
        "overall": overall,
        "windows": windows,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(overall)
    print(f"Wrote {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
