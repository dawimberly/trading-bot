"""Per-overlay STRICT A/B: baseline vs one research overlay at a time.

Legs (all paper-aggressive, no-thinking, hygiene ON, STRICT base):
  STRICT          — all lookahead overlays off
  +insider        — STRICT allow insider only
  +rvol           — STRICT allow RVOL only
  +catalyst       — STRICT allow catalyst only (no hist-news corpus)
  +hist_news      — STRICT allow historical news only

Usage (from stock-bot/):
  python scripts/analysis/eval_strict_overlay_ab.py
  python scripts/analysis/eval_strict_overlay_ab.py --days 90
  python scripts/analysis/eval_strict_overlay_ab.py --days 90 --legs strict,rvol

Writes:
  scripts/analysis/eval_strict_overlay_ab_last.md
  scripts/analysis/eval_strict_overlay_ab_last.json
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

OUT_MD = Path(__file__).with_name("eval_strict_overlay_ab_last.md")
OUT_JSON = Path(__file__).with_name("eval_strict_overlay_ab_last.json")

DISCLAIMER = (
    "Per-overlay deltas vs STRICT are diagnostic only; do not promote live from overlays alone"
)

# label -> allow list (empty = pure STRICT)
LEGS: list[tuple[str, list[str]]] = [
    ("STRICT", []),
    ("+insider", ["insider"]),
    ("+rvol", ["rvol"]),
    ("+catalyst", ["catalyst"]),
    ("+hist_news", ["hist_news"]),
]

LEG_ALIASES = {
    "strict": "STRICT",
    "baseline": "STRICT",
    "insider": "+insider",
    "+insider": "+insider",
    "rvol": "+rvol",
    "+rvol": "+rvol",
    "catalyst": "+catalyst",
    "+catalyst": "+catalyst",
    "hist_news": "+hist_news",
    "news": "+hist_news",
    "+hist_news": "+hist_news",
    "+news": "+hist_news",
}


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


def _extract_metrics(result: dict | None, label: str, allow: list[str]) -> dict[str, Any]:
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
        "strict_pit": True,
        "strict_pit_allow": list(allow),
        "nyse_entry_hygiene": None,
        "error": None,
        "lookahead_sources": list(allow),
        "active_overlays": list(allow),
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
    notes = "pure STRICT (hygiene ON)" if not allow else f"STRICT + {','.join(allow)} only"
    row.update(
        {
            "ok": bool(result.get("ok", True)),
            "verdict": "OK" if result.get("ok", True) else "HOLD",
            "return_pct": ret,
            "sharpe": sharpe,
            "max_dd_pct": max_dd,
            "trade_count": trades if trades is not None else 0,
            "nyse_fills": nyse if nyse is not None else 0,
            "strict_pit": bool(result.get("strict_pit", True)),
            "strict_pit_allow": list(result.get("strict_pit_allow") or allow),
            "nyse_entry_hygiene": bool(result.get("nyse_entry_hygiene")),
            "final_equity": _safe_float(result.get("final_equity")),
            "start_date": result.get("start_date"),
            "end_date": result.get("end_date"),
            "sim_days": result.get("sim_days"),
            "notes": notes,
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


def _run_leg(data, *, allow: list[str], paper_crypto_enabled: bool | None) -> dict:
    saved_env_flag = bool(config.STRICT_PIT_BACKTEST)
    saved_ctx = config.backtest_strict_pit_context()
    saved_allow = set(config.strict_pit_allow())
    saved_thinking = config.PAPER_THINKING_ENGINE_ENABLED
    try:
        config.STRICT_PIT_BACKTEST = True
        RUN_OPTIONS.strict_pit = True
        RUN_OPTIONS.no_thinking = True
        config.apply_strict_pit_kill_switches(allow=allow)
        try:
            config.assert_strict_pit_gates_off(allow=set(allow))
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc), "total_return_pct": None}
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
            strict_pit_allow=allow,
            paper_nyse_entry_hygiene=True,
            paper_crypto_enabled=paper_crypto_enabled,
            verbose=False,
        )
    finally:
        config.STRICT_PIT_BACKTEST = saved_env_flag
        config.set_backtest_strict_pit_context(saved_ctx)
        config.set_strict_pit_allow(saved_allow)
        config.PAPER_THINKING_ENGINE_ENABLED = saved_thinking
        RUN_OPTIONS.strict_pit = False
        RUN_OPTIONS.no_thinking = False


def _delta(row: dict, base: dict, key: str) -> float | None:
    a = row.get(key)
    b = base.get(key)
    if a is None or b is None:
        return None
    return float(a) - float(b)


def _verdict(rows: list[dict]) -> str:
    base = next((r for r in rows if r["label"] == "STRICT"), None)
    if not base or not base.get("ok"):
        return "HOLD - STRICT baseline failed metric parse; do not promote. " + DISCLAIMER
    parts: list[str] = []
    for row in rows:
        if row["label"] == "STRICT":
            continue
        if not row.get("ok"):
            parts.append(f"{row['label']}: HOLD (parse fail)")
            continue
        d_ret = _delta(row, base, "return_pct")
        d_sh = _delta(row, base, "sharpe")
        d_nyse = _delta(row, base, "nyse_fills")
        assert d_ret is not None and d_sh is not None
        tag = "noise"
        if d_ret > 1.0 or d_sh > 0.15:
            tag = "LOOKAHEAD-RISK uplift"
        elif d_ret < -1.0 or d_sh < -0.15:
            tag = "drag vs STRICT"
        elif abs(d_ret) < 0.5 and abs(d_sh) < 0.05:
            tag = "negligible"
        nyse_s = f", NYSE {d_nyse:+.0f}" if d_nyse is not None else ""
        parts.append(
            f"{row['label']}: ret {d_ret:+.2f}pp Sharpe {d_sh:+.2f}{nyse_s} ({tag})"
        )
    if not parts:
        return "Only STRICT ran. " + DISCLAIMER
    # Which overlay moved the needle most (abs return)?
    movers = []
    for row in rows:
        if row["label"] == "STRICT" or not row.get("ok"):
            continue
        d_ret = _delta(row, base, "return_pct")
        if d_ret is not None:
            movers.append((abs(d_ret), row["label"], d_ret))
    movers.sort(reverse=True)
    summary = " | ".join(parts)
    if movers and movers[0][0] >= 0.5:
        summary += (
            f" Largest |delta|: {movers[0][1]} ({movers[0][2]:+.2f}pp) "
            "- isolate before trusting FULL bundle."
        )
    else:
        summary += " No single overlay moves return much vs STRICT on this window."
    return summary + " " + DISCLAIMER


def _write_reports(
    *,
    days: int,
    window: str,
    bench: float | None,
    rows: list[dict],
    verdict: str,
) -> None:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    base = next((r for r in rows if r["label"] == "STRICT"), None)
    payload = {
        "generated_at": generated,
        "days": days,
        "window": window,
        "benchmark_return_pct": bench,
        "disclaimer": DISCLAIMER,
        "legs": {r["label"]: r for r in rows},
        "deltas_vs_strict": {},
        "verdict": verdict,
        "ok": all(bool(r.get("ok")) for r in rows),
    }
    if base and base.get("ok"):
        for r in rows:
            if r["label"] == "STRICT" or not r.get("ok"):
                continue
            payload["deltas_vs_strict"][r["label"]] = {
                "return_pp": _delta(r, base, "return_pct"),
                "sharpe": _delta(r, base, "sharpe"),
                "max_dd_pp": _delta(r, base, "max_dd_pct"),
                "trades": _delta(r, base, "trade_count"),
                "nyse_fills": _delta(r, base, "nyse_fills"),
            }
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines = [
        "# STRICT per-overlay A/B",
        "",
        f"Generated: {generated}",
        f"Window: {window} ({days} sim days requested)",
        f"Benchmark VTI B&H: {_fmt_pct(bench) if bench is not None else 'n/a'}",
        "",
        f"**{DISCLAIMER}**",
        "",
        "| Mode | Return | Sharpe | MaxDD | Trades | NYSE | vs STRICT ret | Notes |",
        "|------|--------|--------|-------|--------|------|---------------|-------|",
    ]
    for r in rows:
        status = "OK" if r.get("ok") else "HOLD"
        d_ret = _delta(r, base, "return_pct") if base and r["label"] != "STRICT" else None
        d_s = _fmt_pct(d_ret) if d_ret is not None else ("--" if r["label"] == "STRICT" else "n/a")
        lines.append(
            f"| {r['label']} ({status}) "
            f"| {_fmt_pct(r.get('return_pct'))} "
            f"| {_fmt_num(r.get('sharpe'))} "
            f"| {_fmt_pct(r.get('max_dd_pct'))} "
            f"| {_fmt_int(r.get('trade_count'))} "
            f"| {_fmt_int(r.get('nyse_fills'))} "
            f"| {d_s} "
            f"| {r.get('notes') or ''} |"
        )
    lines.extend(["", "## Verdict", "", verdict, ""])
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


def _select_legs(raw: str | None) -> list[tuple[str, list[str]]]:
    if not raw or not str(raw).strip():
        return list(LEGS)
    wanted: list[str] = []
    for part in str(raw).split(","):
        key = LEG_ALIASES.get(part.strip().lower())
        if not key:
            raise SystemExit(f"Unknown leg {part!r}. Choose from: {', '.join(LEG_ALIASES)}")
        if key not in wanted:
            wanted.append(key)
    # Always include STRICT first if any overlay leg present
    if "STRICT" not in wanted and any(w != "STRICT" for w in wanted):
        wanted.insert(0, "STRICT")
    by_label = {label: allow for label, allow in LEGS}
    return [(lab, by_label[lab]) for lab in wanted]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="STRICT per-overlay A/B (insider / RVOL / catalyst / hist-news)"
    )
    ap.add_argument("--days", type=int, default=90, help="Sim days (default 90)")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument(
        "--legs",
        default="",
        help="Comma list: strict,insider,rvol,catalyst,hist_news (default: all)",
    )
    args = ap.parse_args()
    days = max(20, int(args.days))
    legs = _select_legs(args.legs)
    paper_crypto = False

    print(f"--- STRICT per-overlay A/B ({days}d) ---")
    print(f"Legs: {', '.join(lab for lab, _ in legs)}")
    print("All legs: paper-aggressive, no-thinking, hygiene ON, STRICT base")
    data = _ensure_daily_data(days, refresh=args.refresh, use_max=False)
    if len(data) < 20:
        print(f"Need at least 20 daily bars; got {len(data)}.")
        return 1

    warmup = min(MIN_HISTORY, max(0, len(data) - 5))
    window = f"{data.index[warmup].date()} -> {data.index[-1].date()}"
    bench = _benchmark_return(data, warmup)
    print(f"Window: {window} ({len(data) - warmup} sim bars)")
    if bench is not None:
        print(f"VTI buy & hold benchmark: {bench:+.2f}%")

    rows: list[dict] = []
    for label, allow in legs:
        print(f"\n>>> Running {label} ...")
        raw = _run_leg(data, allow=allow, paper_crypto_enabled=paper_crypto)
        rows.append(_extract_metrics(raw, label, allow))

    print(
        f"\n{'Mode':<14} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'Trades':>7} {'NYSE':>6}"
    )
    print("-" * 58)
    for row in rows:
        print(
            f"{row['label']:<14} "
            f"{_fmt_pct(row.get('return_pct')):>8} "
            f"{_fmt_num(row.get('sharpe')):>7} "
            f"{_fmt_pct(row.get('max_dd_pct')):>8} "
            f"{_fmt_int(row.get('trade_count')):>7} "
            f"{_fmt_int(row.get('nyse_fills')):>6}"
        )
    print("-" * 58)
    print(DISCLAIMER)

    verdict = _verdict(rows)
    _write_reports(days=days, window=window, bench=bench, rows=rows, verdict=verdict)
    print("\n## Verdict")
    try:
        print(verdict)
    except UnicodeEncodeError:
        print(verdict.encode("ascii", "replace").decode("ascii"))
    print(f"\nWrote {OUT_MD.name} and {OUT_JSON.name}")
    return 0 if all(r.get("ok") for r in rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
