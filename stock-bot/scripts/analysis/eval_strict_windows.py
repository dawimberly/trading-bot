"""Multi-window STRICT PIT panel (price-path credibility across horizons).

Runs paper-aggressive STRICT (hygiene ON, no-thinking, overlays off) on each window.
Default windows: 90, 180, 365.

Usage (from stock-bot/):
  python scripts/analysis/eval_strict_windows.py
  python scripts/analysis/eval_strict_windows.py --windows 90,180
  python scripts/analysis/eval_strict_windows.py --windows 90,180,365 --with-full

Writes:
  scripts/analysis/eval_strict_windows_last.md
  scripts/analysis/eval_strict_windows_last.json
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

os.environ.setdefault("PAPER_DEPLOY_DEBUG", "false")
os.environ.setdefault("PYTHONUNBUFFERED", "1")

import config

config.PAPER_DEPLOY_DEBUG = False

from backtester import MIN_HISTORY, _benchmark_return, _ensure_daily_data, run_backtest
from modules.backtester_core import RUN_OPTIONS

OUT_MD = Path(__file__).with_name("eval_strict_windows_last.md")
OUT_JSON = Path(__file__).with_name("eval_strict_windows_last.json")

DISCLAIMER = (
    "Promote from STRICT across windows only; FULL is not point-in-time"
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


def _extract(result: dict | None, *, label: str, days: int, mode: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "label": label,
        "days": days,
        "mode": mode,
        "ok": False,
        "verdict": "HOLD",
        "return_pct": None,
        "sharpe": None,
        "max_dd_pct": None,
        "trade_count": None,
        "nyse_fills": None,
        "notes": "",
        "error": None,
        "lookahead_sources": [] if mode == "STRICT" else [
            "insider",
            "rvol",
            "catalyst",
            "hist_news",
            "dyn_univ",
            "buffett",
        ],
        "active_overlays": [] if mode == "STRICT" else ["research_overlays_default"],
    }
    if not isinstance(result, dict):
        row["error"] = "missing_result"
        row["notes"] = "metrics parse failed"
        return row
    ret = _safe_float(result.get("total_return_pct"))
    sharpe = _safe_float(result.get("sharpe"))
    max_dd = _safe_float(result.get("max_drawdown_pct"))
    trades = _safe_int(result.get("total_orders"))
    nyse = _safe_int(result.get("nyse_signals"))
    if ret is None or sharpe is None or max_dd is None:
        row["error"] = "metrics_parse_failed"
        row["notes"] = "metrics parse failed - HOLD (no fake zeros)"
        row["return_pct"] = ret
        row["sharpe"] = sharpe
        row["max_dd_pct"] = max_dd
        row["trade_count"] = trades
        row["nyse_fills"] = nyse
        return row
    row.update(
        {
            "ok": bool(result.get("ok", True)),
            "verdict": "OK" if result.get("ok", True) else "HOLD",
            "return_pct": ret,
            "sharpe": sharpe,
            "max_dd_pct": max_dd,
            "trade_count": trades if trades is not None else 0,
            "nyse_fills": nyse if nyse is not None else 0,
            "strict_pit": bool(result.get("strict_pit")),
            "nyse_entry_hygiene": bool(result.get("nyse_entry_hygiene")),
            "final_equity": _safe_float(result.get("final_equity")),
            "start_date": result.get("start_date"),
            "end_date": result.get("end_date"),
            "sim_days": result.get("sim_days"),
            "window": f"{result.get('start_date')} -> {result.get('end_date')}",
            "notes": "STRICT PIT hygiene ON" if mode == "STRICT" else "FULL overlays (not PIT)",
            "benchmark_return_pct": _safe_float(result.get("benchmark_return_pct")),
        }
    )
    return row


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.2f}%"


def _fmt_num(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "n/a"
    return f"{v:.{digits}f}"


def _fmt_int(v: int | None) -> str:
    if v is None:
        return "n/a"
    return str(v)


def _run_leg(data, *, strict: bool) -> dict:
    saved_env = bool(config.STRICT_PIT_BACKTEST)
    saved_ctx = config.backtest_strict_pit_context()
    saved_allow = set(config.strict_pit_allow())
    saved_thinking = config.PAPER_THINKING_ENGINE_ENABLED
    try:
        if strict:
            config.STRICT_PIT_BACKTEST = True
            RUN_OPTIONS.strict_pit = True
            RUN_OPTIONS.no_thinking = True
            config.apply_strict_pit_kill_switches(allow=None)
        else:
            config.STRICT_PIT_BACKTEST = False
            RUN_OPTIONS.strict_pit = False
            RUN_OPTIONS.no_thinking = True
            config.set_backtest_strict_pit_context(False)
            config.clear_strict_pit_allow()
            config.PAPER_THINKING_ENGINE_ENABLED = False
        return run_backtest(
            data,
            track_active_exposure=True,
            track_metrics=True,
            paper_aggressive=True,
            paper_sleeve_features=True,
            paper_dynamic_vti=True,
            paper_thinking=False,
            with_news=False,
            strict_pit=strict,
            paper_nyse_entry_hygiene=True if strict else None,
            paper_crypto_enabled=False,
            verbose=False,
        )
    finally:
        config.STRICT_PIT_BACKTEST = saved_env
        config.set_backtest_strict_pit_context(saved_ctx)
        config.set_strict_pit_allow(saved_allow)
        config.PAPER_THINKING_ENGINE_ENABLED = saved_thinking
        RUN_OPTIONS.strict_pit = False
        RUN_OPTIONS.no_thinking = False


def _verdict(rows: list[dict]) -> str:
    strict_rows = [r for r in rows if r.get("mode") == "STRICT"]
    if not strict_rows or not all(r.get("ok") for r in strict_rows):
        return "HOLD - one or more STRICT windows failed parse; do not promote. " + DISCLAIMER
    sharpes = [float(r["sharpe"]) for r in strict_rows]
    rets = [float(r["return_pct"]) for r in strict_rows]
    dds = [float(r["max_dd_pct"]) for r in strict_rows]
    sh_min, sh_max = min(sharpes), max(sharpes)
    parts = [
        f"STRICT Sharpe range {sh_min:.2f}-{sh_max:.2f} across {len(strict_rows)} windows; "
        f"return {min(rets):+.2f}% to {max(rets):+.2f}%; "
        f"MaxDD {min(dds):+.2f}% to {max(dds):+.2f}%."
    ]
    if sh_min < 0.5 or any(r < 0 for r in rets):
        parts.append("Unstable / weak on at least one window - do not promote.")
    elif (sh_max - sh_min) > 1.0:
        parts.append("Sharpe varies a lot by window - treat as regime-sensitive, need more folds.")
    else:
        parts.append("STRICT edge is directionally consistent across windows (still paper-only).")
    for r in rows:
        if r.get("mode") != "FULL" or not r.get("ok"):
            continue
        s = next((x for x in strict_rows if x["days"] == r["days"]), None)
        if not s or not s.get("ok"):
            continue
        d_ret = float(r["return_pct"]) - float(s["return_pct"])
        parts.append(f"{r['days']}d FULL-STRICT ret {d_ret:+.2f}pp.")
    parts.append(DISCLAIMER)
    return " ".join(parts)


def _write_reports(*, windows: list[int], rows: list[dict], verdict: str) -> None:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    payload = {
        "generated_at": generated,
        "windows": windows,
        "disclaimer": DISCLAIMER,
        "legs": rows,
        "verdict": verdict,
        "ok": all(bool(r.get("ok")) for r in rows),
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines = [
        "# STRICT multi-window panel",
        "",
        f"Generated: {generated}",
        f"Windows: {', '.join(str(w) for w in windows)}",
        "",
        f"**{DISCLAIMER}**",
        "",
        "| Days | Mode | Return | Sharpe | MaxDD | Trades | NYSE | Window |",
        "|------|------|--------|--------|-------|--------|------|--------|",
    ]
    for r in rows:
        status = "OK" if r.get("ok") else "HOLD"
        lines.append(
            f"| {r.get('days')} "
            f"| {r.get('mode')} ({status}) "
            f"| {_fmt_pct(r.get('return_pct'))} "
            f"| {_fmt_num(r.get('sharpe'))} "
            f"| {_fmt_pct(r.get('max_dd_pct'))} "
            f"| {_fmt_int(r.get('trade_count'))} "
            f"| {_fmt_int(r.get('nyse_fills'))} "
            f"| {r.get('window') or 'n/a'} |"
        )
    lines.extend(["", "## Verdict", "", verdict, ""])
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Multi-window STRICT PIT panel")
    ap.add_argument(
        "--windows",
        default="90,180,365",
        help="Comma list of sim-day windows (default 90,180,365)",
    )
    ap.add_argument(
        "--with-full",
        action="store_true",
        help="Also run FULL overlays per window (2x runtime)",
    )
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    windows = [max(20, int(x.strip())) for x in str(args.windows).split(",") if x.strip()]
    if not windows:
        raise SystemExit("No windows specified")

    print(f"--- STRICT multi-window panel: {windows} ---")
    if args.with_full:
        print("Also running FULL overlays per window")
    print("STRICT: paper-aggressive, no-thinking, hygiene ON, overlays off")

    rows: list[dict] = []
    for days in windows:
        print(f"\n=== Window {days}d ===")
        data = _ensure_daily_data(days, refresh=args.refresh, use_max=False)
        if len(data) < 20:
            print(f"Skip {days}d: need >=20 bars, got {len(data)}")
            rows.append(_extract(None, label=f"STRICT_{days}", days=days, mode="STRICT"))
            continue
        warmup = min(MIN_HISTORY, max(0, len(data) - 5))
        print(
            f"Bars: {data.index[warmup].date()} -> {data.index[-1].date()} "
            f"({len(data) - warmup} sim)"
        )
        bench = _benchmark_return(data, warmup)
        if bench is not None:
            print(f"VTI B&H: {bench:+.2f}%")

        print(f">>> STRICT {days}d ...")
        strict_raw = _run_leg(data, strict=True)
        strict_row = _extract(strict_raw, label=f"STRICT_{days}", days=days, mode="STRICT")
        if strict_row.get("ok") and bench is not None and strict_row.get("benchmark_return_pct") is None:
            strict_row["benchmark_return_pct"] = bench
        rows.append(strict_row)
        print(
            f"STRICT {days}d: {_fmt_pct(strict_row.get('return_pct'))} "
            f"Sharpe {_fmt_num(strict_row.get('sharpe'))} "
            f"MaxDD {_fmt_pct(strict_row.get('max_dd_pct'))}"
        )

        if args.with_full:
            print(f">>> FULL {days}d ...")
            full_raw = _run_leg(data, strict=False)
            full_row = _extract(full_raw, label=f"FULL_{days}", days=days, mode="FULL")
            rows.append(full_row)
            print(
                f"FULL {days}d: {_fmt_pct(full_row.get('return_pct'))} "
                f"Sharpe {_fmt_num(full_row.get('sharpe'))} "
                f"MaxDD {_fmt_pct(full_row.get('max_dd_pct'))}"
            )

    print(
        f"\n{'Days':>5} {'Mode':<8} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Trades':>7} {'NYSE':>6}"
    )
    print("-" * 56)
    for r in rows:
        print(
            f"{r.get('days'):>5} {r.get('mode'):<8} "
            f"{_fmt_pct(r.get('return_pct')):>8} "
            f"{_fmt_num(r.get('sharpe')):>7} "
            f"{_fmt_pct(r.get('max_dd_pct')):>8} "
            f"{_fmt_int(r.get('trade_count')):>7} "
            f"{_fmt_int(r.get('nyse_fills')):>6}"
        )
    print("-" * 56)

    verdict = _verdict(rows)
    _write_reports(windows=windows, rows=rows, verdict=verdict)
    print("\n## Verdict")
    try:
        print(verdict)
    except UnicodeEncodeError:
        print(verdict.encode("ascii", "replace").decode("ascii"))
    print(f"\nWrote {OUT_MD.name} and {OUT_JSON.name}")
    return 0 if all(r.get("ok") for r in rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
