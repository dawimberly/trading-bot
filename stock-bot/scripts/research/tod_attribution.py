"""Time-of-day attribution helpers for research backtests.

Reusable across strategies. Research-only; not wired into paper/live.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")

# Equity RTH segments (ET)
EQUITY_SEGMENTS = (
    ("open_30m", time(9, 30), time(10, 0)),
    ("mid_am", time(10, 0), time(11, 30)),
    ("lunch", time(11, 30), time(13, 30)),
    ("mid_pm", time(13, 30), time(15, 0)),
    ("close_60m", time(15, 0), time(16, 0)),
)

# Crypto / 24h regions in ET
CRYPTO_SEGMENTS = (
    ("asia", time(18, 0), time(2, 0)),  # wraps midnight
    ("europe", time(2, 0), time(8, 0)),
    ("us_pre", time(8, 0), time(9, 30)),
    ("us_rth", time(9, 30), time(16, 0)),
    ("us_after", time(16, 0), time(18, 0)),
)


@dataclass
class TodBucket:
    label: str
    trades: int = 0
    wins: int = 0
    total_pnl: float = 0.0
    sum_r: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def avg_r(self) -> float:
        return self.sum_r / self.trades if self.trades else 0.0


def parse_entry_ts(entry_time: str | datetime | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(entry_time)
    if ts.tzinfo is None:
        ts = ts.tz_localize(ET)
    else:
        ts = ts.tz_convert(ET)
    return ts


def entry_hour_et(entry_time: str | datetime | pd.Timestamp) -> int:
    return int(parse_entry_ts(entry_time).hour)


def _in_window(t: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def equity_segment(entry_time: str | datetime | pd.Timestamp) -> str:
    t = parse_entry_ts(entry_time).timetz().replace(tzinfo=None)
    for name, start, end in EQUITY_SEGMENTS:
        if start <= t < end:
            return name
    return "off_rth"


def crypto_segment(entry_time: str | datetime | pd.Timestamp) -> str:
    t = parse_entry_ts(entry_time).timetz().replace(tzinfo=None)
    for name, start, end in CRYPTO_SEGMENTS:
        if _in_window(t, start, end):
            return name
    return "other"


def aggregate_tod(
    trades: Iterable[dict],
    *,
    mode: str = "equity",
) -> tuple[dict[str, TodBucket], dict[str, TodBucket]]:
    """Return (by_hour, by_segment). Each trade dict needs entry_time, pnl, r_multiple."""
    by_hour: dict[str, TodBucket] = {}
    by_seg: dict[str, TodBucket] = {}

    for tr in trades:
        ts = tr.get("entry_time")
        if ts is None:
            continue
        hour = entry_hour_et(ts)
        h_key = f"{hour:02d}:00"
        seg = equity_segment(ts) if mode == "equity" else crypto_segment(ts)
        pnl = float(tr.get("pnl") or 0.0)
        r = float(tr.get("r_multiple") or 0.0)
        win = pnl > 0

        if h_key not in by_hour:
            by_hour[h_key] = TodBucket(label=h_key)
        bh = by_hour[h_key]
        bh.trades += 1
        bh.wins += int(win)
        bh.total_pnl += pnl
        bh.sum_r += r

        if seg not in by_seg:
            by_seg[seg] = TodBucket(label=seg)
        bs = by_seg[seg]
        bs.trades += 1
        bs.wins += int(win)
        bs.total_pnl += pnl
        bs.sum_r += r

    return by_hour, by_seg


def format_tod_markdown(
    *,
    title: str,
    by_hour: dict[str, TodBucket],
    by_seg: dict[str, TodBucket],
    mode: str,
) -> str:
    lines = [
        f"# {title}",
        "",
        f"Mode: `{mode}` | Generated: {datetime.now(ET).strftime('%Y-%m-%d %H:%M %Z')}",
        "",
        "## By session segment (ET)",
        "",
        "| Segment | Trades | Win% | PnL | Avg R |",
        "|---------|-------:|-----:|----:|------:|",
    ]
    seg_order = [s[0] for s in (EQUITY_SEGMENTS if mode == "equity" else CRYPTO_SEGMENTS)]
    for name in seg_order + sorted(k for k in by_seg if k not in seg_order):
        b = by_seg.get(name)
        if not b or b.trades == 0:
            continue
        lines.append(
            f"| {b.label} | {b.trades} | {b.win_rate:.1%} | ${b.total_pnl:,.0f} | {b.avg_r:.2f} |"
        )
    lines += [
        "",
        "## By entry hour (ET)",
        "",
        "| Hour | Trades | Win% | PnL | Avg R |",
        "|------|-------:|-----:|----:|------:|",
    ]
    for h in sorted(by_hour.keys()):
        b = by_hour[h]
        lines.append(
            f"| {b.label} | {b.trades} | {b.win_rate:.1%} | ${b.total_pnl:,.0f} | {b.avg_r:.2f} |"
        )
    lines.append("")
    return "\n".join(lines)


def best_worst_hours(
    by_hour: dict[str, TodBucket], *, min_trades: int = 5
) -> tuple[TodBucket | None, TodBucket | None]:
    eligible = [b for b in by_hour.values() if b.trades >= min_trades]
    if not eligible:
        return None, None
    best = max(eligible, key=lambda b: b.avg_r)
    worst = min(eligible, key=lambda b: b.avg_r)
    return best, worst
