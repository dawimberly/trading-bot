"""STRICT Live Conservative SPY on/off A/B.

Live-shaped research only (high VTI + small active sleeve). No live Profile A changes.

Legs:
  spy_on  — Live Conservative defaults (LIVE_ACTIVE_SLEEVE_CHOICE=spy)
  spy_off — SPY satellite disabled (choice=cash + SPY_SLEEVE_CAP_PCT=0)

Usage (from stock-bot/):
  python scripts/analysis/eval_live_spy_ab.py --days 90
  python scripts/analysis/eval_live_spy_ab.py --days 365

Writes:
  scripts/analysis/live_spy_ab_{days}_last.md
  scripts/analysis/live_spy_ab_{days}_last.json
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
    "STRICT live-shaped research only; do not change live Profile A until 365d confirms"
)
MAX_DD_WORSE_PP = 1.0
# $100 + $10 max/order cannot fill a 5% SPY sleeve (below min notional).
# Keep live *ratios*; use research equity so SPY on/off is actually exercised.
RESEARCH_START_EQUITY = float(os.getenv("LIVE_SPY_AB_EQUITY", "10000"))
RESEARCH_MAX_NOTIONAL = float(os.getenv("LIVE_SPY_AB_MAX_NOTIONAL", "500"))


def _out_paths(days: int) -> tuple[Path, Path]:
    base = Path(__file__).with_name(f"live_spy_ab_{int(days)}_last")
    return base.with_suffix(".md"), base.with_suffix(".json")


@dataclass(frozen=True)
class Leg:
    name: str
    config_label: str
    notes: str
    active_sleeve: str  # spy | cash
    spy_cap: float | None  # None = leave module default until enforce


LEGS: list[Leg] = [
    Leg(
        "spy_on",
        "Live Conservative: 85% VTI + 5% SPY trend",
        "current live default (SPY active sleeve ON)",
        active_sleeve="spy",
        spy_cap=None,
    ),
    Leg(
        "spy_off",
        "Live Conservative: 85% VTI + 5% cash (SPY cap=0)",
        "SPY satellite OFF; cash buffer instead of SPY trend",
        active_sleeve="cash",
        spy_cap=0.0,
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
        # Live-shaped: never paper-aggressive (would re-lock paper SPY-off).
        config.set_paper_aggressive_context(False)
        config.set_backtest_paper_sleeves_context(False)
        config.set_backtest_small_account_context(True)
        config.set_backtest_live_conservative_context(True)
        config.SMALL_ACCOUNT_BACKTEST_EQUITY = float(RESEARCH_START_EQUITY)
        config.SMALL_ACCOUNT_MAX_NOTIONAL = float(RESEARCH_MAX_NOTIONAL)
        config.LIVE_ACTIVE_SLEEVE_CHOICE = str(leg.active_sleeve)
        if leg.spy_cap is not None:
            config.SPY_SLEEVE_CAP_PCT = float(leg.spy_cap)
        else:
            config.SPY_SLEEVE_CAP_PCT = 0.45
        # Keep paper hard-cap irrelevant on live path.
        config.PAPER_SPY_MAX_EXPOSURE_PCT = 0.0
        config.STRICT_PIT_BACKTEST = True
        RUN_OPTIONS.strict_pit = True
        RUN_OPTIONS.no_thinking = True
        config.apply_strict_pit_kill_switches(allow=None)
        config.enforce_live_conservative_profile()
        # Re-apply sleeve choice after enforce (enforce may reset choice if env not set).
        config.LIVE_ACTIVE_SLEEVE_CHOICE = str(leg.active_sleeve)
        if leg.spy_cap is not None:
            config.SPY_SLEEVE_CAP_PCT = float(leg.spy_cap)
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
    if key.endswith("_pct") or key == "return_pct":
        return f"{float(a) - float(b):+.2f}pp"
    return f"{float(a) - float(b):+.2f}"


def _promote_ok(row: dict, base: dict) -> bool:
    if not base.get("ok") or not row.get("ok") or row.get("name") == "spy_on":
        return False
    ret_ok = float(row["return_pct"]) > float(base["return_pct"])
    sh_ok = float(row.get("sharpe") or -999) > float(base.get("sharpe") or -999)
    dd_row = float(row.get("max_dd_pct") or 0)
    dd_base = float(base.get("max_dd_pct") or 0)
    dd_ok = dd_row >= (dd_base - MAX_DD_WORSE_PP)
    return ret_ok and sh_ok and dd_ok


def _verdict(rows: list[dict], base: dict | None, *, days: int) -> str:
    ok = [r for r in rows if r.get("ok")]
    if not ok or not base or not base.get("ok"):
        return f"HOLD — incomplete results. {DISCLAIMER}"
    off = next((r for r in ok if r["name"] == "spy_off"), None)
    parts: list[str] = []
    if off:
        parts.append(
            f"spy_off {_fmt_pct(off.get('return_pct'))} Sharpe {_fmt_num(off.get('sharpe'))} "
            f"MaxDD {_fmt_pct(off.get('max_dd_pct'))} SPY fills {off.get('spy_fills')} "
            f"vs spy_on {_fmt_pct(base.get('return_pct'))} Sharpe {_fmt_num(base.get('sharpe'))} "
            f"({_delta(off, base, 'return_pct')} ret)."
        )
        if _promote_ok(off, base):
            if days >= 300:
                parts.append(
                    "LIVE DEFAULT CANDIDATE (365d): spy_off beat spy_on on return+Sharpe "
                    f"with MaxDD within {MAX_DD_WORSE_PP:.1f}pp — discuss live change only after review."
                )
            else:
                parts.append("Queue 365d STRICT live-shaped confirm before any live change.")
        else:
            parts.append(
                "spy_off did not clear return+Sharpe+MaxDD rule — keep live SPY trend ON."
            )
    parts.append(DISCLAIMER)
    return " ".join(parts)


def _write(rows, *, days, window, bench, verdict, strict_banner, out_md: Path, out_json: Path) -> None:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    base = next((r for r in rows if r["name"] == "spy_on"), None)
    payload = {
        "generated_at": generated,
        "days": days,
        "window": window,
        "benchmark_return_pct": bench,
        "profile": "live_conservative",
        "strict_pit_banner": strict_banner,
        "disclaimer": DISCLAIMER,
        "promote_rule": {
            "return_and_sharpe_beat_baseline": True,
            "max_dd_worse_pp_max": MAX_DD_WORSE_PP,
        },
        "legs": rows,
        "verdict": verdict,
        "ok": all(bool(r.get("ok")) for r in rows),
    }
    out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    lines = [
        f"# STRICT Live Conservative SPY on/off A/B ({days}d)",
        "",
        f"Generated: {generated}",
        f"Window: {window} ({days}d)",
        f"Benchmark VTI B&H: {_fmt_pct(bench)}",
        f"Research sizing: ${RESEARCH_START_EQUITY:,.0f} start / "
        f"${RESEARCH_MAX_NOTIONAL:,.0f} max order (live 85/5 ratios)",
        "",
        f"**{strict_banner}**",
        "",
        f"**{DISCLAIMER}**",
        "",
        "| Leg | Config | Return | Sharpe | MaxDD | Trades | SPY fills | NYSE fills | vs spy_on ret |",
        "|-----|--------|--------|--------|-------|--------|-----------|------------|---------------|",
    ]
    for r in rows:
        lines.append(
            f"| {r['name']} "
            f"| {r.get('config', '')} "
            f"| {_fmt_pct(r.get('return_pct'))} "
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
    ap = argparse.ArgumentParser(description="STRICT Live Conservative SPY on/off A/B")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--legs", default="", help="Comma subset: spy_on,spy_off")
    args = ap.parse_args()
    days = max(20, int(args.days))
    out_md, out_json = _out_paths(days)
    wanted = {x.strip() for x in args.legs.split(",") if x.strip()}
    legs = [L for L in LEGS if not wanted or L.name in wanted]
    if not legs:
        raise SystemExit(f"Choose from: {[L.name for L in LEGS]}")

    print(f"--- STRICT Live Conservative SPY A/B ({days}d) ---")
    print(f"Legs ({len(legs)}):", ", ".join(L.name for L in legs))
    print(
        f"Research sizing: start ${RESEARCH_START_EQUITY:,.0f} | "
        f"max/order ${RESEARCH_MAX_NOTIONAL:,.0f} (live ratios kept)"
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
    live_vti = float(config.LIVE_VTI_CORE_PCT)
    for leg in legs:
        print(f"\n>>> {leg.name}: {leg.config_label}", flush=True)
        with _apply_leg(leg):
            strict_banner = config.format_strict_pit_banner() or (
                "STRICT PIT: ON | insider/RVOL/catalyst/news/LLM/dyn_univ/buffett-fallback off"
            )
            print(f"    {strict_banner}", flush=True)
            print(f"    {config.format_live_conservative_banner()}", flush=True)
            raw = run_backtest(
                data,
                track_active_exposure=True,
                track_metrics=True,
                paper_aggressive=False,
                small_account=True,
                vti_core_pct=live_vti,
                paper_thinking=False,
                strict_pit=True,
                paper_crypto_enabled=False,
                live_thinking_start_equity=float(RESEARCH_START_EQUITY),
                verbose=False,
            )
        row = _extract(raw, leg, strict_banner=strict_banner)
        rows.append(row)
        print(
            f"    -> ret {_fmt_pct(row.get('return_pct'))} "
            f"Sharpe {_fmt_num(row.get('sharpe'))} "
            f"MaxDD {_fmt_pct(row.get('max_dd_pct'))} "
            f"trades {row.get('trade_count')} "
            f"SPY {row.get('spy_fills')} NYSE {row.get('nyse_fills')}",
            flush=True,
        )
        base = next((r for r in rows if r["name"] == "spy_on"), None)
        _write(
            rows,
            days=days,
            window=window,
            bench=bench,
            verdict=_verdict(rows, base, days=days),
            strict_banner=strict_banner,
            out_md=out_md,
            out_json=out_json,
        )

    base = next((r for r in rows if r["name"] == "spy_on"), None)
    verdict = _verdict(rows, base, days=days)
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
