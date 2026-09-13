"""Research-only A/B/C: paper v2 33/67 vs 100% NYSE vs 100% VTI.

Adapted from scripts/analysis/one_r_hit_backtest.py (scripts/research/one_r_hit_backtest.py
does not exist). Daily closes. No costs/slippage (same as that script).

Does not write .env, restart bots, or change config.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

import config  # noqa: E402
from modules.data_loader import load_close_matrix  # noqa: E402
from modules.one_r_hit_test import plan_from_entry  # noqa: E402

OUT_MD = Path(__file__).with_name("ab_33_67_vs_nyse100_last.md")
OUT_JSON = Path(__file__).with_name("ab_33_67_vs_nyse100_last.json")

WINDOW_START = date(2025, 8, 11)
WINDOW_END = date(2026, 9, 4)
START_EQUITY = 100_000.0
SKIP = frozenset({"VTI", "VOO", "SPY", "QQQ", "BND", "AGG"})
PARTIAL_FRAC = 0.50
PER_NAME_MAX = 0.10


def _universe(columns) -> list[str]:
    cols = config.nyse_momentum_universe(columns)
    out = []
    for c in cols:
        n = config.normalize_symbol(c)
        if not n or n in SKIP or config.is_crypto(n):
            continue
        out.append(n)
    return out


def _as_date(idx_val) -> date:
    return pd.Timestamp(idx_val).date()


def _sharpe(equity: list[float]) -> float | None:
    rets = pd.Series(equity, dtype=float).pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    if len(rets) < 5:
        return None
    std = float(rets.std(ddof=1))
    if std <= 0 or not math.isfinite(std):
        return None
    return float(rets.mean()) / std * math.sqrt(252.0)


def _cagr(start_eq: float, end_eq: float, start_d: date, end_d: date) -> float | None:
    days = max(1, (end_d - start_d).days)
    if start_eq <= 0 or end_eq <= 0:
        return None
    return (end_eq / start_eq) ** (365.25 / days) - 1.0


def _max_dd(equity: list[float]) -> float:
    peak = equity[0]
    dd = 0.0
    for x in equity:
        peak = max(peak, x)
        if peak > 0:
            dd = max(dd, (peak - x) / peak)
    return dd


@dataclass
class Pos:
    symbol: str
    entry_i: int
    entry: float
    stop: float
    target: float
    notional: float
    orig_notional: float
    peak: float
    realized_pnl: float = 0.0
    partial_taken: bool = False
    touched_1r: bool = False
    exit_reasons: list[str] = field(default_factory=list)


@dataclass
class RoundTrip:
    symbol: str
    bars: int
    pnl: float
    ret_pct: float
    touched_1r: bool
    reasons: list[str]


def _clip(equity: float, sleeve: float, max_active: int, cash: float) -> float:
    if equity <= 0 or max_active <= 0:
        return 0.0
    raw = (equity * sleeve) / max_active
    return max(0.0, min(raw, equity * PER_NAME_MAX, cash))


def _fmt_pct(x: float | None, digits: int = 2) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "n/a"
    return f"{x * 100:.{digits}f}%"


def _fmt_num(x: float | None, digits: int = 2) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "n/a"
    return f"{x:.{digits}f}"


def run_book(
    frame: pd.DataFrame,
    atr: pd.DataFrame,
    mom: pd.DataFrame,
    above: pd.DataFrame,
    symbols: list[str],
    vti: pd.Series,
    *,
    start_i: int,
    end_i: int,
    start_d: date,
    end_d: date,
    sleeve: float,
    vti_frac: float,
    max_active: int,
    max_hold: int,
) -> dict:
    cash = START_EQUITY
    vti_qty = 0.0
    open_pos: dict[str, Pos] = {}
    trips: list[RoundTrip] = []
    equity_curve: list[float] = []
    arm = float(getattr(config, "TRAIL_ARM_PCT", 0.50))
    pull = float(getattr(config, "TRAIL_PULLBACK_PCT", 0.35))

    vti0 = float(vti.iloc[start_i])
    if vti_frac > 0:
        if not math.isfinite(vti0) or vti0 <= 0:
            raise RuntimeError("VTI close missing at window start")
        spend = START_EQUITY * vti_frac
        vti_qty = spend / vti0
        cash -= spend

    def nyse_mtm(i: int) -> float:
        tot = 0.0
        for p in open_pos.values():
            px = float(frame[p.symbol].iloc[i])
            if math.isfinite(px) and px > 0 and p.entry > 0:
                tot += p.notional * (px / p.entry)
        return tot

    def vti_mtm(i: int) -> float:
        px = float(vti.iloc[i])
        if math.isfinite(px) and px > 0:
            return vti_qty * px
        return 0.0

    def total_eq(i: int) -> float:
        return cash + vti_mtm(i) + nyse_mtm(i)

    def close_out(pos: Pos, px: float, reason: str, take_notional: float) -> None:
        nonlocal cash
        if take_notional <= 0:
            return
        cash += take_notional * (px / pos.entry)
        pos.realized_pnl += take_notional * (px / pos.entry - 1.0)
        pos.notional -= take_notional
        pos.exit_reasons.append(reason)

    def finish_trip(pos: Pos, i: int) -> None:
        orig = pos.orig_notional
        trips.append(
            RoundTrip(
                symbol=pos.symbol,
                bars=i - pos.entry_i,
                pnl=pos.realized_pnl,
                ret_pct=(pos.realized_pnl / orig * 100.0) if orig else 0.0,
                touched_1r=pos.touched_1r,
                reasons=list(pos.exit_reasons),
            )
        )

    for i in range(start_i, end_i + 1):
        last_bar = i == end_i
        for sym in list(open_pos.keys()):
            pos = open_pos[sym]
            px = float(frame[sym].iloc[i])
            if not math.isfinite(px) or px <= 0:
                continue
            pos.peak = max(pos.peak, px)
            if px >= pos.target:
                pos.touched_1r = True
            bars_held = i - pos.entry_i

            if last_bar and pos.notional > 0:
                close_out(pos, px, "eod", pos.notional)
                finish_trip(pos, i)
                open_pos.pop(sym)
                continue

            if px <= pos.stop:
                close_out(pos, px, "stop", pos.notional)
                finish_trip(pos, i)
                open_pos.pop(sym)
                continue

            if not pos.partial_taken and px >= pos.target:
                close_out(pos, px, "1r_partial", pos.notional * PARTIAL_FRAC)
                pos.partial_taken = True

            if pos.partial_taken and pos.notional > 0:
                gain = (pos.peak - pos.entry) / pos.entry if pos.entry else 0.0
                if gain >= arm and px <= pos.peak * (1.0 - pull):
                    close_out(pos, px, "trail", pos.notional)
                    finish_trip(pos, i)
                    open_pos.pop(sym)
                    continue

            if bars_held >= max_hold and pos.notional > 0:
                close_out(pos, px, "time", pos.notional)
                finish_trip(pos, i)
                open_pos.pop(sym)
                continue

        eq = total_eq(i)
        if not last_bar and len(open_pos) < max_active:
            scores: list[tuple[float, str]] = []
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
            for _m, sym in scores[: max_active - len(open_pos)]:
                px = float(frame[sym].iloc[i])
                if not math.isfinite(px) or px <= 0:
                    continue
                a = float(atr[sym].iloc[i]) if sym in atr.columns else float("nan")
                if not math.isfinite(a) or a <= 0:
                    continue
                plan = plan_from_entry(px, a, rr=1.0)
                clip = _clip(eq, sleeve, max_active, cash)
                if clip < 1.0:
                    continue
                cash -= clip
                open_pos[sym] = Pos(
                    symbol=sym,
                    entry_i=i,
                    entry=px,
                    stop=float(plan["stop"]),
                    target=float(plan["target"]),
                    notional=clip,
                    orig_notional=clip,
                    peak=px,
                )
                eq = total_eq(i)

        equity_curve.append(total_eq(i))

    end_eq = float(equity_curve[-1])
    n = len(trips)
    wins = sum(1 for t in trips if t.pnl > 0)
    hits = sum(1 for t in trips if t.touched_1r)
    holds = [t.bars for t in trips]
    reason_counts = {"stop": 0, "time": 0, "1r_partial": 0, "trail": 0, "eod": 0}
    for t in trips:
        for r in t.reasons:
            if r in reason_counts:
                reason_counts[r] += 1
    return {
        "equity_start": START_EQUITY,
        "equity_end": round(end_eq, 2),
        "total_return": end_eq / START_EQUITY - 1.0,
        "cagr": _cagr(START_EQUITY, end_eq, start_d, end_d),
        "sharpe": _sharpe(equity_curve),
        "max_dd": _max_dd(equity_curve),
        "trades": n,
        "win_pct": (100.0 * wins / n) if n else 0.0,
        "hit_1r_pct": (100.0 * hits / n) if n else 0.0,
        "avg_hold": float(np.mean(holds)) if holds else 0.0,
        "exits": reason_counts,
        "start_date": str(start_d),
        "end_date": str(end_d),
        "bars": end_i - start_i + 1,
        "end_cash": round(cash, 2),
        "vti_end": round(vti_mtm(end_i), 2),
    }


def run_vti_only(
    vti: pd.Series, start_i: int, end_i: int, start_d: date, end_d: date
) -> dict:
    px0 = float(vti.iloc[start_i])
    if not math.isfinite(px0) or px0 <= 0:
        raise RuntimeError("VTI close missing at window start")
    qty = START_EQUITY / px0
    curve: list[float] = []
    for i in range(start_i, end_i + 1):
        px = float(vti.iloc[i])
        if math.isfinite(px) and px > 0:
            curve.append(qty * px)
        else:
            curve.append(curve[-1] if curve else START_EQUITY)
    end_eq = float(curve[-1])
    ret = end_eq / START_EQUITY - 1.0
    return {
        "equity_start": START_EQUITY,
        "equity_end": round(end_eq, 2),
        "total_return": ret,
        "cagr": _cagr(START_EQUITY, end_eq, start_d, end_d),
        "sharpe": _sharpe(curve),
        "max_dd": _max_dd(curve),
        "trades": 1,
        "win_pct": 100.0 if ret > 0 else 0.0,
        "hit_1r_pct": float("nan"),
        "avg_hold": float(end_i - start_i),
        "exits": {"stop": 0, "time": 0, "1r_partial": 0, "trail": 0, "eod": 1},
        "start_date": str(start_d),
        "end_date": str(end_d),
        "bars": end_i - start_i + 1,
        "end_cash": 0.0,
        "vti_end": round(end_eq, 2),
    }


def write_md(rows: list[dict], *, meta: dict) -> str:
    lines = [
        "# A/B/C — paper v2 33/67 vs 100% NYSE vs 100% VTI",
        "",
        "Research only. No `.env` change, no restart, no orders.",
        "",
        f"**Window:** {meta['start']} → {meta['end']} ({meta['bars']} daily bars, {meta['symbols']} NYSE names).",
        "**Source:** adapted from `scripts/analysis/one_r_hit_backtest.py` (`scripts/research/one_r_hit_backtest.py` does not exist).",
        "**Costs / slippage:** **off** (same as the source script — no model added).",
        f"**NYSE engine:** MA{meta['ma']} rank, max {meta['max_active']} names, clip = min(book×sleeve/15, 10%/name, cash). "
        f"Stop = max(2× daily ATR, 1% of entry). Max hold {meta['max_hold']} daily bars.",
        "**Exits in this run:** 50% partial at 1R on daily close, then trail on the remainder "
        f"(arm `TRAIL_ARM_PCT`={meta['arm']:.0%} from entry, pullback `TRAIL_PULLBACK_PCT`={meta['pull']:.0%} of peak). "
        "No conviction/regime scale.",
        "**Not added:** RHYME, scanners, RSI/hygiene, thinking, extra sleeves.",
        "**A:** 33% VTI shares at window open, never rebalanced (drift). 67% NYSE path. VTI marked daily.",
        "**B:** 100% NYSE path (sleeve=1.0), same engine/exits.",
        "**C:** 100% VTI buy-and-hold from the same open.",
        "",
        "| Arm | Start | End | Return | CAGR | Sharpe | Max DD | Trades | Win% | 1R-touch% | Avg hold | stop | time | 1R-partial | trail | eod | vs C (pp) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        ex = r["exits"]
        hit = r["hit_1r_pct"]
        hit_s = "n/a" if not math.isfinite(float(hit)) else f"{hit:.1f}%"
        lines.append(
            "| {name} | {start:,.0f} | {end:,.2f} | {ret} | {cagr} | {sh} | {dd} | {tr} | {win:.1f}% | {hit} | {hold:.1f} | "
            "{stop} | {time} | {p} | {trail} | {eod} | {vs:+.2f} |".format(
                name=r["name"],
                start=r["equity_start"],
                end=r["equity_end"],
                ret=_fmt_pct(r["total_return"]),
                cagr=_fmt_pct(r["cagr"]),
                sh=_fmt_num(r["sharpe"]),
                dd=_fmt_pct(r["max_dd"]),
                tr=r["trades"],
                win=r["win_pct"],
                hit=hit_s,
                hold=r["avg_hold"],
                stop=ex.get("stop", 0),
                time=ex.get("time", 0),
                p=ex.get("1r_partial", 0),
                trail=ex.get("trail", 0),
                eod=ex.get("eod", 0),
                vs=r["vs_c_pp"],
            )
        )
    lines += [
        "",
        "## What won",
        "",
        meta["verdict"],
        "",
        "## Why this is NOT a promote",
        "",
        meta["not_promote"],
        "",
        "## Still missing vs live paper v2",
        "",
        meta["missing"],
        "",
        f"10%/name cap did not bind (67%/15 ≈ 4.47% of book; 100%/15 ≈ 6.67%). "
        f"Trail arms only after a **+{meta['arm']:.0%} price gain from entry**, so trail exits on a 30-bar MA70 clip are expected to be rare.",
        "",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ma_win = int(getattr(config, "PAPER_NYSE_MA_WINDOW", 70))
    max_hold = int(getattr(config, "PAPER_POSITION_MAX_HOLD_BARS", 30))
    max_active = 15
    atr_period = int(getattr(config, "ATR_PERIOD", 14))
    need = (WINDOW_END - WINDOW_START).days + ma_win + 40
    print(f"Loading daily closes (~{need} rows, MA{ma_win})...")
    data = load_close_matrix(interval="1d", days=need)
    if data is None or data.empty:
        print("No daily history")
        return 1
    data = data.sort_index()
    if "VTI" not in data.columns:
        print("VTI missing from close matrix")
        return 1
    symbols = [c for c in _universe(data.columns) if c in data.columns]
    frame = data[symbols].apply(pd.to_numeric, errors="coerce")
    vti = pd.to_numeric(data["VTI"], errors="coerce").ffill()
    ma = frame.rolling(ma_win, min_periods=ma_win).mean()
    mom = frame / ma - 1.0
    above = frame > ma
    atr = frame.diff().abs().rolling(atr_period, min_periods=atr_period).mean()

    start_i = end_i = None
    for i, ts in enumerate(data.index):
        d = _as_date(ts)
        if start_i is None and d >= WINDOW_START:
            start_i = i
        if d <= WINDOW_END:
            end_i = i
    if start_i is None or end_i is None or end_i <= start_i:
        print("Window not in data")
        return 1

    actual_start = _as_date(data.index[start_i])
    actual_end = _as_date(data.index[end_i])
    print(f"Sim {actual_start} -> {actual_end}  bars={end_i - start_i + 1}  names={len(symbols)}")

    common = dict(
        frame=frame,
        atr=atr,
        mom=mom,
        above=above,
        symbols=symbols,
        vti=vti,
        start_i=start_i,
        end_i=end_i,
        start_d=actual_start,
        end_d=actual_end,
        max_active=max_active,
        max_hold=max_hold,
    )
    print("Running A (33/67)...")
    a = run_book(**common, sleeve=0.67, vti_frac=0.33)
    print("Running B (100% NYSE)...")
    b = run_book(**common, sleeve=1.0, vti_frac=0.0)
    print("Running C (100% VTI)...")
    c = run_vti_only(vti, start_i, end_i, actual_start, actual_end)

    c_ret = float(c["total_return"])

    def pack(name: str, r: dict) -> dict:
        out = dict(r)
        out["name"] = name
        out["vs_c_pp"] = (r["total_return"] - c_ret) * 100.0
        return out

    rows = [pack("A 33/67", a), pack("B 100% NYSE", b), pack("C 100% VTI", c)]
    winner = max(rows, key=lambda r: r["total_return"])["name"]
    arm = float(getattr(config, "TRAIL_ARM_PCT", 0.50))
    pull = float(getattr(config, "TRAIL_PULLBACK_PCT", 0.35))
    verdict = (
        f"**{winner}** won on total return this window "
        f"(A {_fmt_pct(a['total_return'])} / Sharpe {_fmt_num(a['sharpe'])} / DD {_fmt_pct(a['max_dd'])}; "
        f"B {_fmt_pct(b['total_return'])} / Sharpe {_fmt_num(b['sharpe'])} / DD {_fmt_pct(b['max_dd'])}; "
        f"C {_fmt_pct(c['total_return'])} / Sharpe {_fmt_num(c['sharpe'])} / DD {_fmt_pct(c['max_dd'])}). "
        "B is the profit-max toolkit layout (all capital in the same NYSE engine). "
        "A is that engine with 33% VTI ballast marked. "
        f"Trail exits on B = {b['exits'].get('trail', 0)} "
        f"(arm is +{arm:.0%} from entry, so remainder usually dies on stop/time, not trail)."
    )
    not_promote = (
        "Daily closes only; no 5-minute path, no commissions/slippage, no RHYME pause/sizing, "
        "no RSI/hygiene/same-day block, no scanners. Partial+trail is a close-to-close proxy of the "
        "paper exit knobs, not the live fill engine. One ~390-bar window is not a promote. "
        "B’s extra return is extra NYSE risk (deeper DD than A/C). Do not copy B to paper/live."
    )
    missing = (
        "Live paper v2 still has RHYME primary (paper B sizes ×0.3; live hard-pauses B/E), GARCH, "
        "conviction 0.4–2.0×, RVOL/ORB-scanner/catalyst/MTF/insider rank boosts, RSI 70/72, "
        "open cooldown, max 2 adds, same-day rebuy block, correlation/concentration guards, "
        "daily banking, smart-stop tighten @ −5%/hard −10%, 1R **intraday** (this test uses daily close), "
        "and yield-gate override. VTI here is buy-and-hold shares; paper does not trade VTI daily. "
        "Forward 33/67 from the Sep 2 $100k restock is +0.4% in 3 sessions — not this sim."
    )
    md = write_md(
        rows,
        meta={
            "start": str(actual_start),
            "end": str(actual_end),
            "bars": end_i - start_i + 1,
            "symbols": len(symbols),
            "ma": ma_win,
            "max_active": max_active,
            "max_hold": max_hold,
            "arm": arm,
            "pull": pull,
            "verdict": verdict,
            "not_promote": not_promote,
            "missing": missing,
        },
    )
    OUT_MD.write_text(md, encoding="utf-8")
    OUT_JSON.write_text(json.dumps({"A": a, "B": b, "C": c}, indent=2, default=str), encoding="utf-8")
    print()
    print(md)
    print(f"Wrote {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
