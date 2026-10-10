"""STRICT conviction book: 0% VTI + top-N NYSE ranks vs diversified baseline.

Paper/research only. Hygiene ON. Overlays OFF (STRICT PIT).

Legs:
  baseline_strict   — current STRICT paper stack (dynamic VTI ~40%+ floor)
  zero_vti          — fixed 0% VTI, otherwise same
  conviction_top5   — 0% VTI, top 5 momentum only, max 5 actives, fat NYSE/name
  conviction_top3   — same with top 3 / max 3

Usage (from stock-bot/):
  python scripts/analysis/eval_strict_conviction.py --days 90
  python scripts/analysis/eval_strict_conviction.py --days 365

Writes:
  scripts/analysis/eval_strict_conviction_last.md
  scripts/analysis/eval_strict_conviction_last.json
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

OUT_MD = Path(__file__).with_name("eval_strict_conviction_last.md")
OUT_JSON = Path(__file__).with_name("eval_strict_conviction_last.json")
DISCLAIMER = (
    "STRICT research only; conviction book is not live Profile A"
)


@dataclass(frozen=True)
class Leg:
    name: str
    notes: str
    dynamic_vti: bool
    vti_fixed: float
    top_n: int  # 0 = no truncate
    max_active: int
    max_equity_trades: int
    nyse_cap: float
    nyse_max_exp: float
    max_position_pct: float
    boost: float
    risk: float
    concentration_guard: bool
    spy_disabled: bool = False


LEGS: list[Leg] = [
    Leg(
        "baseline_strict",
        "STRICT defaults (dynamic VTI ballast)",
        dynamic_vti=True,
        vti_fixed=0.40,
        top_n=0,
        max_active=25,
        max_equity_trades=3,
        nyse_cap=0.20,
        nyse_max_exp=0.22,
        max_position_pct=0.08,
        boost=1.40,
        risk=0.018,
        concentration_guard=True,
    ),
    Leg(
        "zero_vti",
        "fixed 0% VTI; still multi-name sleeve",
        dynamic_vti=False,
        vti_fixed=0.0,
        top_n=0,
        max_active=25,
        max_equity_trades=3,
        nyse_cap=0.20,
        nyse_max_exp=0.22,
        max_position_pct=0.08,
        boost=1.40,
        risk=0.018,
        concentration_guard=True,
    ),
    Leg(
        "conviction_top5",
        "0% VTI + top 5 ranks, max 5 names, fat size",
        dynamic_vti=False,
        vti_fixed=0.0,
        top_n=5,
        max_active=5,
        max_equity_trades=5,
        nyse_cap=0.55,
        nyse_max_exp=0.60,
        max_position_pct=0.20,
        boost=2.50,
        risk=0.040,
        concentration_guard=False,
    ),
    Leg(
        "conviction_top3",
        "0% VTI + top 3 ranks, max 3 names, fatter size",
        dynamic_vti=False,
        vti_fixed=0.0,
        top_n=3,
        max_active=3,
        max_equity_trades=3,
        nyse_cap=0.65,
        nyse_max_exp=0.70,
        max_position_pct=0.30,
        boost=3.00,
        risk=0.050,
        concentration_guard=False,
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
        "PAPER_DYNAMIC_VTI_ENABLED",
        "DYNAMIC_VTI_PAPER_FLOOR",
        "DYNAMIC_VTI_PAPER_CEILING",
        "DYNAMIC_VTI_FLOOR_MIN",
        "DYNAMIC_VTI_ALLOW_ZERO",
        "DYNAMIC_VTI_OPTIONAL_ENABLED",
        "PAPER_VTI_CORE_PCT",
        "PAPER_NYSE_TOP_N",
        "MAX_ACTIVE_TICKERS",
        "PAPER_MAX_EQUITY_TRADES",
        "NYSE_SLEEVE_CAP_PCT",
        "PAPER_NYSE_SLEEVE_CAP_PCT",
        "PAPER_NYSE_MAX_EXPOSURE_PCT",
        "PAPER_MAX_POSITION_PCT",
        "PER_NAME_MAX_PCT",
        "PAPER_ACTIVE_SLEEVE_BOOST",
        "PAPER_RISK_PER_TRADE",
        "PAPER_RISK_CALM_BULL_PCT",
        "RISK_PER_TRADE",
        "CONCENTRATION_GUARD_ENABLED",
        "SPY_SLEEVE_CAP_PCT",
        "STRICT_PIT_BACKTEST",
    ]
    saved = {k: getattr(config, k) for k in keys}
    saved["thinking"] = config.PAPER_THINKING_ENGINE_ENABLED
    saved["strict_ctx"] = config.backtest_strict_pit_context()
    saved["strict_allow"] = set(config.strict_pit_allow())
    try:
        config.PAPER_DYNAMIC_VTI_ENABLED = bool(leg.dynamic_vti)
        if leg.dynamic_vti:
            config.DYNAMIC_VTI_PAPER_FLOOR = 0.40
            config.DYNAMIC_VTI_PAPER_CEILING = 0.75
            config.DYNAMIC_VTI_FLOOR_MIN = 0.40
            config.DYNAMIC_VTI_ALLOW_ZERO = False
            config.DYNAMIC_VTI_OPTIONAL_ENABLED = False
        else:
            config.DYNAMIC_VTI_PAPER_FLOOR = float(leg.vti_fixed)
            config.DYNAMIC_VTI_PAPER_CEILING = float(leg.vti_fixed)
            config.DYNAMIC_VTI_FLOOR_MIN = float(leg.vti_fixed)
            config.DYNAMIC_VTI_ALLOW_ZERO = leg.vti_fixed <= 0
            config.DYNAMIC_VTI_OPTIONAL_ENABLED = leg.vti_fixed <= 0
        config.PAPER_VTI_CORE_PCT = float(leg.vti_fixed)
        config.PAPER_NYSE_TOP_N = int(leg.top_n)
        config.MAX_ACTIVE_TICKERS = int(leg.max_active)
        config.PAPER_MAX_EQUITY_TRADES = int(leg.max_equity_trades)
        config.NYSE_SLEEVE_CAP_PCT = float(leg.nyse_cap)
        config.PAPER_NYSE_SLEEVE_CAP_PCT = float(leg.nyse_cap)
        config.PAPER_NYSE_MAX_EXPOSURE_PCT = float(leg.nyse_max_exp)
        config.PAPER_MAX_POSITION_PCT = float(leg.max_position_pct)
        config.PER_NAME_MAX_PCT = float(leg.max_position_pct)
        config.PAPER_ACTIVE_SLEEVE_BOOST = float(leg.boost)
        config.PAPER_RISK_PER_TRADE = float(leg.risk)
        config.PAPER_RISK_CALM_BULL_PCT = float(leg.risk)
        config.RISK_PER_TRADE = float(leg.risk)
        config.CONCENTRATION_GUARD_ENABLED = bool(leg.concentration_guard)
        if leg.spy_disabled:
            config.SPY_SLEEVE_CAP_PCT = 0.0
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
        "trade_count": None,
        "nyse_fills": None,
        "avg_vti_core": None,
        "avg_active_exposure_pct": None,
        "nyse_pick_counts": {},
        "unique_nyse_picks": None,
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
    picks = result.get("nyse_pick_counts") or {}
    row.update(
        {
            "ok": bool(result.get("ok", True)),
            "return_pct": ret,
            "sharpe": sharpe,
            "max_dd_pct": max_dd,
            "trade_count": _safe_int(result.get("total_orders")) or 0,
            "nyse_fills": _safe_int(result.get("nyse_signals")) or 0,
            "avg_vti_core": _safe_float(result.get("vti_core_pct")),
            "avg_active_exposure_pct": _safe_float(result.get("avg_active_exposure_pct")),
            "nyse_pick_counts": dict(list(picks.items())[:12]) if isinstance(picks, dict) else {},
            "unique_nyse_picks": len(picks) if isinstance(picks, dict) else None,
            "final_equity": _safe_float(result.get("final_equity")),
        }
    )
    return row


def _fmt_pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.2f}%"


def _fmt_num(v: float | None, d: int = 2) -> str:
    return "n/a" if v is None else f"{v:.{d}f}"


def _pick_winner(rows: list[dict]) -> dict | None:
    ok = [r for r in rows if r.get("ok") and r.get("return_pct") is not None]
    if not ok:
        return None
    return max(ok, key=lambda r: (float(r["return_pct"]), float(r["sharpe"] or -999)))


def _verdict(rows: list[dict], base: dict | None, winner: dict | None) -> str:
    if not winner:
        return "HOLD - no successful legs. " + DISCLAIMER
    parts = [
        f"Best: {winner['name']} return {_fmt_pct(winner.get('return_pct'))} "
        f"Sharpe {_fmt_num(winner.get('sharpe'))} MaxDD {_fmt_pct(winner.get('max_dd_pct'))} "
        f"avg VTI {_fmt_num(winner.get('avg_vti_core'), 2)} "
        f"unique NYSE {winner.get('unique_nyse_picks')}."
    ]
    if base and base.get("ok"):
        d = float(winner["return_pct"]) - float(base["return_pct"])
        parts.append(f"vs baseline_strict: {d:+.2f}pp.")
    conv = [r for r in rows if r["name"].startswith("conviction") and r.get("ok")]
    if conv and base and base.get("ok"):
        best_c = max(conv, key=lambda r: float(r["return_pct"]))
        if float(best_c["return_pct"]) > float(base["return_pct"]) + 0.5:
            parts.append(
                "Conviction beat diversified baseline on this window — queue 365d."
            )
        else:
            parts.append(
                "Conviction did not clearly beat baseline — top-N ranks may lack "
                "predictive edge; do not promote yet."
            )
    parts.append(DISCLAIMER)
    return " ".join(parts)


def _write(rows, *, days, window, bench, verdict) -> None:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    winner = _pick_winner(rows)
    payload = {
        "generated_at": generated,
        "days": days,
        "window": window,
        "benchmark_return_pct": bench,
        "disclaimer": DISCLAIMER,
        "legs": rows,
        "winner": winner["name"] if winner else None,
        "verdict": verdict,
        "ok": all(bool(r.get("ok")) for r in rows),
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    lines = [
        "# STRICT conviction book (0% VTI + top-N)",
        "",
        f"Generated: {generated}",
        f"Window: {window} ({days}d)",
        f"Benchmark VTI B&H: {_fmt_pct(bench)}",
        "",
        f"**{DISCLAIMER}**",
        "",
        "| Leg | Return | Sharpe | MaxDD | Avg VTI | Active% | Unique NYSE | Notes |",
        "|-----|--------|--------|-------|---------|---------|-------------|-------|",
    ]
    for r in rows:
        mark = " **" if winner and r["name"] == winner["name"] else ""
        lines.append(
            f"| {r['name']}{mark} "
            f"| {_fmt_pct(r.get('return_pct'))} "
            f"| {_fmt_num(r.get('sharpe'))} "
            f"| {_fmt_pct(r.get('max_dd_pct'))} "
            f"| {_fmt_num(r.get('avg_vti_core'), 2)} "
            f"| {_fmt_num(r.get('avg_active_exposure_pct'), 1)} "
            f"| {r.get('unique_nyse_picks')} "
            f"| {r.get('notes') or ''} |"
        )
    lines.extend(["", "## Verdict", "", verdict, ""])
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="STRICT 0% VTI + top-N conviction A/B")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--legs", default="", help="Comma subset of leg names")
    args = ap.parse_args()
    days = max(20, int(args.days))
    wanted = {x.strip() for x in args.legs.split(",") if x.strip()}
    legs = [L for L in LEGS if not wanted or L.name in wanted]
    if not legs:
        raise SystemExit(f"Choose from: {[L.name for L in LEGS]}")

    print(f"--- STRICT conviction A/B ({days}d) ---")
    print("Legs:", ", ".join(L.name for L in legs))
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
        print(
            f"    VTI={leg.vti_fixed:.0%} dyn={leg.dynamic_vti} "
            f"top_n={leg.top_n or 'all'} max_active={leg.max_active}"
        )
        with _apply_leg(leg):
            raw = run_backtest(
                data,
                track_active_exposure=True,
                track_metrics=True,
                paper_aggressive=True,
                paper_sleeve_features=True,
                paper_dynamic_vti=leg.dynamic_vti,
                vti_core_pct=0.0 if leg.dynamic_vti else float(leg.vti_fixed),
                paper_thinking=False,
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
            f"avgVTI {_fmt_num(row.get('avg_vti_core'), 2)} "
            f"uniqueNYSE {row.get('unique_nyse_picks')}"
        )

    base = next((r for r in rows if r["name"] == "baseline_strict"), None)
    winner = _pick_winner(rows)
    verdict = _verdict(rows, base, winner)
    _write(rows, days=days, window=window, bench=bench, verdict=verdict)
    print("\n## Verdict")
    print(verdict)
    print(f"\nWrote {OUT_MD.name}")
    return 0 if all(r.get("ok") for r in rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
