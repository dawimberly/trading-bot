"""STRICT 365d: spend MaxDD budget via concentration / sleeve caps (not VTI floor alone).

Builds on prior winner floor10_b2.0_r3.5; raises NYSE/SPY caps, per-name limits,
disables concentration guard / stat-arb diversifier where noted. Halt 25%.

Usage (from stock-bot/):
  python scripts/analysis/eval_strict_maxdd25_concentration.py
  python scripts/analysis/eval_strict_maxdd25_concentration.py --days 90

Writes:
  scripts/analysis/eval_strict_maxdd25_concentration_last.md
  scripts/analysis/eval_strict_maxdd25_concentration_last.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
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

OUT_MD = Path(__file__).with_name("eval_strict_maxdd25_concentration_last.md")
OUT_JSON = Path(__file__).with_name("eval_strict_maxdd25_concentration_last.json")

MAX_DD_BUDGET = 0.25
DISCLAIMER = (
    "STRICT research only; higher concentration is not live Profile A; MaxDD 25% is a halt rail"
)

# Prior ladder winner (floor10_b2.0_r3.5).
WINNER = dict(
    dynamic_vti=True,
    vti_floor=0.10,
    vti_ceiling=0.65,
    vti_fixed=0.10,
    boost=2.00,
    risk=0.035,
)


@dataclass(frozen=True)
class Leg:
    name: str
    notes: str = ""
    dynamic_vti: bool = True
    vti_floor: float = 0.10
    vti_ceiling: float = 0.65
    vti_fixed: float = 0.10
    boost: float = 2.00
    risk: float = 0.035
    nyse_cap: float | None = None
    nyse_max_exp: float | None = None
    spy_cap: float | None = None
    spy_max_exp: float | None = None
    max_position_pct: float | None = None
    max_active_tickers: int | None = None
    concentration_guard: bool | None = None
    stat_arb_cap: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)


LEGS: list[Leg] = [
    Leg("winner_base", notes="prior winner floor10 b2.0 r3.5", **WINNER),
    Leg(
        "nyse35_pos12",
        notes="raise NYSE sleeve + per-name cap",
        nyse_cap=0.35,
        nyse_max_exp=0.38,
        max_position_pct=0.12,
        **WINNER,
    ),
    Leg(
        "nyse45_pos18_noguard",
        notes="NYSE 45%, 18%/name, concentration guard OFF",
        nyse_cap=0.45,
        nyse_max_exp=0.50,
        max_position_pct=0.18,
        concentration_guard=False,
        **WINNER,
    ),
    Leg(
        "spy55_nyse40",
        notes="heavy SPY+NYSE caps, low VTI ceiling",
        vti_ceiling=0.50,
        spy_cap=0.55,
        spy_max_exp=0.58,
        nyse_cap=0.40,
        nyse_max_exp=0.45,
        max_position_pct=0.20,
        concentration_guard=False,
        **{k: v for k, v in WINNER.items() if k != "vti_ceiling"},
    ),
    Leg(
        "no_stat_arb",
        notes="winner + stat-arb sleeve cap disabled",
        stat_arb_cap=False,
        **WINNER,
    ),
    Leg(
        "conc_stack",
        notes="max concentration stack (few names, big sleeves)",
        vti_floor=0.0,
        vti_ceiling=0.45,
        vti_fixed=0.0,
        boost=3.00,
        risk=0.060,
        spy_cap=0.55,
        spy_max_exp=0.60,
        nyse_cap=0.45,
        nyse_max_exp=0.50,
        max_position_pct=0.25,
        max_active_tickers=8,
        concentration_guard=False,
        stat_arb_cap=False,
        dynamic_vti=True,
    ),
    Leg(
        "conc_stack_r8",
        notes="conc_stack + 8% risk/trade",
        vti_floor=0.0,
        vti_ceiling=0.40,
        boost=3.50,
        risk=0.080,
        spy_cap=0.60,
        spy_max_exp=0.65,
        nyse_cap=0.50,
        nyse_max_exp=0.55,
        max_position_pct=0.30,
        max_active_tickers=6,
        concentration_guard=False,
        stat_arb_cap=False,
        dynamic_vti=True,
        vti_fixed=0.0,
    ),
]


def _safe_float(val: Any) -> float | None:
    try:
        return None if val is None else float(val)
    except (TypeError, ValueError):
        return None


def _safe_int(val: Any) -> int | None:
    try:
        return None if val is None else int(val)
    except (TypeError, ValueError):
        return None


@contextmanager
def _apply_leg(leg: Leg):
    keys = [
        "MAX_DRAWDOWN_PCT",
        "PAPER_HALT_RESUME_DRAWDOWN_PCT",
        "HALT_RESUME_DRAWDOWN_PCT",
        "PAPER_DYNAMIC_VTI_ENABLED",
        "DYNAMIC_VTI_PAPER_FLOOR",
        "DYNAMIC_VTI_PAPER_CEILING",
        "DYNAMIC_VTI_FLOOR_MIN",
        "DYNAMIC_VTI_ALLOW_ZERO",
        "DYNAMIC_VTI_OPTIONAL_ENABLED",
        "PAPER_ACTIVE_SLEEVE_BOOST",
        "PAPER_RISK_PER_TRADE",
        "PAPER_RISK_CALM_BULL_PCT",
        "RISK_PER_TRADE",
        "PAPER_VTI_CORE_PCT",
        "NYSE_SLEEVE_CAP_PCT",
        "PAPER_NYSE_SLEEVE_CAP_PCT",
        "PAPER_NYSE_MAX_EXPOSURE_PCT",
        "SPY_SLEEVE_CAP_PCT",
        "PAPER_SPY_MAX_EXPOSURE_PCT",
        "PAPER_MAX_POSITION_PCT",
        "PER_NAME_MAX_PCT",
        "CONCENTRATION_GUARD_ENABLED",
        "STAT_ARB_SLEEVE_CAP_ENABLED",
        "MAX_ACTIVE_TICKERS",
        "STRICT_PIT_BACKTEST",
    ]
    saved = {k: getattr(config, k) for k in keys}
    saved["thinking"] = config.PAPER_THINKING_ENGINE_ENABLED
    saved["strict_ctx"] = config.backtest_strict_pit_context()
    saved["strict_allow"] = set(config.strict_pit_allow())
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
        if leg.nyse_cap is not None:
            config.NYSE_SLEEVE_CAP_PCT = float(leg.nyse_cap)
            config.PAPER_NYSE_SLEEVE_CAP_PCT = float(leg.nyse_cap)
        if leg.nyse_max_exp is not None:
            config.PAPER_NYSE_MAX_EXPOSURE_PCT = float(leg.nyse_max_exp)
        if leg.spy_cap is not None:
            config.SPY_SLEEVE_CAP_PCT = float(leg.spy_cap)
        if leg.spy_max_exp is not None:
            config.PAPER_SPY_MAX_EXPOSURE_PCT = float(leg.spy_max_exp)
        if leg.max_position_pct is not None:
            config.PAPER_MAX_POSITION_PCT = float(leg.max_position_pct)
            config.PER_NAME_MAX_PCT = float(leg.max_position_pct)
        if leg.max_active_tickers is not None:
            config.MAX_ACTIVE_TICKERS = int(leg.max_active_tickers)
        if leg.concentration_guard is not None:
            config.CONCENTRATION_GUARD_ENABLED = bool(leg.concentration_guard)
        if leg.stat_arb_cap is not None:
            config.STAT_ARB_SLEEVE_CAP_ENABLED = bool(leg.stat_arb_cap)
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
        "return_pct": None,
        "sharpe": None,
        "max_dd_pct": None,
        "max_dd_abs": None,
        "within_budget": None,
        "trade_count": None,
        "nyse_fills": None,
        "avg_active_exposure_pct": None,
        "leg": asdict(leg),
        "notes": leg.notes,
        "error": None,
    }
    if not isinstance(result, dict):
        row["error"] = "missing_result"
        return row
    ret = _safe_float(result.get("total_return_pct"))
    sharpe = _safe_float(result.get("sharpe"))
    max_dd = _safe_float(result.get("max_drawdown_pct"))
    if ret is None or sharpe is None or max_dd is None:
        row["error"] = "metrics_parse_failed"
        return row
    dd_abs = abs(float(max_dd))
    row.update(
        {
            "ok": bool(result.get("ok", True)),
            "return_pct": ret,
            "sharpe": sharpe,
            "max_dd_pct": max_dd,
            "max_dd_abs": dd_abs,
            "within_budget": dd_abs <= MAX_DD_BUDGET * 100.0 + 0.05,
            "trade_count": _safe_int(result.get("total_orders")) or 0,
            "nyse_fills": _safe_int(result.get("nyse_signals")) or 0,
            "avg_active_exposure_pct": _safe_float(result.get("avg_active_exposure_pct")),
            "final_equity": _safe_float(result.get("final_equity")),
            "halt_events": _safe_int(result.get("halt_events")),
        }
    )
    return row


def _fmt_pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.2f}%"


def _fmt_num(v: float | None, d: int = 2) -> str:
    return "n/a" if v is None else f"{v:.{d}f}"


def _pick_winner(rows: list[dict]) -> dict | None:
    ok = [r for r in rows if r.get("ok") and r.get("within_budget")]
    if not ok:
        return None
    return max(
        ok,
        key=lambda r: (
            float(r["return_pct"]),
            float(r["sharpe"] or -999),
            float(r.get("max_dd_abs") or 0),
        ),
    )


def _write(rows: list[dict], *, days: int, window: str, bench: float | None, verdict: str) -> None:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    winner = _pick_winner(rows)
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
        "# STRICT MaxDD 25% — concentration / sleeve caps",
        "",
        f"Generated: {generated}",
        f"Window: {window} ({days}d)",
        f"Benchmark VTI B&H: {_fmt_pct(bench)}",
        "",
        f"**{DISCLAIMER}**",
        "",
        "| Leg | Return | Sharpe | MaxDD | Active% | In 25% | NYSE | Notes |",
        "|-----|--------|--------|-------|---------|--------|------|-------|",
    ]
    for r in rows:
        mark = " **" if winner and r["name"] == winner["name"] else ""
        bud = "YES" if r.get("within_budget") else "NO"
        lines.append(
            f"| {r['name']}{mark} "
            f"| {_fmt_pct(r.get('return_pct'))} "
            f"| {_fmt_num(r.get('sharpe'))} "
            f"| {_fmt_pct(r.get('max_dd_pct'))} "
            f"| {_fmt_num(r.get('avg_active_exposure_pct'), 1)} "
            f"| {bud} "
            f"| {r.get('nyse_fills')} "
            f"| {r.get('notes') or ''} |"
        )
    lines.extend(["", "## Verdict", "", verdict, ""])
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


def _verdict(rows: list[dict], winner: dict | None, base: dict | None) -> str:
    if not winner:
        return "HOLD - no leg within 25% MaxDD budget. " + DISCLAIMER
    parts = [
        f"Best: {winner['name']} return {_fmt_pct(winner.get('return_pct'))} "
        f"Sharpe {_fmt_num(winner.get('sharpe'))} MaxDD {_fmt_pct(winner.get('max_dd_pct'))} "
        f"active {_fmt_num(winner.get('avg_active_exposure_pct'), 1)}."
    ]
    if base and base.get("ok"):
        d = float(winner["return_pct"]) - float(base["return_pct"])
        dd = float(winner.get("max_dd_abs") or 0) - float(base.get("max_dd_abs") or 0)
        parts.append(f"vs winner_base: {d:+.2f}pp return, MaxDD {dd:+.2f}pp deeper.")
    max_dd = max(float(r.get("max_dd_abs") or 0) for r in rows if r.get("ok"))
    if max_dd < 15:
        parts.append(
            f"Still only ~{max_dd:.1f}% realized MaxDD — strategy may be structurally "
            "low-vol; true 25% DD may need leverage or a different regime window."
        )
    parts.append(DISCLAIMER)
    return " ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--legs", default="", help="Comma subset of leg names")
    args = ap.parse_args()
    days = max(20, int(args.days))
    wanted = {x.strip() for x in args.legs.split(",") if x.strip()}
    legs = [L for L in LEGS if not wanted or L.name in wanted]
    if not legs:
        raise SystemExit(f"No legs. Choose from: {[L.name for L in LEGS]}")

    print(f"--- STRICT concentration ladder ({days}d, MaxDD {MAX_DD_BUDGET:.0%}) ---")
    data = _ensure_daily_data(days, refresh=args.refresh, use_max=False)
    warmup = min(MIN_HISTORY, max(0, len(data) - 5))
    window = f"{data.index[warmup].date()} -> {data.index[-1].date()}"
    bench = _benchmark_return(data, warmup)
    print(f"Window: {window}")
    if bench is not None:
        print(f"VTI B&H: {bench:+.2f}%")

    rows: list[dict] = []
    for leg in legs:
        print(f"\n>>> {leg.name}: {leg.notes}")
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
                strict_pit=True,
                paper_nyse_entry_hygiene=True,
                paper_crypto_enabled=False,
                verbose=False,
            )
        row = _extract(raw, leg)
        rows.append(row)
        print(
            f"    ret {_fmt_pct(row.get('return_pct'))} "
            f"Sharpe {_fmt_num(row.get('sharpe'))} "
            f"MaxDD {_fmt_pct(row.get('max_dd_pct'))} "
            f"active {_fmt_num(row.get('avg_active_exposure_pct'), 1)}"
        )

    base = next((r for r in rows if r["name"] == "winner_base"), None)
    winner = _pick_winner(rows)
    verdict = _verdict(rows, winner, base)
    _write(rows, days=days, window=window, bench=bench, verdict=verdict)
    print("\n## Verdict")
    print(verdict)
    print(f"\nWrote {OUT_MD.name}")
    return 0 if all(r.get("ok") for r in rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
