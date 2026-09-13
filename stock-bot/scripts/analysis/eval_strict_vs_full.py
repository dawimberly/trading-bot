"""STRICT PIT vs FULL overlays — evaluation honesty for Realistic Research paper.

Runs the same window twice under paper-aggressive + --no-thinking:
  STRICT: insider / RVOL / catalyst / hist-news / LLM forced OFF; hygiene ON
  FULL:   research overlays as currently default ON (not point-in-time)

Usage (from stock-bot/):
  python scripts/analysis/eval_strict_vs_full.py
  python scripts/analysis/eval_strict_vs_full.py --days 90
  python scripts/analysis/eval_strict_vs_full.py --days 365

Writes:
  scripts/analysis/eval_strict_vs_full_last.md
  scripts/analysis/eval_strict_vs_full_last.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import os

# Quiet deploy spam so dual 90d legs finish in reasonable time (no .env rewrite).
os.environ.setdefault("PAPER_DEPLOY_DEBUG", "false")
os.environ.setdefault("PYTHONUNBUFFERED", "1")

import config

config.PAPER_DEPLOY_DEBUG = False

from backtester import MIN_HISTORY, _benchmark_return, _ensure_daily_data, run_backtest
from modules.backtester_core import RUN_OPTIONS

OUT_MD = Path(__file__).with_name("eval_strict_vs_full_last.md")
OUT_JSON = Path(__file__).with_name("eval_strict_vs_full_last.json")

DISCLAIMER = (
    "FULL is not point-in-time; do not promote live from FULL alone"
)

# Documented FULL-side soft/hard overlay risk (STRICT kills these by default).
FULL_LOOKAHEAD_SOURCES = [
    "insider",
    "rvol",
    "catalyst",
    "hist_news",
    "dyn_univ",
    "buffett",
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


def _extract_metrics(result: dict | None, label: str) -> dict[str, Any]:
    """Parse backtest result; on failure set ok=False (no fake zeros)."""
    row: dict[str, Any] = {
        "label": label,
        "ok": False,
        "verdict": "HOLD",
        "return_pct": None,
        "sharpe": None,
        "max_dd_pct": None,
        "trade_count": None,
        "nyse_fills": None,
        "notes": "",
        "strict_pit": None,
        "nyse_entry_hygiene": None,
        "error": None,
        "lookahead_sources": [],
        "active_overlays": [],
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
    is_strict = "STRICT" in label.upper()
    if ret is None or sharpe is None or max_dd is None:
        row["error"] = "metrics_parse_failed"
        row["notes"] = "metrics parse failed - HOLD (no fake zeros)"
        row["return_pct"] = ret
        row["sharpe"] = sharpe
        row["max_dd_pct"] = max_dd
        row["trade_count"] = trades
        row["nyse_fills"] = nyse
        row["lookahead_sources"] = [] if is_strict else list(FULL_LOOKAHEAD_SOURCES)
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
            "lookahead_sources": [] if is_strict else list(FULL_LOOKAHEAD_SOURCES),
            "active_overlays": [] if is_strict else list(FULL_LOOKAHEAD_SOURCES),
        }
    )
    if label.upper().startswith("STRICT"):
        row["notes"] = "point-in-time kill switches; hygiene ON"
    else:
        row["notes"] = "current overlays (lookahead risk on insider/RVOL/catalyst/news)"
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


def _run_leg(
    data,
    *,
    strict: bool,
    paper_crypto_enabled: bool | None,
) -> dict:
    # Isolate STRICT_PIT_BACKTEST env so STRICT→FULL in one process works.
    saved_env_flag = bool(config.STRICT_PIT_BACKTEST)
    saved_ctx = config.backtest_strict_pit_context()
    saved_thinking = config.PAPER_THINKING_ENGINE_ENABLED
    try:
        if strict:
            config.STRICT_PIT_BACKTEST = True
            RUN_OPTIONS.strict_pit = True
            RUN_OPTIONS.no_thinking = True
            config.apply_strict_pit_kill_switches()
        else:
            config.STRICT_PIT_BACKTEST = False
            RUN_OPTIONS.strict_pit = False
            RUN_OPTIONS.no_thinking = True
            config.set_backtest_strict_pit_context(False)
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
            paper_crypto_enabled=paper_crypto_enabled,
            verbose=False,
        )
    finally:
        config.STRICT_PIT_BACKTEST = saved_env_flag
        config.set_backtest_strict_pit_context(saved_ctx)
        config.PAPER_THINKING_ENGINE_ENABLED = saved_thinking
        RUN_OPTIONS.strict_pit = False
        RUN_OPTIONS.no_thinking = False


def _verdict(strict_row: dict, full_row: dict) -> str:
    if not strict_row.get("ok") or not full_row.get("ok"):
        return (
            "HOLD — one or both legs failed metric parse; do not promote. "
            + DISCLAIMER
        )
    d_ret = float(full_row["return_pct"]) - float(strict_row["return_pct"])
    d_sh = float(full_row["sharpe"]) - float(strict_row["sharpe"])
    d_dd = float(full_row["max_dd_pct"]) - float(strict_row["max_dd_pct"])
    d_nyse = int(full_row["nyse_fills"]) - int(strict_row["nyse_fills"])
    lines = [
        f"FULL - STRICT: return {d_ret:+.2f}pp | Sharpe {d_sh:+.2f} | "
        f"MaxDD {d_dd:+.2f}pp | NYSE fills {d_nyse:+d}.",
    ]
    # Heuristic: large positive FULL edge with more fills -> overlay lookahead risk.
    if abs(d_ret) < 0.5 and abs(d_sh) < 0.05:
        lines.append(
            "Deltas are small -> overlays add little; STRICT price-path is the credible baseline."
        )
    elif d_ret > 1.0 or d_sh > 0.15:
        lines.append(
            "FULL looks better mainly via overlay stack (insider/RVOL/catalyst/news) - "
            "treat as lookahead-tainted uplift, not proven live edge."
        )
    elif d_ret < -1.0 or d_sh < -0.15:
        lines.append(
            "STRICT outperforms FULL -> overlays may be noise/drag; price-path edge is in STRICT."
        )
    else:
        lines.append(
            "Modest delta -> mix of overlay effects and path noise; promote only from STRICT."
        )
    lines.append(DISCLAIMER)
    return " ".join(lines)


def _write_reports(
    *,
    days: int,
    window: str,
    bench: float | None,
    strict_row: dict,
    full_row: dict,
    verdict: str,
) -> None:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    payload = {
        "generated_at": generated,
        "days": days,
        "window": window,
        "benchmark_return_pct": bench,
        "disclaimer": DISCLAIMER,
        "legs": {"STRICT": strict_row, "FULL": full_row},
        "verdict": verdict,
        "ok": bool(strict_row.get("ok") and full_row.get("ok")),
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines = [
        "# STRICT PIT vs FULL overlays",
        "",
        f"Generated: {generated}",
        f"Window: {window} ({days} sim days requested)",
        f"Benchmark VTI B&H: {_fmt_pct(bench) if bench is not None else 'n/a'}",
        "",
        f"**{DISCLAIMER}**",
        "",
        "| Mode | Return | Sharpe | MaxDD | Trades | NYSE fills | Notes |",
        "|------|--------|--------|-------|--------|------------|-------|",
    ]
    for row in (strict_row, full_row):
        status = "OK" if row.get("ok") else "HOLD"
        lines.append(
            f"| {row['label']} ({status}) "
            f"| {_fmt_pct(row.get('return_pct'))} "
            f"| {_fmt_num(row.get('sharpe'))} "
            f"| {_fmt_pct(row.get('max_dd_pct'))} "
            f"| {_fmt_int(row.get('trade_count'))} "
            f"| {_fmt_int(row.get('nyse_fills'))} "
            f"| {row.get('notes') or ''} |"
        )
    lines.extend(["", "## Verdict", "", verdict, ""])
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Dual eval: STRICT PIT vs FULL research overlays (paper only)"
    )
    ap.add_argument("--days", type=int, default=90, help="Sim days (default 90; try 365 after 90 is stable)")
    ap.add_argument("--refresh", action="store_true", help="Refresh price cache")
    args = ap.parse_args()
    days = max(20, int(args.days))

    # Paper crypto off keeps both legs comparable / faster (match typical paper A/B).
    paper_crypto = False

    print(f"--- STRICT vs FULL dual eval ({days}d) ---")
    print("Both legs: --paper-aggressive --no-thinking; STRICT implies overlay kill switches")
    data = _ensure_daily_data(days, refresh=args.refresh, use_max=False)
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
        fail = _extract_metrics(None, "STRICT PIT")
        fail2 = _extract_metrics(None, "FULL overlays")
        verdict = _verdict(fail, fail2)
        _write_reports(
            days=days,
            window="n/a",
            bench=None,
            strict_row=fail,
            full_row=fail2,
            verdict=verdict,
        )
        print(verdict)
        return 1

    warmup = min(MIN_HISTORY, max(0, len(data) - 5))
    window = f"{data.index[warmup].date()} -> {data.index[-1].date()}"
    bench = _benchmark_return(data, warmup)
    print(f"Window: {window} ({len(data) - warmup} sim bars)")
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")

    print("\n>>> Running STRICT PIT ...")
    strict_raw = _run_leg(data, strict=True, paper_crypto_enabled=paper_crypto)
    strict_row = _extract_metrics(strict_raw, "STRICT PIT")

    print("\n>>> Running FULL overlays ...")
    full_raw = _run_leg(data, strict=False, paper_crypto_enabled=paper_crypto)
    full_row = _extract_metrics(full_raw, "FULL overlays")

    print(
        f"{'Mode':<16} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Trades':>7} {'NYSE':>6}"
    )
    print("-" * 60)
    for row in (strict_row, full_row):
        print(
            f"{row['label']:<16} "
            f"{_fmt_pct(row.get('return_pct')):>8} "
            f"{_fmt_num(row.get('sharpe')):>7} "
            f"{_fmt_pct(row.get('max_dd_pct')):>8} "
            f"{_fmt_int(row.get('trade_count')):>7} "
            f"{_fmt_int(row.get('nyse_fills')):>6}"
        )
    print("-" * 60)
    print(DISCLAIMER)

    verdict = _verdict(strict_row, full_row)
    _write_reports(
        days=days,
        window=window,
        bench=bench,
        strict_row=strict_row,
        full_row=full_row,
        verdict=verdict,
    )
    print("\n## Verdict")
    try:
        print(verdict)
    except UnicodeEncodeError:
        print(verdict.encode("ascii", "replace").decode("ascii"))
    print(f"\nWrote {OUT_MD.name} and {OUT_JSON.name}")
    return 0 if (strict_row.get("ok") and full_row.get("ok")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
