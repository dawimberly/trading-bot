"""STRICT Live Conservative Level-2 A/B: SPY sleeve vs NYSE room (beat VTI).

Live-shaped research only. Does not change live Profile A / portal .env.

Legs:
  baseline — 85% VTI + 5% SPY (current live default)
  level2   — 80% VTI + 15% NYSE momentum (active sleeve routed to NYSE)

Promote gate (90d): level2 excess vs VTI B&H > 0, MaxDD within 1pp of baseline,
and at least one NYSE fill. Queue 365d before any live .env change.

Usage (from stock-bot/):
  python scripts/analysis/eval_live_nyse_ab.py --days 90
  python scripts/analysis/eval_live_nyse_ab.py --days 365

Writes:
  scripts/analysis/live_nyse_ab_{days}_last.md
  scripts/analysis/live_nyse_ab_{days}_last.json
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

DISCLAIMER = (
    "STRICT live-shaped research only; do not change live .env until 365d confirms "
    "and owner explicitly promotes"
)
MAX_DD_WORSE_PP = 1.0
RESEARCH_START_EQUITY = float(os.getenv("LIVE_NYSE_AB_EQUITY", "10000"))
RESEARCH_MAX_NOTIONAL = float(os.getenv("LIVE_NYSE_AB_MAX_NOTIONAL", "500"))


def _out_paths(days: int) -> tuple[Path, Path]:
    base = Path(__file__).with_name(f"live_nyse_ab_{int(days)}_last")
    return base.with_suffix(".md"), base.with_suffix(".json")


@dataclass(frozen=True)
class Leg:
    name: str
    config_label: str
    notes: str
    active_sleeve: str  # spy | nyse | cash
    vti_pct: float
    active_pct: float


LEGS: list[Leg] = [
    Leg(
        "baseline",
        "Live Cons: 85% VTI + 5% SPY",
        "current live default",
        active_sleeve="spy",
        vti_pct=0.85,
        active_pct=0.05,
    ),
    Leg(
        "level2",
        "Level 2: 80% VTI + 15% NYSE",
        "beat-VTI path — active sleeve routed to NYSE",
        active_sleeve="nyse",
        vti_pct=0.80,
        active_pct=0.15,
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
        "LIVE_ACTIVE_SLEEVE_CHOICE",
        "LIVE_VTI_CORE_PCT",
        "LIVE_SMALL_ACTIVE_SLEEVE_PCT",
        "SMALL_ACCOUNT_VTI_CORE_PCT",
        "SPY_SLEEVE_CAP_PCT",
        "PAPER_SPY_MAX_EXPOSURE_PCT",
        "SMALL_ACCOUNT_BACKTEST_EQUITY",
        "SMALL_ACCOUNT_MAX_NOTIONAL",
        "STRICT_PIT_BACKTEST",
    ]
    saved = {k: getattr(config, k) for k in keys}
    saved["strict_ctx"] = config.backtest_strict_pit_context()
    saved["strict_allow"] = set(config.strict_pit_allow())
    saved["paper_ctx"] = config.paper_aggressive_context()
    saved["live_ctx"] = config.backtest_live_conservative_context()
    saved["small_ctx"] = config.backtest_small_account_context()
    try:
        config.set_paper_aggressive_context(False)
        config.set_backtest_paper_sleeves_context(False)
        config.set_backtest_small_account_context(True)
        config.set_backtest_live_conservative_context(True)
        config.SMALL_ACCOUNT_BACKTEST_EQUITY = float(RESEARCH_START_EQUITY)
        config.SMALL_ACCOUNT_MAX_NOTIONAL = float(RESEARCH_MAX_NOTIONAL)
        config.LIVE_ACTIVE_SLEEVE_CHOICE = str(leg.active_sleeve)
        config.LIVE_VTI_CORE_PCT = float(leg.vti_pct)
        config.LIVE_SMALL_ACTIVE_SLEEVE_PCT = float(leg.active_pct)
        config.SMALL_ACCOUNT_VTI_CORE_PCT = float(leg.vti_pct)
        config.SPY_SLEEVE_CAP_PCT = 0.45 if leg.active_sleeve == "spy" else 0.0
        config.PAPER_SPY_MAX_EXPOSURE_PCT = 0.0
        config.STRICT_PIT_BACKTEST = True
        RUN_OPTIONS.strict_pit = True
        RUN_OPTIONS.no_thinking = True
        config.apply_strict_pit_kill_switches(allow=None)
        config.enforce_live_conservative_profile()
        # Re-apply after enforce (env may not be set in process).
        config.LIVE_ACTIVE_SLEEVE_CHOICE = str(leg.active_sleeve)
        config.LIVE_VTI_CORE_PCT = float(leg.vti_pct)
        config.LIVE_SMALL_ACTIVE_SLEEVE_PCT = float(leg.active_pct)
        config.SMALL_ACCOUNT_VTI_CORE_PCT = float(leg.vti_pct)
        config.SPY_SLEEVE_CAP_PCT = 0.45 if leg.active_sleeve == "spy" else 0.0
        config.SMALL_ACCOUNT_BACKTEST_EQUITY = float(RESEARCH_START_EQUITY)
        config.SMALL_ACCOUNT_MAX_NOTIONAL = float(RESEARCH_MAX_NOTIONAL)
        yield
    finally:
        for k, v in saved.items():
            if k == "strict_ctx":
                config.set_backtest_strict_pit_context(v)
            elif k == "strict_allow":
                config.set_strict_pit_allow(v)
            elif k == "paper_ctx":
                config.set_paper_aggressive_context(v)
            elif k == "live_ctx":
                config.set_backtest_live_conservative_context(v)
            elif k == "small_ctx":
                config.set_backtest_small_account_context(v)
            else:
                setattr(config, k, v)
        RUN_OPTIONS.strict_pit = False
        RUN_OPTIONS.no_thinking = False


def _extract(result: dict | None, leg: Leg, *, strict_banner: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": leg.name,
        "config": leg.config_label,
        "notes": leg.notes,
        "strict_pit_banner": strict_banner,
        "ok": False,
        "return_pct": None,
        "sharpe": None,
        "max_dd_pct": None,
        "trade_count": None,
        "spy_fills": None,
        "nyse_fills": None,
        "excess_vs_vti_pp": None,
        "leg": asdict(leg),
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
    row.update(
        {
            "ok": bool(result.get("ok", True)),
            "return_pct": ret,
            "sharpe": sharpe,
            "max_dd_pct": max_dd,
            "trade_count": _safe_int(result.get("total_orders")) or 0,
            "spy_fills": _safe_int(result.get("spy_signals")) or 0,
            "nyse_fills": _safe_int(result.get("nyse_signals")) or 0,
            "strict_pit": bool(result.get("strict_pit")),
            "live_banner": config.format_live_conservative_banner(),
        }
    )
    return row


def _fmt_pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.2f}%"


def _fmt_num(v: float | None, d: int = 2) -> str:
    return "n/a" if v is None else f"{v:.{d}f}"


def _delta(row: dict, base: dict | None, key: str) -> str:
    if not base or not base.get("ok") or not row.get("ok"):
        return "--"
    a, b = row.get(key), base.get(key)
    if a is None or b is None:
        return "--"
    if key.endswith("_pct") or key in ("return_pct", "excess_vs_vti_pp"):
        return f"{float(a) - float(b):+.2f}pp"
    return f"{float(a) - float(b):+.2f}"


def _promote_ok(row: dict, base: dict, *, bench: float | None, days: int) -> bool:
    if not base.get("ok") or not row.get("ok") or row.get("name") != "level2":
        return False
    if bench is None:
        return False
    excess = float(row["return_pct"]) - float(bench)
    row["excess_vs_vti_pp"] = excess
    beat_vti = excess > 0.0
    nyse_used = int(row.get("nyse_fills") or 0) > 0
    dd_row = float(row.get("max_dd_pct") or 0)
    dd_base = float(base.get("max_dd_pct") or 0)
    dd_ok = dd_row >= (dd_base - MAX_DD_WORSE_PP)
    # Primary mission: beat VTI with NYSE actually firing; DD not blown vs baseline.
    return beat_vti and nyse_used and dd_ok


def _verdict(
    rows: list[dict], base: dict | None, *, days: int, bench: float | None
) -> str:
    ok = [r for r in rows if r.get("ok")]
    if not ok or not base or not base.get("ok"):
        return f"HOLD — incomplete results. {DISCLAIMER}"
    for r in ok:
        if bench is not None and r.get("return_pct") is not None:
            r["excess_vs_vti_pp"] = float(r["return_pct"]) - float(bench)
    lvl = next((r for r in ok if r["name"] == "level2"), None)
    parts: list[str] = []
    if lvl:
        parts.append(
            f"level2 {_fmt_pct(lvl.get('return_pct'))} excess {_fmt_num(lvl.get('excess_vs_vti_pp'))}pp "
            f"Sharpe {_fmt_num(lvl.get('sharpe'))} MaxDD {_fmt_pct(lvl.get('max_dd_pct'))} "
            f"NYSE fills {lvl.get('nyse_fills')} vs baseline {_fmt_pct(base.get('return_pct'))} "
            f"excess {_fmt_num(base.get('excess_vs_vti_pp'))}pp "
            f"({_delta(lvl, base, 'return_pct')} ret)."
        )
        if _promote_ok(lvl, base, bench=bench, days=days):
            if days >= 300:
                parts.append(
                    "LIVE DEFAULT CANDIDATE (365d): level2 beat VTI with NYSE fills "
                    f"and MaxDD within {MAX_DD_WORSE_PP:.1f}pp of baseline — owner promote only."
                )
            else:
                parts.append(
                    "90d gate PASSED (beat VTI + NYSE used + DD ok). "
                    "Queue 365d STRICT confirm before any live .env change."
                )
        else:
            parts.append(
                "level2 did not clear beat-VTI + NYSE-fills + MaxDD gate — "
                "keep live on baseline (85/5 SPY); do not promote."
            )
    parts.append(DISCLAIMER)
    return " ".join(parts)


def _write(
    rows,
    *,
    days,
    window,
    bench,
    verdict,
    strict_banner,
    out_md: Path,
    out_json: Path,
) -> None:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    base = next((r for r in rows if r["name"] == "baseline"), None)
    payload = {
        "generated_at": generated,
        "days": days,
        "window": window,
        "benchmark_return_pct": bench,
        "profile": "live_conservative_level2_nyse",
        "strict_pit_banner": strict_banner,
        "disclaimer": DISCLAIMER,
        "promote_rule": {
            "beat_vti_excess_pp_gt": 0.0,
            "nyse_fills_gt": 0,
            "max_dd_worse_pp_max": MAX_DD_WORSE_PP,
            "require_365d_before_live": True,
        },
        "legs": rows,
        "verdict": verdict,
        "ok": all(bool(r.get("ok")) for r in rows),
    }
    out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    lines = [
        f"# STRICT Live Conservative Level-2 NYSE A/B ({days}d)",
        "",
        f"Generated: {generated}",
        f"Window: {window} ({days}d)",
        f"Benchmark VTI B&H: {_fmt_pct(bench)}",
        f"Research sizing: ${RESEARCH_START_EQUITY:,.0f} start / "
        f"${RESEARCH_MAX_NOTIONAL:,.0f} max order",
        "",
        f"**{strict_banner}**",
        "",
        f"**{DISCLAIMER}**",
        "",
        "| Leg | Config | Return | vs VTI | Sharpe | MaxDD | Trades | SPY | NYSE | vs base ret |",
        "|-----|--------|--------|--------|--------|-------|--------|-----|------|-------------|",
    ]
    for r in rows:
        lines.append(
            f"| {r['name']} "
            f"| {r.get('config', '')} "
            f"| {_fmt_pct(r.get('return_pct'))} "
            f"| {_fmt_num(r.get('excess_vs_vti_pp'))}pp "
            f"| {_fmt_num(r.get('sharpe'))} "
            f"| {_fmt_pct(r.get('max_dd_pct'))} "
            f"| {r.get('trade_count')} "
            f"| {r.get('spy_fills')} "
            f"| {r.get('nyse_fills')} "
            f"| {_delta(r, base, 'return_pct')} |"
        )
    lines.extend(["", "## Verdict", "", verdict, ""])
    out_md.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="STRICT Live Conservative Level-2 NYSE A/B (beat VTI)"
    )
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--legs", default="", help="Comma subset: baseline,level2")
    args = ap.parse_args()
    days = max(20, int(args.days))
    out_md, out_json = _out_paths(days)
    wanted = {x.strip() for x in args.legs.split(",") if x.strip()}
    legs = [L for L in LEGS if not wanted or L.name in wanted]
    if not legs:
        raise SystemExit(f"Choose from: {[L.name for L in LEGS]}")

    print(f"--- STRICT Live Conservative Level-2 NYSE A/B ({days}d) ---")
    print(f"Legs ({len(legs)}):", ", ".join(L.name for L in legs))
    print(
        f"Research sizing: start ${RESEARCH_START_EQUITY:,.0f} | "
        f"max/order ${RESEARCH_MAX_NOTIONAL:,.0f}"
    )
    print(f"Output: {out_md.name}")
    data = _ensure_daily_data(days, refresh=args.refresh, use_max=False)
    warmup = min(MIN_HISTORY, max(0, len(data) - 5))
    window = f"{data.index[warmup].date()} -> {data.index[-1].date()}"
    bench = _benchmark_return(data, warmup)
    print(f"Window: {window}")
    if bench is not None:
        print(f"VTI B&H: {bench:+.2f}%")

    rows: list[dict] = []
    strict_banner = ""
    for leg in legs:
        print(f"\n>>> {leg.name}: {leg.config_label}", flush=True)
        with _apply_leg(leg):
            strict_banner = config.format_strict_pit_banner() or (
                "STRICT PIT: ON | insider/RVOL/catalyst/news/LLM/dyn_univ/buffett-fallback off"
            )
            print(f"    {strict_banner}", flush=True)
            print(f"    {config.format_live_conservative_banner()}", flush=True)
            print(
                f"    NYSE boost check: "
                f"{config._live_conservative_sleeve_boost('nyse'):.2%} "
                f"(choice={config.LIVE_ACTIVE_SLEEVE_CHOICE})",
                flush=True,
            )
            raw = run_backtest(
                data,
                track_active_exposure=True,
                track_metrics=True,
                paper_aggressive=False,
                small_account=True,
                vti_core_pct=float(leg.vti_pct),
                paper_thinking=False,
                strict_pit=True,
                paper_crypto_enabled=False,
                live_thinking_start_equity=float(RESEARCH_START_EQUITY),
                verbose=False,
            )
        row = _extract(raw, leg, strict_banner=strict_banner)
        if bench is not None and row.get("return_pct") is not None:
            row["excess_vs_vti_pp"] = float(row["return_pct"]) - float(bench)
        rows.append(row)
        print(
            f"    -> ret {_fmt_pct(row.get('return_pct'))} "
            f"vsVTI {_fmt_num(row.get('excess_vs_vti_pp'))}pp "
            f"Sharpe {_fmt_num(row.get('sharpe'))} "
            f"MaxDD {_fmt_pct(row.get('max_dd_pct'))} "
            f"trades {row.get('trade_count')} "
            f"SPY {row.get('spy_fills')} NYSE {row.get('nyse_fills')}",
            flush=True,
        )
        base = next((r for r in rows if r["name"] == "baseline"), None)
        _write(
            rows,
            days=days,
            window=window,
            bench=bench,
            verdict=_verdict(rows, base, days=days, bench=bench),
            strict_banner=strict_banner,
            out_md=out_md,
            out_json=out_json,
        )

    base = next((r for r in rows if r["name"] == "baseline"), None)
    verdict = _verdict(rows, base, days=days, bench=bench)
    _write(
        rows,
        days=days,
        window=window,
        bench=bench,
        verdict=verdict,
        strict_banner=strict_banner,
        out_md=out_md,
        out_json=out_json,
    )
    print("\n## Verdict")
    print(verdict)
    print(f"\nWrote {out_md.name}")
    return 0 if all(r.get("ok") for r in rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
