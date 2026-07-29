"""STRICT bear/stress notebook — report only (no promote, no param search).

Runs paper-aggressive freeze profile (SPY off, hygiene ON, STRICT PIT, no-thinking)
on a real drawdown calendar window (prefer 2022), and compares to a recent bull
90d STRICT baseline (from eval_strict_windows_last or a fresh 90d leg).

Usage (from stock-bot/):
  python scripts/analysis/eval_strict_stress_notebook.py
  python scripts/analysis/eval_strict_stress_notebook.py --stress-start 2022-01-03 --stress-end 2022-12-30
  python scripts/analysis/eval_strict_stress_notebook.py --refresh

Writes:
  scripts/analysis/eval_strict_stress_notebook_last.md
  scripts/analysis/eval_strict_stress_notebook_last.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PAPER_DEPLOY_DEBUG", "false")
os.environ.setdefault("PYTHONUNBUFFERED", "1")

import config

config.PAPER_DEPLOY_DEBUG = False

from backtester import (  # noqa: E402
    MIN_HISTORY,
    _benchmark_return,
    _ensure_daily_data,
    run_backtest,
    simulation_warmup_bars,
)
from modules.backtester_core import RUN_OPTIONS  # noqa: E402

OUT_MD = Path(__file__).with_name("eval_strict_stress_notebook_last.md")
OUT_JSON = Path(__file__).with_name("eval_strict_stress_notebook_last.json")
WINDOWS_MD = Path(__file__).with_name("eval_strict_windows_last.md")
WINDOWS_JSON = Path(__file__).with_name("eval_strict_windows_last.json")

DISCLAIMER = (
    "REPORT ONLY - freeze stays on; no promote recommendations; "
    "no live Profile A changes; no param search"
)


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


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.2f}%"


def _fmt_num(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "n/a"
    return f"{v:.{digits}f}"


def _run_strict(data) -> dict:
    saved_env = bool(config.STRICT_PIT_BACKTEST)
    saved_ctx = config.backtest_strict_pit_context()
    saved_allow = set(config.strict_pit_allow())
    saved_thinking = config.PAPER_THINKING_ENGINE_ENABLED
    saved_spy = config.SPY_SLEEVE_CAP_PCT
    saved_spy_exp = config.PAPER_SPY_MAX_EXPOSURE_PCT
    try:
        config.STRICT_PIT_BACKTEST = True
        RUN_OPTIONS.strict_pit = True
        RUN_OPTIONS.no_thinking = True
        config.apply_strict_pit_kill_switches(allow=None)
        try:
            config.assert_strict_pit_gates_off(allow=set())
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        # Freeze profile: SPY satellite OFF (even if env had it on).
        config.SPY_SLEEVE_CAP_PCT = 0.0
        config.PAPER_SPY_MAX_EXPOSURE_PCT = 0.0
        return run_backtest(
            data,
            track_active_exposure=True,
            track_metrics=True,
            paper_aggressive=True,
            paper_sleeve_features=True,
            paper_dynamic_vti=True,
            paper_thinking=False,
            with_news=False,
            strict_pit=True,
            paper_nyse_entry_hygiene=True,
            paper_crypto_enabled=False,
            verbose=False,
        )
    finally:
        config.STRICT_PIT_BACKTEST = saved_env
        config.set_backtest_strict_pit_context(saved_ctx)
        config.set_strict_pit_allow(saved_allow)
        config.PAPER_THINKING_ENGINE_ENABLED = saved_thinking
        config.SPY_SLEEVE_CAP_PCT = saved_spy
        config.PAPER_SPY_MAX_EXPOSURE_PCT = saved_spy_exp
        RUN_OPTIONS.strict_pit = False
        RUN_OPTIONS.no_thinking = False


def _extract(result: dict | None, *, label: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "label": label,
        "ok": False,
        "return_pct": None,
        "sharpe": None,
        "max_dd_pct": None,
        "trade_count": None,
        "nyse_fills": None,
        "spy_fills": None,
        "benchmark_return_pct": None,
        "start_date": None,
        "end_date": None,
        "sim_days": None,
        "error": None,
        "strict_pit": True,
        "nyse_entry_hygiene": True,
        "spy_sleeve_off": True,
    }
    if not isinstance(result, dict):
        row["error"] = "missing_result"
        return row
    if result.get("error"):
        row["error"] = str(result.get("error"))
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
    row.update(
        {
            "ok": bool(result.get("ok", True)),
            "return_pct": ret,
            "sharpe": sharpe,
            "max_dd_pct": max_dd,
            "trade_count": _safe_int(result.get("total_orders")) or 0,
            "nyse_fills": _safe_int(result.get("nyse_signals")) or 0,
            "spy_fills": _safe_int(result.get("spy_fills")) or 0,
            "benchmark_return_pct": _safe_float(result.get("benchmark_return_pct")),
            "final_equity": _safe_float(result.get("final_equity")),
            "start_date": result.get("start_date"),
            "end_date": result.get("end_date"),
            "sim_days": result.get("sim_days"),
            "strict_pit": bool(result.get("strict_pit", True)),
            "nyse_entry_hygiene": bool(result.get("nyse_entry_hygiene", True)),
        }
    )
    return row


def _slice_stress_window(
    data: pd.DataFrame,
    *,
    stress_start: pd.Timestamp,
    stress_end: pd.Timestamp,
) -> tuple[pd.DataFrame, str]:
    """Return frame ending at stress_end with warmup so sim starts near stress_start."""
    if data is None or data.empty:
        raise RuntimeError("empty market data")
    idx = data.index
    # Normalize tz-naive comparisons
    if getattr(idx, "tz", None) is not None:
        stress_start = stress_start.tz_localize(idx.tz) if stress_start.tzinfo is None else stress_start
        stress_end = stress_end.tz_localize(idx.tz) if stress_end.tzinfo is None else stress_end
    available_end = idx[-1]
    available_start = idx[0]
    note = ""
    if stress_end > available_end:
        stress_end = pd.Timestamp(available_end)
        note = f"clamped stress_end to data end {stress_end.date()}"
    if stress_start < available_start:
        stress_start = pd.Timestamp(available_start)
        note = (note + "; " if note else "") + f"clamped stress_start to data start {stress_start.date()}"

    clipped = data.loc[idx <= stress_end].copy()
    if clipped.empty:
        raise RuntimeError(f"no bars on/before {stress_end.date()}")

    # Ensure enough pre-window bars for warmup so simulation_warmup lands near stress_start.
    n = len(clipped)
    warm = simulation_warmup_bars(n)
    # Ideal: first sim bar ~= first bar >= stress_start
    post = clipped.loc[clipped.index >= stress_start]
    if post.empty:
        # Fall back: use last ~252 trading days ending at stress_end
        take = min(len(clipped), warm + 252)
        frame = clipped.iloc[-take:].copy()
        note = (note + "; " if note else "") + "stress_start not in data; used ~1y ending at stress_end"
        return frame, note or "ok"

    first_trade_i = clipped.index.get_indexer([post.index[0]], method="pad")[0]
    need_start_i = max(0, first_trade_i - warm)
    frame = clipped.iloc[need_start_i:].copy()
    # After warmup, sim should start at/near stress_start
    warm2 = simulation_warmup_bars(len(frame))
    sim_start = frame.index[warm2]
    if abs((sim_start - stress_start).days) > 10:
        note = (
            (note + "; " if note else "")
            + f"sim starts {sim_start.date()} (target {stress_start.date()})"
        )
    return frame, note or "ok"


def _load_bull_90_from_cache() -> dict[str, Any] | None:
    if WINDOWS_JSON.is_file():
        try:
            payload = json.loads(WINDOWS_JSON.read_text(encoding="utf-8"))
            rows = payload.get("legs") or payload.get("rows") or []
            for r in rows:
                if int(r.get("days") or 0) == 90 and str(r.get("mode") or "").upper() == "STRICT":
                    if r.get("ok") and r.get("return_pct") is not None:
                        out = dict(r)
                        out["label"] = "bull_90d_strict_cached"
                        out["source"] = str(WINDOWS_JSON.name)
                        return out
        except Exception:
            pass
    return None


def _write_report(
    *,
    stress: dict[str, Any],
    bull: dict[str, Any] | None,
    stress_meta: dict[str, Any],
) -> str:
    lines = [
        "# STRICT bear/stress notebook (report only)",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"Profile: paper-aggressive freeze (SPY off, Dyn VTI ON, hygiene ON)",
        f"**STRICT PIT: ON | no-thinking | overlays off**",
        "",
        f"**{DISCLAIMER}**",
        "",
        "## Stress window",
        "",
        f"- Requested: {stress_meta.get('requested_start')} -> {stress_meta.get('requested_end')}",
        f"- Slice note: {stress_meta.get('slice_note')}",
        f"- Data coverage: {stress_meta.get('data_start')} -> {stress_meta.get('data_end')} ({stress_meta.get('data_bars')} bars)",
        "",
        "| Leg | Window | Return | Sharpe | MaxDD | VTI B&H | Trades | SPY fills | NYSE |",
        "|-----|--------|--------|--------|-------|---------|--------|-----------|------|",
    ]

    def _row(r: dict[str, Any]) -> str:
        win = f"{r.get('start_date')} -> {r.get('end_date')}"
        return (
            f"| {r.get('label')} | {win} | {_fmt_pct(r.get('return_pct'))} | "
            f"{_fmt_num(r.get('sharpe'))} | {_fmt_pct(r.get('max_dd_pct'))} | "
            f"{_fmt_pct(r.get('benchmark_return_pct'))} | {r.get('trade_count', 'n/a')} | "
            f"{r.get('spy_fills', 'n/a')} | {r.get('nyse_fills', 'n/a')} |"
        )

    lines.append(_row(stress))
    if bull:
        lines.append(_row(bull))

    lines.extend(["", "## vs VTI B&H (stress leg)", ""])
    if stress.get("ok") and stress.get("benchmark_return_pct") is not None:
        d = float(stress["return_pct"]) - float(stress["benchmark_return_pct"])
        lines.append(
            f"- Strategy {_fmt_pct(stress['return_pct'])} vs VTI {_fmt_pct(stress['benchmark_return_pct'])} "
            f"({d:+.2f}pp)"
        )
    else:
        lines.append(f"- Stress leg incomplete: {stress.get('error') or 'n/a'}")

    lines.extend(["", "## vs recent bull 90d STRICT", ""])
    if bull and bull.get("ok") and stress.get("ok"):
        lines.append(
            f"- Bull 90d: {_fmt_pct(bull.get('return_pct'))} Sharpe {_fmt_num(bull.get('sharpe'))} "
            f"MaxDD {_fmt_pct(bull.get('max_dd_pct'))} "
            f"(source: {bull.get('source') or bull.get('label')})"
        )
        lines.append(
            f"- Stress: {_fmt_pct(stress.get('return_pct'))} Sharpe {_fmt_num(stress.get('sharpe'))} "
            f"MaxDD {_fmt_pct(stress.get('max_dd_pct'))}"
        )
        lines.append(
            f"- Delta (stress - bull90): return "
            f"{float(stress['return_pct']) - float(bull['return_pct']):+.2f}pp, "
            f"Sharpe {float(stress['sharpe']) - float(bull['sharpe']):+.2f}, "
            f"MaxDD {float(stress['max_dd_pct']) - float(bull['max_dd_pct']):+.2f}pp"
        )
    else:
        lines.append("- Bull 90d baseline unavailable or stress leg failed.")

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Freeze stays on — measurement only.",
            "- Do not retune from a single stress window.",
            "- Live Profile A unchanged.",
            f"- {DISCLAIMER}",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="STRICT bear/stress notebook (report only)")
    ap.add_argument("--stress-start", default="2022-01-03")
    ap.add_argument("--stress-end", default="2022-12-30")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument(
        "--rerun-bull90",
        action="store_true",
        help="Force a fresh 90d STRICT bull leg instead of cached windows JSON",
    )
    args = ap.parse_args()

    stress_start = pd.Timestamp(args.stress_start)
    stress_end = pd.Timestamp(args.stress_end)

    print("--- STRICT stress notebook ---")
    print(f"Requested: {stress_start.date()} -> {stress_end.date()}")
    print(DISCLAIMER)

    # Need history through 2022 + warmup; use_max preferred when available.
    print("Loading daily data (deep history for stress window)...")
    try:
        data_full = _ensure_daily_data(1500, refresh=args.refresh, use_max=True)
    except TypeError:
        data_full = _ensure_daily_data(1500, refresh=args.refresh, use_max=False)

    data_start = str(data_full.index[0].date())
    data_end = str(data_full.index[-1].date())
    print(f"Data: {data_start} -> {data_end} ({len(data_full)} bars)")

    # If 2022 not covered, fall back to worst available calendar year by VTI return.
    idx0 = data_full.index[0]
    if stress_end < idx0 or stress_start > data_full.index[-1]:
        print("Requested 2022 window not in data — selecting worst calendar year by VTI...")
        spy_or_vti = None
        for col in ("VTI", "SPY", "Close"):
            if col in data_full.columns:
                spy_or_vti = data_full[col]
                break
        if spy_or_vti is None:
            # multiindex columns?
            spy_or_vti = data_full.iloc[:, 0]
        yearly = spy_or_vti.resample("YE").apply(
            lambda s: float(s.iloc[-1] / s.iloc[0] - 1.0) if len(s) > 5 else 0.0
        )
        worst = yearly.idxmin()
        year = int(worst.year)
        stress_start = pd.Timestamp(f"{year}-01-03")
        stress_end = pd.Timestamp(f"{year}-12-30")
        print(f"Fallback stress year: {year} (VTI/proxy {float(yearly.min()):+.1%})")

    frame, slice_note = _slice_stress_window(
        data_full, stress_start=stress_start, stress_end=stress_end
    )
    print(f"Stress frame: {frame.index[0].date()} -> {frame.index[-1].date()} ({len(frame)} bars)")
    print(f"Slice: {slice_note}")

    print(">>> stress STRICT leg")
    raw_stress = _run_strict(frame)
    # Attach benchmark if missing
    if isinstance(raw_stress, dict) and raw_stress.get("benchmark_return_pct") is None:
        try:
            warm = simulation_warmup_bars(len(frame))
            sim = frame.iloc[warm:]
            raw_stress["benchmark_return_pct"] = _benchmark_return(sim)
        except Exception:
            pass
    stress = _extract(raw_stress, label="stress_strict")
    print(
        f"    -> ret {_fmt_pct(stress.get('return_pct'))} Sharpe {_fmt_num(stress.get('sharpe'))} "
        f"MaxDD {_fmt_pct(stress.get('max_dd_pct'))}"
    )

    bull = None if args.rerun_bull90 else _load_bull_90_from_cache()
    if bull is None:
        print(">>> bull 90d STRICT leg (fresh)")
        data_90 = _ensure_daily_data(90, refresh=False, use_max=False)
        raw_bull = _run_strict(data_90)
        if isinstance(raw_bull, dict) and raw_bull.get("benchmark_return_pct") is None:
            try:
                warm = simulation_warmup_bars(len(data_90))
                raw_bull["benchmark_return_pct"] = _benchmark_return(data_90.iloc[warm:])
            except Exception:
                pass
        bull = _extract(raw_bull, label="bull_90d_strict")
        bull["source"] = "fresh_90d_run"
    else:
        print(f">>> bull 90d STRICT from cache ({bull.get('source')})")

    meta = {
        "requested_start": str(pd.Timestamp(args.stress_start).date()),
        "requested_end": str(pd.Timestamp(args.stress_end).date()),
        "effective_start": str(stress_start.date()),
        "effective_end": str(stress_end.date()),
        "slice_note": slice_note,
        "data_start": data_start,
        "data_end": data_end,
        "data_bars": len(data_full),
    }
    md = _write_report(stress=stress, bull=bull, stress_meta=meta)
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "disclaimer": DISCLAIMER,
        "stress_meta": meta,
        "stress": stress,
        "bull_90d": bull,
        "ok": bool(stress.get("ok")),
    }
    OUT_MD.write_text(md, encoding="utf-8")
    OUT_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(md.encode("ascii", errors="replace").decode("ascii"))
    print(f"\nWrote {OUT_MD.name}")
    return 0 if stress.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
