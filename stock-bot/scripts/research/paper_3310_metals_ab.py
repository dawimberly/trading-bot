"""STRICT PIT A/B: employed paper 33/67 vs 33/57/10 always-on metals.

Research only. No .env write. No orders. No live. No process restart.

BASELINE = employed paper: 33% VTI / 67% NYSE, metal sleeve OFF
TREATMENT = 33% VTI / 57% NYSE / 10% always-on GLD50/SLV30/CPER20
  (thin overlay — run_backtest does not hold always-on metals; game_plan
   stress-gates dump metals off-stress, so we do NOT use that path)

Usage (from stock-bot/):
  python -u scripts/research/paper_3310_metals_ab.py
  python -u scripts/research/paper_3310_metals_ab.py --windows 90,180,365
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PAPER_DEPLOY_DEBUG", "false")
os.environ.setdefault("PYTHONUNBUFFERED", "1")

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
from modules.backtester_core import RUN_OPTIONS, compute_performance_metrics
from modules.data_loader import load_close_matrix

OUT_MD = Path(__file__).with_name("paper_3310_metals_ab_last.md")
OUT_JSON = Path(__file__).with_name("paper_3310_metals_ab_last.json")

START_EQUITY = 100_000.0
METAL_PCT = 0.10
PAPER_FRAC = 1.0 - METAL_PCT  # 0.90
VTI_TOTAL = 0.33
NYSE_BASE = 0.67
NYSE_TREAT = 0.57
DRIFT_BAND = 0.02  # rebalance when |metals_pct - 0.10| > 2pp
DISCLAIMER = "HOLD / no promote. Do not copy into alpaca_paper_v2/.env."


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


def _checkpoint(rows: list[dict[str, Any]]) -> None:
    """Write partial results after each window so long runs are not lost."""
    md = _write_report(rows)
    OUT_MD.write_text(md, encoding="utf-8")
    payload = {
        "research_only": True,
        "disclaimer": DISCLAIMER,
        "start_equity": START_EQUITY,
        "baseline": "33/67 metals OFF",
        "treatment": "33/57/10 always-on metal overlay",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "windows": rows,
        "partial": True,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"  checkpoint -> {OUT_MD.name}", flush=True)


def _fmt_usd(v: float | None) -> str:
    return "n/a" if v is None else f"${v:,.0f}"


@contextmanager
def _employed_stack(*, nyse_cap: float, vti_pct: float):
    """In-process employed paper locks; always restore."""
    keys = [
        "PAPER_DYNAMIC_VTI_ENABLED",
        "PAPER_VTI_CORE_PCT",
        "VTI_CORE_PCT",
        "DYNAMIC_VTI_PAPER_FLOOR",
        "DYNAMIC_VTI_PAPER_CEILING",
        "DYNAMIC_VTI_FLOOR_MIN",
        "DYNAMIC_VTI_ALLOW_ZERO",
        "PAPER_NYSE_SLEEVE_CAP_PCT",
        "NYSE_SLEEVE_CAP_PCT",
        "PAPER_NYSE_HIGH_CASH_CAP_PCT",
        "PAPER_NYSE_MAX_EXPOSURE_PCT",
        "PAPER_REGIME_WEAK_SLEEVE_MAX_PCT",
        "SPY_SLEEVE_CAP_PCT",
        "PAPER_SPY_MAX_EXPOSURE_PCT",
        "CRYPTO_SLEEVE_ENABLED",
        "PAPER_CRYPTO_ENABLED",
        "PAPER_STAT_ARB_ENABLED",
        "PAPER_SOCIAL_SLEEVE_ENABLED",
        "SOCIAL_SLEEVE_ENABLED",
        "FELIX_SOCIAL_DYNAMIC_ENABLED",
        "ORB_ENABLED",
        "PAPER_ORB_ENABLED",
        "ORB_MOMENTUM_ENABLED",
        "PAPER_ORB_MOMENTUM_ENABLED",
        "PAPER_SECTOR_ROTATION_ENABLED",
        "SECTOR_ROTATION_ENABLED",
        "PAPER_VOL_BREAKOUT_ENABLED",
        "PAPER_VOL_TRADING_ENABLED",
        "PAPER_OPTIONS_SLEEVE_ENABLED",
        "OPTIONS_SLEEVE_ENABLED",
        "METALS_AS_EQUITY_ENABLED",
        "GAME_PLAN_ENABLED",
        "GAME_PLAN_YIELD_GATE_ONLY",
        "METAL_SLEEVE_CAP_PCT",
        "MAX_ACTIVE_TICKERS",
        "PAPER_MAX_EQUITY_TRADES",
        "PAPER_MAX_POSITION_PCT",
        "PAPER_NYSE_PER_NAME_MAX_PCT",
        "PAPER_ACTIVE_SLEEVE_BOOST",
        "PAPER_THINKING_ENGINE_ENABLED",
        "STRICT_PIT_BACKTEST",
        "PAPER_NYSE_ENTRY_HYGIENE_ENABLED",
        "PAPER_DYNAMIC_UNIVERSE_ENABLED",
        "OPPORTUNISTIC_SHORTS_ENABLED",
        "PAPER_OPPORTUNISTIC_SHORTS_ENABLED",
    ]
    saved = {k: getattr(config, k, None) for k in keys}
    saved["thinking"] = config.PAPER_THINKING_ENGINE_ENABLED
    saved["strict_ctx"] = config.backtest_strict_pit_context()
    saved["strict_allow"] = set(config.strict_pit_allow())
    try:
        config.PAPER_DYNAMIC_VTI_ENABLED = False
        config.PAPER_VTI_CORE_PCT = float(vti_pct)
        config.VTI_CORE_PCT = float(vti_pct)
        config.DYNAMIC_VTI_PAPER_FLOOR = float(vti_pct)
        config.DYNAMIC_VTI_PAPER_CEILING = float(vti_pct)
        config.DYNAMIC_VTI_FLOOR_MIN = float(vti_pct)
        config.DYNAMIC_VTI_ALLOW_ZERO = False

        config.PAPER_NYSE_SLEEVE_CAP_PCT = float(nyse_cap)
        config.NYSE_SLEEVE_CAP_PCT = float(nyse_cap)
        config.PAPER_NYSE_HIGH_CASH_CAP_PCT = float(nyse_cap)
        config.PAPER_NYSE_MAX_EXPOSURE_PCT = float(nyse_cap)
        config.PAPER_REGIME_WEAK_SLEEVE_MAX_PCT = float(nyse_cap)

        config.SPY_SLEEVE_CAP_PCT = 0.0
        config.PAPER_SPY_MAX_EXPOSURE_PCT = 0.0
        config.CRYPTO_SLEEVE_ENABLED = False
        config.PAPER_CRYPTO_ENABLED = False
        config.PAPER_STAT_ARB_ENABLED = False
        config.PAPER_SOCIAL_SLEEVE_ENABLED = False
        config.SOCIAL_SLEEVE_ENABLED = False
        config.FELIX_SOCIAL_DYNAMIC_ENABLED = False
        config.ORB_ENABLED = False
        config.PAPER_ORB_ENABLED = False
        config.ORB_MOMENTUM_ENABLED = False
        config.PAPER_ORB_MOMENTUM_ENABLED = False
        config.PAPER_SECTOR_ROTATION_ENABLED = False
        config.SECTOR_ROTATION_ENABLED = False
        config.PAPER_VOL_BREAKOUT_ENABLED = False
        config.PAPER_VOL_TRADING_ENABLED = False
        config.PAPER_OPTIONS_SLEEVE_ENABLED = False
        config.OPTIONS_SLEEVE_ENABLED = False
        for short_k in ("OPPORTUNISTIC_SHORTS_ENABLED", "PAPER_OPPORTUNISTIC_SHORTS_ENABLED"):
            if hasattr(config, short_k):
                setattr(config, short_k, False)

        # Metals are the 10% bucket via overlay — not NYSE miners, not METALS_AS_EQUITY.
        config.METALS_AS_EQUITY_ENABLED = False
        # Keep metal_sleeve_enabled() False inside run_backtest (stress-gated path unused).
        config.GAME_PLAN_ENABLED = False
        config.GAME_PLAN_YIELD_GATE_ONLY = True
        config.METAL_SLEEVE_CAP_PCT = float(METAL_PCT)

        config.MAX_ACTIVE_TICKERS = 15
        config.PAPER_MAX_EQUITY_TRADES = 15
        config.PAPER_MAX_POSITION_PCT = 0.10
        config.PAPER_NYSE_PER_NAME_MAX_PCT = 0.10
        config.PAPER_ACTIVE_SLEEVE_BOOST = 1.0
        config.PAPER_THINKING_ENGINE_ENABLED = False
        config.PAPER_NYSE_ENTRY_HYGIENE_ENABLED = True
        config.PAPER_DYNAMIC_UNIVERSE_ENABLED = False
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
            elif hasattr(config, k):
                setattr(config, k, v)
        RUN_OPTIONS.strict_pit = False
        RUN_OPTIONS.no_thinking = False


def _run_paper_leg(
    data,
    *,
    nyse_cap: float,
    vti_pct: float,
    start_equity: float,
) -> dict:
    with _employed_stack(nyse_cap=nyse_cap, vti_pct=vti_pct):
        print(
            f"    STRICT PIT | VTI={vti_pct:.0%} fixed | NYSE cap={nyse_cap:.0%} | "
            f"hygiene ON | metals_as_equity OFF | start=${start_equity:,.0f}",
            flush=True,
        )
        return run_backtest(
            data,
            track_active_exposure=True,
            track_metrics=True,
            track_sleeve_path=True,
            paper_aggressive=True,
            paper_sleeve_features=False,
            paper_dynamic_vti=False,
            paper_dynamic_universe=False,
            paper_thinking=False,
            with_news=False,
            strict_pit=True,
            paper_nyse_entry_hygiene=True,
            paper_crypto_enabled=False,
            paper_stat_arb=False,
            paper_options_sleeve=False,
            paper_vol_trading=False,
            felix_social_dynamic=False,
            paper_social_enhanced=False,
            small_account=True,
            live_thinking_start_equity=float(start_equity),
            vti_core_pct=float(vti_pct),
            verbose=False,
        )


def _equity_series(result: dict) -> pd.Series:
    idx = result.get("equity_index") or []
    vals = result.get("equity_values") or []
    if not idx or not vals or len(idx) != len(vals):
        return pd.Series(dtype=float)
    ts = pd.to_datetime(idx)
    return pd.Series(vals, index=ts, dtype=float)


def _end_metals_pct_from_sleeve_path(result: dict) -> float | None:
    series = result.get("sleeve_path_series") or []
    if not series:
        trough = result.get("sleeve_path_trough") or {}
        peak = result.get("sleeve_path_peak") or {}
        snap = trough or peak or {}
        return _safe_float(snap.get("metals_pct"))
    last = series[-1] if isinstance(series[-1], dict) else {}
    return _safe_float(last.get("metals_pct"))


def _load_metal_prices(index: pd.DatetimeIndex) -> pd.DataFrame:
    need = list(config.LIVE_METAL_SYMBOLS)
    # Exclude CEXY explicitly if ever present
    need = [s for s in need if s != "CEXY"]
    days = max(400, len(index) + 80)
    raw = load_close_matrix(interval="1d", days=days)
    if raw is None or raw.empty:
        raise RuntimeError("metal price load failed")
    cols = {}
    for sym in need:
        if sym not in raw.columns:
            raise RuntimeError(f"missing metal column {sym}")
        cols[sym] = pd.to_numeric(raw[sym], errors="coerce")
    frame = pd.DataFrame(cols)
    frame.index = pd.to_datetime(frame.index)
    # Align to paper equity index (ffill gaps)
    aligned = frame.reindex(index).ffill().bfill()
    if aligned.isna().any().any():
        bad = aligned.columns[aligned.isna().any()].tolist()
        raise RuntimeError(f"metal prices still NA after align: {bad}")
    return aligned


def always_on_metal_overlay(
    paper_eq: pd.Series,
    metal_px: pd.DataFrame,
    *,
    metal_target_pct: float = METAL_PCT,
    band: float = DRIFT_BAND,
) -> dict[str, Any]:
    """Drift-rebalanced always-on metal mini-core on top of paper 90% path.

    paper_eq starts near START_EQUITY * 0.90. Metals start at 10% and are
    rebalanced when |metals/combined - target| > band (cash transfer vs paper).
    """
    weights = config.metal_blend_weights()
    # Drop CEXY if present
    weights = {k: v for k, v in weights.items() if k != "CEXY"}
    wsum = sum(weights.values()) or 1.0
    weights = {k: v / wsum for k, v in weights.items()}

    paper = paper_eq.astype(float).copy()
    metal_cash_proxy = float(paper.iloc[0]) * (metal_target_pct / (1.0 - metal_target_pct))
    # Prefer exact: if paper starts at 90k, metals 10k
    start_combined = float(paper.iloc[0]) / (1.0 - metal_target_pct)
    metal_value = start_combined * metal_target_pct
    # Buy blend at first bar
    qty: dict[str, float] = {}
    px0 = metal_px.iloc[0]
    for sym, w in weights.items():
        p = float(px0[sym])
        qty[sym] = (metal_value * w) / p if p > 0 else 0.0

    combined_curve: list[float] = []
    metal_curve: list[float] = []
    metal_pct_curve: list[float] = []
    rebalances = 0

    for i, ts in enumerate(paper.index):
        px = metal_px.loc[ts]
        mtm = sum(qty[s] * float(px[s]) for s in qty)
        p_eq = float(paper.iloc[i])
        combined = p_eq + mtm
        if combined <= 0:
            combined_curve.append(0.0)
            metal_curve.append(0.0)
            metal_pct_curve.append(0.0)
            continue
        m_pct = mtm / combined
        if abs(m_pct - metal_target_pct) > band:
            target_m = combined * metal_target_pct
            # Transfer: adjust paper series from this bar forward by delta
            delta = target_m - mtm
            # Shrink/grow paper equity path from i onward by -delta (cash move)
            paper.iloc[i:] = paper.iloc[i:] - delta
            p_eq = float(paper.iloc[i])
            mtm = target_m
            combined = p_eq + mtm
            # Redeploy blend at target
            for sym, w in weights.items():
                p = float(px[sym])
                qty[sym] = (mtm * w) / p if p > 0 else 0.0
            rebalances += 1
            m_pct = mtm / combined if combined > 0 else 0.0

        combined_curve.append(combined)
        metal_curve.append(mtm)
        metal_pct_curve.append(m_pct)

    curve = pd.Series(combined_curve, index=paper.index, dtype=float)
    init = float(start_combined)
    perf = compute_performance_metrics(
        list(curve.values),
        initial_capital=init,
        benchmark_return_pct=None,
        total_orders=0,
        equity_index=[str(x) for x in curve.index],
    )
    return {
        "combined_curve": curve,
        "metal_curve": pd.Series(metal_curve, index=paper.index),
        "metal_pct_curve": pd.Series(metal_pct_curve, index=paper.index),
        "perf": perf,
        "rebalances": rebalances,
        "end_metal_value": float(metal_curve[-1]) if metal_curve else None,
        "end_metal_pct": float(metal_pct_curve[-1]) * 100.0 if metal_pct_curve else None,
        "start_combined": init,
        "weights": weights,
    }


def _extract_base(result: dict | None, *, days: int, bench: float | None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "days": days,
        "ok": False,
        "return_pct": None,
        "sharpe": None,
        "max_dd_pct": None,
        "vti_bh_pct": bench,
        "delta_vs_vti_pp": None,
        "nyse_trades": None,
        "metals_pct": None,
        "metal_value": None,
        "final_equity": None,
        "start_date": None,
        "end_date": None,
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
    metals_pct = _end_metals_pct_from_sleeve_path(result)
    final_eq = _safe_float(result.get("final_equity"))
    metal_val = None
    if metals_pct is not None and final_eq is not None:
        metal_val = final_eq * (metals_pct / 100.0)
    row.update(
        {
            "ok": bool(result.get("ok", True)),
            "return_pct": ret,
            "sharpe": sharpe,
            "max_dd_pct": max_dd,
            "delta_vs_vti_pp": None if bench is None else ret - float(bench),
            "nyse_trades": int(result.get("nyse_signals") or 0),
            "metals_pct": metals_pct,
            "metal_value": metal_val,
            "final_equity": final_eq,
            "start_date": result.get("start_date"),
            "end_date": result.get("end_date"),
            "sim_days": result.get("sim_days"),
            "vti_core_pct": _safe_float(result.get("vti_core_pct")),
        }
    )
    return row


def _window_slice(data: pd.DataFrame, days: int) -> pd.DataFrame:
    need = days + MIN_HISTORY + 5
    if len(data) < need:
        return data.copy()
    return data.iloc[-(need):].copy()


def _write_report(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Paper 33/67 vs 33/57/10 always-on metals (STRICT PIT)",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "**Research only. No `.env` write. No orders. No live. No restart.**",
        "",
        f"**Capital:** ${START_EQUITY:,.0f} paper-scale.  "
        f"**Baseline:** VTI {VTI_TOTAL:.0%} / NYSE {NYSE_BASE:.0%}, metal OFF "
        f"(`metal_sleeve_enabled=False`).  "
        f"**Treatment:** VTI {VTI_TOTAL:.0%} / NYSE {NYSE_TREAT:.0%} / metals "
        f"{METAL_PCT:.0%} always-on GLD/SLV/CPER ({config.METAL_BLEND_GLD:.0%}/"
        f"{config.METAL_BLEND_SLV:.0%}/{config.METAL_BLEND_CPER:.0%}) via thin "
        f"drift overlay (not `backtester_metals` stress gate).  "
        "Miners stay in NYSE; `METALS_AS_EQUITY=false`; no CEXY.",
        "",
        "| Window | Dates | Base ret | Base Sharpe | Base MaxDD | Treat ret | Treat Sharpe | Treat MaxDD | d ret vs base | d MaxDD vs base | d treat vs VTI | NYSE trades base/treat | Base metals% | Treat metals% | Treat metal $"
        " |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        b = r["baseline"]
        t = r["treatment"]
        dates = "n/a"
        if b.get("start_date") and b.get("end_date"):
            dates = f"{b['start_date']} → {b['end_date']}"
        d_ret = None
        d_dd = None
        if b.get("return_pct") is not None and t.get("return_pct") is not None:
            d_ret = t["return_pct"] - b["return_pct"]
        if b.get("max_dd_pct") is not None and t.get("max_dd_pct") is not None:
            d_dd = t["max_dd_pct"] - b["max_dd_pct"]
        lines.append(
            f"| {r['days']}d | {dates} | {_fmt_pct(b.get('return_pct'))} | "
            f"{_fmt_num(b.get('sharpe'))} | {_fmt_pct(b.get('max_dd_pct'))} | "
            f"{_fmt_pct(t.get('return_pct'))} | {_fmt_num(t.get('sharpe'))} | "
            f"{_fmt_pct(t.get('max_dd_pct'))} | {_fmt_delta(d_ret)} | {_fmt_delta(d_dd)} | "
            f"{_fmt_delta(t.get('delta_vs_vti_pp'))} | "
            f"{b.get('nyse_trades')}/{t.get('nyse_trades')} | "
            f"{_fmt_num(b.get('metals_pct'), 2)}% | {_fmt_num(t.get('metals_pct'), 2)}% | "
            f"{_fmt_usd(t.get('metal_value'))} |"
        )

    # Survival sentence from 365d if present else longest
    pick = next((r for r in rows if r["days"] == 365), rows[-1] if rows else None)
    sentence = "HOLD — insufficient rows to judge metals MaxDD effect."
    if pick and pick["baseline"].get("ok") and pick["treatment"].get("ok"):
        bdd = pick["baseline"].get("max_dd_pct")
        tdd = pick["treatment"].get("max_dd_pct")
        Bret = pick["baseline"].get("return_pct")
        Tret = pick["treatment"].get("return_pct")
        if bdd is not None and tdd is not None:
            # MaxDD is negative in this codebase usually
            helped_dd = float(tdd) > float(bdd)  # less negative = helped
            # Actually max_drawdown_pct is often negative; "helped survival" means MaxDD less severe
            # e.g. -8 > -12 means better. If both positive convention, smaller is better.
            # Check sign from existing reports: "-8.67%" — negative. Higher (closer to 0) = better.
            if float(bdd) <= 0 and float(tdd) <= 0:
                helped_dd = float(tdd) > float(bdd)
            else:
                helped_dd = abs(float(tdd)) < abs(float(bdd))
            if helped_dd and (Tret is None or Bret is None or Tret >= Bret - 1.0):
                sentence = (
                    f"On {pick['days']}d, 10% always-on metals **helped MaxDD** "
                    f"({_fmt_pct(bdd)} → {_fmt_pct(tdd)}); not only a copper/gold rally add-on — "
                    f"but still **HOLD / no promote** without a clean post-freeze week."
                )
            elif helped_dd:
                sentence = (
                    f"On {pick['days']}d, metals improved MaxDD ({_fmt_pct(bdd)} → {_fmt_pct(tdd)}) "
                    f"but return lagged — survival help, not free alpha; **HOLD / no promote**."
                )
            elif Tret is not None and Bret is not None and Tret > Bret + 0.5:
                sentence = (
                    f"On {pick['days']}d, 10% metals lifted return without MaxDD help "
                    f"(MaxDD {_fmt_pct(bdd)} → {_fmt_pct(tdd)}) — looks like metal beta when "
                    f"gold/copper ran, not survival; **HOLD / no promote**."
                )
            else:
                sentence = (
                    f"On {pick['days']}d, 10% always-on metals did **not** clearly help survival "
                    f"or return vs 33/67 (MaxDD {_fmt_pct(bdd)} → {_fmt_pct(tdd)}); "
                    f"**HOLD / no promote**."
                )

    lines.extend(
        [
            "",
            "## Verdict",
            "",
            sentence,
            "",
            f"**{DISCLAIMER}**",
            "",
            "## Method notes",
            "",
            "- Both legs: STRICT PIT, no thinking overlays, paper NYSE hygiene ON, "
            "SPY/crypto/stat-arb/shorts/social/felix/ORB/sector/vol-BO/options OFF, "
            "`PAPER_DYNAMIC_VTI=false`, fixed VTI 33%.",
            "- Baseline metals_pct from `track_sleeve_path` end snap (expect ~0).",
            "- Treatment: paper stack at 90% capital with VTI/NYSE = 33/57 of **total** "
            f"(i.e. VTI={VTI_TOTAL/PAPER_FRAC:.2%} / NYSE={NYSE_TREAT/PAPER_FRAC:.2%} of the "
            "90% book), then always-on metal overlay at 10% with ±2pp drift rebalance.",
            "- Overlay is research-only; not wired into `run_backtest` / game_plan.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--windows", default="365,180,90")
    args = ap.parse_args()
    windows = [int(x.strip()) for x in args.windows.split(",") if x.strip()]

    max_days = max(windows)
    print(f"Loading daily data for max window {max_days}d…", flush=True)
    data = _ensure_daily_data(days=max_days + MIN_HISTORY + 40)
    if data is None or data.empty or len(data) < MIN_HISTORY + 30:
        print("Not enough daily history")
        return 1

    rows: list[dict[str, Any]] = []
    for days in windows:
        print(f"\n=== Window {days}d ===", flush=True)
        sliced = _window_slice(data, days)
        bench = _benchmark_return(sliced, MIN_HISTORY)

        print("  BASELINE 33/67 metals OFF…", flush=True)
        base_raw = _run_paper_leg(
            sliced,
            nyse_cap=NYSE_BASE,
            vti_pct=VTI_TOTAL,
            start_equity=START_EQUITY,
        )
        baseline = _extract_base(base_raw, days=days, bench=bench)

        print("  TREATMENT paper 90% @ 33/57 of total…", flush=True)
        # VTI/NYSE as fraction of the 90% paper book so totals are 33/57 of combined
        vti_paper = VTI_TOTAL / PAPER_FRAC
        nyse_paper = NYSE_TREAT / PAPER_FRAC
        treat_paper_raw = _run_paper_leg(
            sliced,
            nyse_cap=nyse_paper,
            vti_pct=vti_paper,
            start_equity=START_EQUITY * PAPER_FRAC,
        )
        paper_eq = _equity_series(treat_paper_raw)
        treatment: dict[str, Any] = {
            "days": days,
            "ok": False,
            "return_pct": None,
            "sharpe": None,
            "max_dd_pct": None,
            "vti_bh_pct": bench,
            "delta_vs_vti_pp": None,
            "nyse_trades": int(treat_paper_raw.get("nyse_signals") or 0)
            if isinstance(treat_paper_raw, dict)
            else None,
            "metals_pct": None,
            "metal_value": None,
            "final_equity": None,
            "start_date": treat_paper_raw.get("start_date")
            if isinstance(treat_paper_raw, dict)
            else None,
            "end_date": treat_paper_raw.get("end_date")
            if isinstance(treat_paper_raw, dict)
            else None,
            "error": None,
            "overlay_rebalances": None,
        }
        if paper_eq.empty:
            treatment["error"] = "empty_paper_equity"
        else:
            print("  Overlay always-on GLD/SLV/CPER 10%…", flush=True)
            try:
                metal_px = _load_metal_prices(paper_eq.index)
                overlay = always_on_metal_overlay(paper_eq, metal_px)
                perf = overlay["perf"]
                ret = _safe_float(perf.get("total_return_pct"))
                sharpe = _safe_float(perf.get("sharpe"))
                max_dd = _safe_float(perf.get("max_drawdown_pct"))
                treatment.update(
                    {
                        "ok": ret is not None and sharpe is not None and max_dd is not None,
                        "return_pct": ret,
                        "sharpe": sharpe,
                        "max_dd_pct": max_dd,
                        "delta_vs_vti_pp": None
                        if bench is None or ret is None
                        else ret - float(bench),
                        "metals_pct": overlay.get("end_metal_pct"),
                        "metal_value": overlay.get("end_metal_value"),
                        "final_equity": _safe_float(perf.get("final_equity")),
                        "overlay_rebalances": overlay.get("rebalances"),
                        "error": None
                        if ret is not None
                        else "overlay_metrics_failed",
                    }
                )
            except Exception as exc:
                treatment["error"] = f"overlay_failed: {exc}"
                print(f"    overlay error: {exc}", flush=True)

        rows.append(
            {
                "days": days,
                "baseline": baseline,
                "treatment": treatment,
                "vti_bh_pct": bench,
            }
        )
        print(
            f"  base ret={_fmt_pct(baseline.get('return_pct'))} "
            f"metals%={_fmt_num(baseline.get('metals_pct'))} | "
            f"treat ret={_fmt_pct(treatment.get('return_pct'))} "
            f"metals%={_fmt_num(treatment.get('metals_pct'))}",
            flush=True,
        )
        _checkpoint(rows)

    md = _write_report(rows)
    OUT_MD.write_text(md, encoding="utf-8")
    payload = {
        "research_only": True,
        "disclaimer": DISCLAIMER,
        "start_equity": START_EQUITY,
        "baseline": "33/67 metals OFF",
        "treatment": "33/57/10 always-on metal overlay",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "windows": rows,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    print("\n" + md.encode("ascii", errors="replace").decode("ascii"))
    print(f"\nWrote {OUT_MD}")
    print(f"Wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
