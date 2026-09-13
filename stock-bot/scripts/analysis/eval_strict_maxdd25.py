"""STRICT 365d risk ladder targeting MaxDD budget ~25%.

Raises halt to 25% and sweeps VTI floor / active boost / risk-per-trade.
Hygiene ON; overlays OFF (STRICT PIT). Live Profile A untouched.

Usage (from stock-bot/):
  python scripts/analysis/eval_strict_maxdd25.py
  python scripts/analysis/eval_strict_maxdd25.py --days 365
  python scripts/analysis/eval_strict_maxdd25.py --days 90   # smoke

Writes:
  scripts/analysis/eval_strict_maxdd25_last.md
  scripts/analysis/eval_strict_maxdd25_last.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PAPER_DEPLOY_DEBUG", "false")
os.environ.setdefault("PYTHONUNBUFFERED", "1")

import config

config.PAPER_DEPLOY_DEBUG = False

from backtester import MIN_HISTORY, _benchmark_return, _ensure_daily_data, run_backtest
from modules.backtester_core import RUN_OPTIONS

OUT_MD = Path(__file__).with_name("eval_strict_maxdd25_last.md")
OUT_JSON = Path(__file__).with_name("eval_strict_maxdd25_last.json")

MAX_DD_BUDGET = 0.25  # 25% halt + selection budget
DISCLAIMER = (
    "STRICT research only; MaxDD budget 25% is a halt/selection rail, not a live Profile A change"
)


@dataclass(frozen=True)
class Leg:
    name: str
    dynamic_vti: bool
    vti_floor: float
    vti_ceiling: float
    vti_fixed: float  # used when dynamic_vti=False
    boost: float
    risk: float
    notes: str = ""


# Progressive aggression ladder (halt always 25%).
LEGS: list[Leg] = [
    Leg(
        "baseline_halt25",
        dynamic_vti=True,
        vti_floor=0.40,
        vti_ceiling=0.75,
        vti_fixed=0.40,
        boost=1.40,
        risk=0.018,
        notes="current paper defaults; halt only raised",
    ),
    Leg(
        "floor20_b1.8_r2.5",
        dynamic_vti=True,
        vti_floor=0.20,
        vti_ceiling=0.70,
        vti_fixed=0.20,
        boost=1.80,
        risk=0.025,
        notes="milder core, higher boost/risk",
    ),
    Leg(
        "floor10_b2.0_r3.5",
        dynamic_vti=True,
        vti_floor=0.10,
        vti_ceiling=0.65,
        vti_fixed=0.10,
        boost=2.00,
        risk=0.035,
        notes="aggressive active sleeve",
    ),
    Leg(
        "floor0_b2.2_r4.0",
        dynamic_vti=True,
        vti_floor=0.0,
        vti_ceiling=0.60,
        vti_fixed=0.0,
        boost=2.20,
        risk=0.040,
        notes="zero-core allowed",
    ),
    Leg(
        "floor0_b2.5_r5.0",
        dynamic_vti=True,
        vti_floor=0.0,
        vti_ceiling=0.55,
        vti_fixed=0.0,
        boost=2.50,
        risk=0.050,
        notes="max ladder aggression",
    ),
    Leg(
        "fixed20_b2.0_r3.5",
        dynamic_vti=False,
        vti_floor=0.20,
        vti_ceiling=0.20,
        vti_fixed=0.20,
        boost=2.00,
        risk=0.035,
        notes="fixed 20% VTI core",
    ),
]


def _safe_float(val: Any) -> float | None:
    try:
        if val is None:
            return None
        return float(val)
    except (TypeError, ValueError):
        return None


def _safe_int(val: Any) -> int | None:
    try:
        if val is None:
            return None
        return int(val)
    except (TypeError, ValueError):
        return None


def _dd_abs_pct(max_dd_pct: float | None) -> float | None:
    """Return positive drawdown percent (e.g. 7.37 for -7.37%)."""
    if max_dd_pct is None:
        return None
    return abs(float(max_dd_pct))


@contextmanager
def _apply_leg(leg: Leg):
    saved = {
        "MAX_DRAWDOWN_PCT": config.MAX_DRAWDOWN_PCT,
        "PAPER_HALT_RESUME_DRAWDOWN_PCT": config.PAPER_HALT_RESUME_DRAWDOWN_PCT,
        "HALT_RESUME_DRAWDOWN_PCT": config.HALT_RESUME_DRAWDOWN_PCT,
        "PAPER_DYNAMIC_VTI_ENABLED": config.PAPER_DYNAMIC_VTI_ENABLED,
        "DYNAMIC_VTI_PAPER_FLOOR": config.DYNAMIC_VTI_PAPER_FLOOR,
        "DYNAMIC_VTI_PAPER_CEILING": config.DYNAMIC_VTI_PAPER_CEILING,
        "DYNAMIC_VTI_FLOOR_MIN": config.DYNAMIC_VTI_FLOOR_MIN,
        "DYNAMIC_VTI_ALLOW_ZERO": config.DYNAMIC_VTI_ALLOW_ZERO,
        "DYNAMIC_VTI_OPTIONAL_ENABLED": config.DYNAMIC_VTI_OPTIONAL_ENABLED,
        "PAPER_ACTIVE_SLEEVE_BOOST": config.PAPER_ACTIVE_SLEEVE_BOOST,
        "PAPER_RISK_PER_TRADE": config.PAPER_RISK_PER_TRADE,
        "PAPER_RISK_CALM_BULL_PCT": config.PAPER_RISK_CALM_BULL_PCT,
        "RISK_PER_TRADE": config.RISK_PER_TRADE,
        "PAPER_VTI_CORE_PCT": config.PAPER_VTI_CORE_PCT,
        "STRICT_PIT_BACKTEST": config.STRICT_PIT_BACKTEST,
        "thinking": config.PAPER_THINKING_ENGINE_ENABLED,
        "strict_ctx": config.backtest_strict_pit_context(),
        "strict_allow": set(config.strict_pit_allow()),
    }
    try:
        config.MAX_DRAWDOWN_PCT = MAX_DD_BUDGET
        config.PAPER_HALT_RESUME_DRAWDOWN_PCT = min(0.20, MAX_DD_BUDGET * 0.8)
        config.HALT_RESUME_DRAWDOWN_PCT = min(0.20, MAX_DD_BUDGET * 0.8)
        config.PAPER_DYNAMIC_VTI_ENABLED = bool(leg.dynamic_vti)
        config.DYNAMIC_VTI_PAPER_FLOOR = float(leg.vti_floor)
        config.DYNAMIC_VTI_PAPER_CEILING = float(leg.vti_ceiling)
        config.DYNAMIC_VTI_FLOOR_MIN = float(leg.vti_floor)
        config.DYNAMIC_VTI_ALLOW_ZERO = bool(leg.vti_floor <= 0)
        config.DYNAMIC_VTI_OPTIONAL_ENABLED = bool(leg.vti_floor <= 0)
        config.PAPER_ACTIVE_SLEEVE_BOOST = float(leg.boost)
        config.PAPER_RISK_PER_TRADE = float(leg.risk)
        config.PAPER_RISK_CALM_BULL_PCT = float(leg.risk)
        config.RISK_PER_TRADE = float(leg.risk)
        config.PAPER_VTI_CORE_PCT = float(leg.vti_fixed)
        config.STRICT_PIT_BACKTEST = True
        RUN_OPTIONS.strict_pit = True
        RUN_OPTIONS.no_thinking = True
        config.apply_strict_pit_kill_switches(allow=None)
        yield
    finally:
        for k, v in saved.items():
            if k == "thinking":
                config.PAPER_THINKING_ENGINE_ENABLED = v
            elif k == "strict_ctx":
                config.set_backtest_strict_pit_context(v)
            elif k == "strict_allow":
                config.set_strict_pit_allow(v)
            else:
                setattr(config, k, v)
        RUN_OPTIONS.strict_pit = False
        RUN_OPTIONS.no_thinking = False


def _extract(result: dict | None, leg: Leg) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": leg.name,
        "ok": False,
        "verdict": "HOLD",
        "return_pct": None,
        "sharpe": None,
        "max_dd_pct": None,
        "max_dd_abs": None,
        "within_budget": None,
        "trade_count": None,
        "nyse_fills": None,
        "error": None,
        "leg": asdict(leg),
        "notes": leg.notes,
    }
    if not isinstance(result, dict):
        row["error"] = "missing_result"
        return row
    ret = _safe_float(result.get("total_return_pct"))
    sharpe = _safe_float(result.get("sharpe"))
    max_dd = _safe_float(result.get("max_drawdown_pct"))
    if ret is None or sharpe is None or max_dd is None:
        row["error"] = "metrics_parse_failed"
        row["return_pct"] = ret
        row["sharpe"] = sharpe
        row["max_dd_pct"] = max_dd
        return row
    dd_abs = _dd_abs_pct(max_dd)
    within = dd_abs is not None and dd_abs <= (MAX_DD_BUDGET * 100.0 + 0.05)
    row.update(
        {
            "ok": bool(result.get("ok", True)),
            "verdict": "OK" if result.get("ok", True) else "HOLD",
            "return_pct": ret,
            "sharpe": sharpe,
            "max_dd_pct": max_dd,
            "max_dd_abs": dd_abs,
            "within_budget": within,
            "trade_count": _safe_int(result.get("total_orders")) or 0,
            "nyse_fills": _safe_int(result.get("nyse_signals")) or 0,
            "final_equity": _safe_float(result.get("final_equity")),
            "avg_vti_core": _safe_float(result.get("vti_core_pct")),
            "start_date": result.get("start_date"),
            "end_date": result.get("end_date"),
            "halt_events": _safe_int(result.get("halt_events")),
        }
    )
    return row


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.2f}%"


def _fmt_num(v: float | None, d: int = 2) -> str:
    if v is None:
        return "n/a"
    return f"{v:.{d}f}"


def _pick_winner(rows: list[dict]) -> dict | None:
    """Best return among legs with MaxDD abs <= 25% and ok=True."""
    eligible = [
        r
        for r in rows
        if r.get("ok") and r.get("within_budget") and r.get("return_pct") is not None
    ]
    if not eligible:
        return None
    # Primary: return; tie-break Sharpe then shallower DD.
    return max(
        eligible,
        key=lambda r: (
            float(r["return_pct"]),
            float(r["sharpe"] or -999),
            -float(r["max_dd_abs"] or 999),
        ),
    )


def _verdict(rows: list[dict], baseline: dict | None, winner: dict | None) -> str:
    if not any(r.get("ok") for r in rows):
        return "HOLD - no successful legs. " + DISCLAIMER
    if winner is None:
        return (
            "No leg stayed within 25% MaxDD with valid metrics "
            "(or all exceeded budget). " + DISCLAIMER
        )
    base_ret = baseline.get("return_pct") if baseline and baseline.get("ok") else None
    delta = (
        float(winner["return_pct"]) - float(base_ret)
        if base_ret is not None
        else None
    )
    parts = [
        f"Winner under 25% MaxDD: {winner['name']} "
        f"return {_fmt_pct(winner.get('return_pct'))} "
        f"Sharpe {_fmt_num(winner.get('sharpe'))} "
        f"MaxDD {_fmt_pct(winner.get('max_dd_pct'))}."
    ]
    if delta is not None:
        parts.append(f"vs baseline_halt25: {delta:+.2f}pp return.")
    if winner["name"] == "baseline_halt25":
        parts.append(
            "Aggression ladder did not beat baseline within budget "
            "(or did not use much more DD)."
        )
    elif (winner.get("max_dd_abs") or 0) < 12:
        parts.append(
            "Winning leg still far from 25% DD - more aggression may still be available."
        )
    else:
        parts.append("Closer to the 25% DD budget - check halt_events and Sharpe.")
    parts.append(DISCLAIMER)
    return " ".join(parts)


def _write(rows: list[dict], *, days: int, window: str, bench: float | None, verdict: str) -> None:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    winner = _pick_winner(rows)
    baseline = next((r for r in rows if r["name"] == "baseline_halt25"), None)
    payload = {
        "generated_at": generated,
        "days": days,
        "window": window,
        "max_dd_budget_pct": MAX_DD_BUDGET * 100.0,
        "benchmark_return_pct": bench,
        "disclaimer": DISCLAIMER,
        "legs": rows,
        "winner": winner["name"] if winner else None,
        "verdict": verdict,
        "ok": all(bool(r.get("ok")) for r in rows),
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines = [
        "# STRICT MaxDD 25% risk ladder",
        "",
        f"Generated: {generated}",
        f"Window: {window} ({days}d requested)",
        f"Halt / budget: {MAX_DD_BUDGET:.0%} MaxDD",
        f"Benchmark VTI B&H: {_fmt_pct(bench) if bench is not None else 'n/a'}",
        "",
        f"**{DISCLAIMER}**",
        "",
        "| Leg | Return | Sharpe | MaxDD | In budget | Trades | NYSE | Notes |",
        "|-----|--------|--------|-------|-----------|--------|------|-------|",
    ]
    for r in rows:
        status = "OK" if r.get("ok") else "HOLD"
        bud = (
            "YES"
            if r.get("within_budget")
            else ("NO" if r.get("within_budget") is False else "n/a")
        )
        mark = " **" if winner and r["name"] == winner["name"] else ""
        lines.append(
            f"| {r['name']}{mark} ({status}) "
            f"| {_fmt_pct(r.get('return_pct'))} "
            f"| {_fmt_num(r.get('sharpe'))} "
            f"| {_fmt_pct(r.get('max_dd_pct'))} "
            f"| {bud} "
            f"| {r.get('trade_count') if r.get('trade_count') is not None else 'n/a'} "
            f"| {r.get('nyse_fills') if r.get('nyse_fills') is not None else 'n/a'} "
            f"| {r.get('notes') or ''} |"
        )
    lines.extend(["", "## Verdict", "", verdict, ""])
    if winner:
        leg = winner.get("leg") or {}
        lines.extend(
            [
                "## Suggested research knobs (winner)",
                "",
                f"- `MAX_DRAWDOWN_PCT={MAX_DD_BUDGET}`",
                f"- `PAPER_DYNAMIC_VTI={'true' if leg.get('dynamic_vti') else 'false'}`",
                f"- `DYNAMIC_VTI_PAPER_FLOOR={leg.get('vti_floor')}`",
                f"- `DYNAMIC_VTI_PAPER_CEILING={leg.get('vti_ceiling')}`",
                f"- `DYNAMIC_VTI_FLOOR_MIN={leg.get('vti_floor')}`",
                f"- `DYNAMIC_VTI_ALLOW_ZERO={'true' if float(leg.get('vti_floor') or 0) <= 0 else 'false'}`",
                f"- `PAPER_ACTIVE_SLEEVE_BOOST={leg.get('boost')}`",
                f"- `PAPER_RISK_PER_TRADE={leg.get('risk')}`",
                f"- `PAPER_VTI_CORE_PCT={leg.get('vti_fixed')}` (if fixed core)",
                f"- `--strict-pit --paper-aggressive --no-thinking`",
                "",
            ]
        )
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="STRICT MaxDD 25% aggression ladder")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument(
        "--legs",
        default="",
        help="Comma subset of leg names (default: all)",
    )
    args = ap.parse_args()
    days = max(20, int(args.days))

    wanted = {x.strip() for x in str(args.legs).split(",") if x.strip()}
    legs = [L for L in LEGS if not wanted or L.name in wanted]
    if not legs:
        raise SystemExit(f"No matching legs. Choose from: {[L.name for L in LEGS]}")

    print(f"--- STRICT MaxDD {MAX_DD_BUDGET:.0%} ladder ({days}d) ---")
    print(f"Legs: {', '.join(L.name for L in legs)}")
    data = _ensure_daily_data(days, refresh=args.refresh, use_max=False)
    if len(data) < 20:
        print(f"Need >=20 bars; got {len(data)}")
        return 1
    warmup = min(MIN_HISTORY, max(0, len(data) - 5))
    window = f"{data.index[warmup].date()} -> {data.index[-1].date()}"
    bench = _benchmark_return(data, warmup)
    print(f"Window: {window} ({len(data) - warmup} sim bars)")
    if bench is not None:
        print(f"VTI B&H: {bench:+.2f}%")

    rows: list[dict] = []
    for leg in legs:
        print(f"\n>>> {leg.name} ...")
        print(
            f"    floor={leg.vti_floor:.0%} boost={leg.boost:.2f}x "
            f"risk={leg.risk:.1%} dyn={leg.dynamic_vti}"
        )
        with _apply_leg(leg):
            raw = run_backtest(
                data,
                track_active_exposure=True,
                track_metrics=True,
                paper_aggressive=True,
                paper_sleeve_features=True,
                paper_dynamic_vti=leg.dynamic_vti,
                vti_core_pct=leg.vti_fixed if not leg.dynamic_vti else 0.0,
                paper_thinking=False,
                with_news=False,
                strict_pit=True,
                paper_nyse_entry_hygiene=True,
                paper_crypto_enabled=False,
                verbose=False,
            )
        row = _extract(raw, leg)
        rows.append(row)
        print(
            f"    -> ret {_fmt_pct(row.get('return_pct'))} "
            f"Sharpe {_fmt_num(row.get('sharpe'))} "
            f"MaxDD {_fmt_pct(row.get('max_dd_pct'))} "
            f"budget={'YES' if row.get('within_budget') else 'NO'}"
        )

    print(
        f"\n{'Leg':<22} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} {'OK?':>5}"
    )
    print("-" * 56)
    for r in rows:
        print(
            f"{r['name']:<22} "
            f"{_fmt_pct(r.get('return_pct')):>8} "
            f"{_fmt_num(r.get('sharpe')):>7} "
            f"{_fmt_pct(r.get('max_dd_pct')):>8} "
            f"{'YES' if r.get('within_budget') else 'NO':>5}"
        )
    print("-" * 56)

    baseline = next((r for r in rows if r["name"] == "baseline_halt25"), None)
    winner = _pick_winner(rows)
    verdict = _verdict(rows, baseline, winner)
    _write(rows, days=days, window=window, bench=bench, verdict=verdict)
    print("\n## Verdict")
    try:
        print(verdict)
    except UnicodeEncodeError:
        print(verdict.encode("ascii", "replace").decode("ascii"))
    print(f"\nWrote {OUT_MD.name} and {OUT_JSON.name}")
    return 0 if all(r.get("ok") for r in rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
