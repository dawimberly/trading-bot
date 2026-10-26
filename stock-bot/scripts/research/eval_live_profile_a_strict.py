"""STRICT Live Profile A (not paper-aggressive) vs VTI B&H.

Reuses scripts/analysis/eval_live_spy_ab.py live-conservative + STRICT PIT
context. One stack: 85% VTI, SPY trend ON, NYSE leftover, scanners/stat-arb OFF.

Usage (from stock-bot/):
  python scripts/research/eval_live_profile_a_strict.py
  python scripts/research/eval_live_profile_a_strict.py --windows 365
  python scripts/research/eval_live_profile_a_strict.py --windows 90,180,365

Writes:
  scripts/research/live_profile_a_strict_last.md
  scripts/research/live_profile_a_strict_last.json

Research only. No promote. No .env / sleeve / live Profile A edits. No MC.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "analysis"))

os.environ.setdefault("PAPER_DEPLOY_DEBUG", "false")
os.environ.setdefault("PYTHONUNBUFFERED", "1")
# Backtest must not re-parse the live portal CSV every bar (ragged header spam).
_STUB_JOURNAL = Path(__file__).resolve().parent / "_empty_paper_journal.csv"
if not _STUB_JOURNAL.is_file():
    _STUB_JOURNAL.write_text(
        "timestamp,event,symbol,ticker,side,regime,pair_key,z_score,"
        "equity,cash,notional,qty,price,sleeve,exit_reason,notes\n",
        encoding="utf-8",
    )
os.environ.setdefault("PAPER_JOURNAL_CSV", str(_STUB_JOURNAL))
import warnings

warnings.filterwarnings("ignore", category=UserWarning, module="pandas")
warnings.filterwarnings("ignore", message="Skipping line")

import config

config.PAPER_DEPLOY_DEBUG = False
config.PAPER_JOURNAL_CSV = str(_STUB_JOURNAL)

from backtester import MIN_HISTORY, _benchmark_return, _ensure_daily_data, run_backtest
from eval_live_spy_ab import (  # noqa: E402
    LEGS,
    RESEARCH_MAX_NOTIONAL,
    RESEARCH_START_EQUITY,
    _apply_leg,
)
from modules.backtester_core import RUN_OPTIONS

OUT_MD = Path(__file__).resolve().parent / "live_profile_a_strict_last.md"
OUT_JSON = Path(__file__).resolve().parent / "live_profile_a_strict_last.json"
DISCLAIMER = (
    "STRICT live-shaped research only. Do not promote. Live Profile A unchanged. "
    "Research equity is scaled so the 5% SPY sleeve can fill; live ~$300 / $10 max cannot."
)


def _safe_float(val: Any) -> float | None:
    try:
        return None if val is None else float(val)
    except (TypeError, ValueError):
        return None


def _fmt_pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.2f}%"


def _fmt_num(v: float | None, d: int = 2) -> str:
    return "n/a" if v is None else f"{v:.{d}f}"


def _fmt_delta(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.2f}pp"


def _extract(result: dict | None, *, days: int, window: str, bench: float | None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "days": days,
        "window": window,
        "ok": False,
        "return_pct": None,
        "sharpe": None,
        "max_dd_pct": None,
        "vti_bh_pct": bench,
        "delta_vs_vti_pp": None,
        "trade_count": None,
        "spy_fills": None,
        "nyse_fills": None,
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
    delta = None if bench is None else ret - float(bench)
    row.update(
        {
            "ok": bool(result.get("ok", True)),
            "return_pct": ret,
            "sharpe": sharpe,
            "max_dd_pct": max_dd,
            "delta_vs_vti_pp": delta,
            "trade_count": int(result.get("total_orders") or 0),
            "spy_fills": int(result.get("spy_signals") or 0),
            "nyse_fills": int(result.get("nyse_signals") or 0),
            "strict_pit": bool(result.get("strict_pit")),
            "start_date": result.get("start_date"),
            "end_date": result.get("end_date"),
            "sim_days": result.get("sim_days"),
            "final_equity": _safe_float(result.get("final_equity")),
        }
    )
    return row


def _run_profile_a(data) -> dict:
    leg = next(L for L in LEGS if L.name == "spy_on")
    live_vti = float(config.LIVE_VTI_CORE_PCT)
    saved_paper = bool(config.PAPER_TRADING)
    try:
        # In-process live-shaped lock only. Does not write .env.
        config.PAPER_TRADING = False
        with _apply_leg(leg):
            banner = config.format_live_conservative_banner()
            strict = config.format_strict_pit_banner() or (
                "STRICT PIT: ON | insider/RVOL/catalyst/news/LLM/dyn_univ/buffett-fallback off"
            )
            print(f"    {strict}", flush=True)
            print(f"    {banner}", flush=True)
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
                paper_stat_arb=False,
                live_thinking_start_equity=float(RESEARCH_START_EQUITY),
                verbose=False,
            )
    finally:
        config.PAPER_TRADING = saved_paper
    return raw


def _write(rows: list[dict], *, caveats: list[str]) -> None:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    payload = {
        "generated_at": generated,
        "profile": "live_profile_a_strict",
        "disclaimer": DISCLAIMER,
        "research_start_equity": RESEARCH_START_EQUITY,
        "research_max_notional": RESEARCH_MAX_NOTIONAL,
        "live_vti_core_pct": float(config.LIVE_VTI_CORE_PCT),
        "rows": rows,
        "caveats": caveats,
        "ok": all(bool(r.get("ok")) for r in rows) if rows else False,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    lines = [
        "# STRICT Live Profile A vs VTI B&H",
        "",
        f"Generated: {generated}",
        "",
        f"**{DISCLAIMER}**",
        "",
        "Stack: 85% VTI core, SPY trend ON, NYSE leftover, same-day rebuy blocked "
        "(LIVE_NYSE_SAME_DAY_REENTRY_BLOCK). Stat arb / shorts / ARIMA / daily bank / "
        "RVOL-ORB-catalyst OFF. GARCH + ATR/exits + corr + tail ON via "
        "`enforce_live_conservative_profile()`.",
        "",
        f"Research sizing: ${RESEARCH_START_EQUITY:,.0f} start / "
        f"${RESEARCH_MAX_NOTIONAL:,.0f} max order (live 1% risk ratio; live $10 max "
        "cannot fill a 5% SPY sleeve).",
        "",
        "| Window | Dates | Profile A return | Sharpe | MaxDD | VTI B&H | Δ vs VTI | Trades | SPY | NYSE |",
        "|--------|-------|------------------|--------|-------|---------|----------|--------|-----|------|",
    ]
    for r in rows:
        lines.append(
            f"| {r.get('days')}d "
            f"| {r.get('window')} "
            f"| {_fmt_pct(r.get('return_pct'))} "
            f"| {_fmt_num(r.get('sharpe'))} "
            f"| {_fmt_pct(r.get('max_dd_pct'))} "
            f"| {_fmt_pct(r.get('vti_bh_pct'))} "
            f"| {_fmt_delta(r.get('delta_vs_vti_pp'))} "
            f"| {r.get('trade_count')} "
            f"| {r.get('spy_fills')} "
            f"| {r.get('nyse_fills')} |"
        )
    lines.extend(["", "## Caveats", ""])
    for c in caveats:
        lines.append(f"- {c}")
    lines.extend(["", "No MC 200. No promote.", ""])
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="STRICT Live Profile A vs VTI B&H")
    ap.add_argument("--windows", default="365,90", help="Comma days, 365 first")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    windows = []
    for tok in str(args.windows).split(","):
        tok = tok.strip()
        if not tok:
            continue
        windows.append(max(20, int(tok)))
    if not windows:
        windows = [365]

    caveats = [
        "Not paper-aggressive; live-conservative context only.",
        "STRICT PIT kill-switches: insider / RVOL / catalyst / hist-news / LLM / dyn_univ / buffett fallback OFF.",
        "Same-day NYSE rebuy uses LIVE_NYSE_SAME_DAY_REENTRY_BLOCK (default true). Paper max-2-adds / $25 min / ATR sleeve cooldown are paper-only and stay off.",
        "Research start equity $10k / $500 max order so 5% SPY can fill. Live ~$300 / 1% / $10 max is smaller; SPY may not trade there.",
        "PAPER_TRADING is False only in-process for enforce_live_conservative_profile(); .env is not written.",
        "Backtest uses a stub PAPER_JOURNAL_CSV so live portal blotter is not re-parsed every bar.",
        "GARCH/ATR/corr/tail follow enforce_live_conservative_profile(); SMART_STOPS_LIVE default OFF unless env-explicit.",
        "No MC 200. Do not promote from this file.",
    ]

    print("--- STRICT Live Profile A vs VTI B&H ---")
    print(f"Windows: {windows}")
    print(
        f"Research sizing: ${RESEARCH_START_EQUITY:,.0f} start / "
        f"${RESEARCH_MAX_NOTIONAL:,.0f} max/order"
    )
    print(DISCLAIMER)

    rows: list[dict] = []
    for days in windows:
        print(f"\n>>> {days}d", flush=True)
        data = _ensure_daily_data(days, refresh=args.refresh, use_max=False)
        warmup = min(MIN_HISTORY, max(0, len(data) - 5))
        window = f"{data.index[warmup].date()} -> {data.index[-1].date()}"
        bench = _benchmark_return(data, warmup)
        print(f"    window {window}  VTI B&H {_fmt_pct(bench)}", flush=True)
        raw = _run_profile_a(data)
        row = _extract(raw, days=days, window=window, bench=bench)
        rows.append(row)
        print(
            f"    Profile A ret {_fmt_pct(row.get('return_pct'))} "
            f"Sharpe {_fmt_num(row.get('sharpe'))} "
            f"MaxDD {_fmt_pct(row.get('max_dd_pct'))} "
            f"vs VTI {_fmt_pct(bench)} "
            f"Δ {_fmt_delta(row.get('delta_vs_vti_pp'))} "
            f"trades {row.get('trade_count')} SPY {row.get('spy_fills')} NYSE {row.get('nyse_fills')}",
            flush=True,
        )
        if row.get("error"):
            print(f"    ERROR {row['error']}", flush=True)
        _write(rows, caveats=caveats)

    print("\n=== Summary ===")
    print(
        f"{'Window':<8} {'Dates':<28} {'A ret':>9} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'VTI':>9} {'Δ':>8} {'n':>5}"
    )
    for r in rows:
        print(
            f"{str(r.get('days'))+'d':<8} {str(r.get('window')):<28} "
            f"{_fmt_pct(r.get('return_pct')):>9} {_fmt_num(r.get('sharpe')):>7} "
            f"{_fmt_pct(r.get('max_dd_pct')):>8} {_fmt_pct(r.get('vti_bh_pct')):>9} "
            f"{_fmt_delta(r.get('delta_vs_vti_pp')):>8} "
            f"{r.get('trade_count'):>5}"
        )
    print(f"\nWrote {OUT_MD}")
    print(DISCLAIMER)
    RUN_OPTIONS.strict_pit = False
    RUN_OPTIONS.no_thinking = False
    return 0 if all(r.get("ok") for r in rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
