"""STRICT exit ladder + SPY-off A/B (paper-aggressive, hygiene ON).

Paper/research only. Overlays OFF (STRICT PIT).

Leg A — baseline: current paper defaults (Dyn VTI ON, normal SPY sleeve).
Leg B — exits grid: hold 20/30/45 x trail base vs tight (~6 legs).
Leg C — SPY satellite OFF (SPY_SLEEVE_CAP_PCT=0, keep Dynamic VTI).

Usage (from stock-bot/):
  python scripts/analysis/eval_exit_spy_ab.py --days 90
  python scripts/analysis/eval_exit_spy_ab.py --days 90 --refresh

Writes:
  scripts/analysis/exit_spy_ab_{days}_last.md
  scripts/analysis/exit_spy_ab_{days}_last.json
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

DISCLAIMER = "STRICT research only; no live Profile A changes"
# Promote only if return AND Sharpe beat baseline and MaxDD not much worse.
MAX_DD_WORSE_PP = 1.0


def _out_paths(days: int) -> tuple[Path, Path]:
    tag = int(days)
    base = Path(__file__).with_name(f"exit_spy_ab_{tag}_last")
    return base.with_suffix(".md"), base.with_suffix(".json")

# Paper trailing defaults (position_exits / paper_risk_controls)
TRAIL_BASE = (0.10, 0.05)  # arm, trail
TRAIL_TIGHT = (0.08, 0.04)


@dataclass(frozen=True)
class Leg:
    name: str
    group: str  # A | B | C
    config_label: str
    notes: str
    max_hold_bars: int
    trail_arm: float
    trail_pull: float
    spy_cap: float | None = None  # None = paper default


def _build_legs() -> list[Leg]:
    legs: list[Leg] = [
        Leg(
            "baseline",
            "A",
            "defaults (hold=30 arm=10% trail=5% SPY cap ON)",
            "STRICT paper defaults; Dyn VTI ON; hygiene ON",
            max_hold_bars=30,
            trail_arm=TRAIL_BASE[0],
            trail_pull=TRAIL_BASE[1],
        ),
    ]
    for hold in (20, 30, 45):
        for tag, (arm, pull) in (("base", TRAIL_BASE), ("tight", TRAIL_TIGHT)):
            if hold == 30 and tag == "base":
                continue  # same as baseline
            legs.append(
                Leg(
                    f"exit_h{hold}_{tag}",
                    "B",
                    f"hold={hold} arm={arm:.0%} trail={pull:.0%}",
                    f"exit ladder hold {hold}d, {tag} trail",
                    max_hold_bars=hold,
                    trail_arm=arm,
                    trail_pull=pull,
                )
            )
    legs.append(
        Leg(
            "spy_off",
            "C",
            "SPY cap=0% (Dyn VTI ON)",
            "SPY satellite disabled; Dynamic VTI core unchanged",
            max_hold_bars=30,
            trail_arm=TRAIL_BASE[0],
            trail_pull=TRAIL_BASE[1],
            spy_cap=0.0,
        )
    )
    return legs


LEGS = _build_legs()


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
        "PAPER_POSITION_MAX_HOLD_BARS",
        "PAPER_TRAILING_STOP_ARM_PCT",
        "PAPER_TRAILING_STOP_TRAIL_PCT",
        "SPY_SLEEVE_CAP_PCT",
        "PAPER_SPY_MAX_EXPOSURE_PCT",
        "STRICT_PIT_BACKTEST",
    ]
    saved = {k: getattr(config, k) for k in keys}
    saved["thinking"] = config.PAPER_THINKING_ENGINE_ENABLED
    saved["strict_ctx"] = config.backtest_strict_pit_context()
    saved["strict_allow"] = set(config.strict_pit_allow())
    try:
        config.PAPER_DYNAMIC_VTI_ENABLED = True
        config.DYNAMIC_VTI_PAPER_FLOOR = 0.40
        config.DYNAMIC_VTI_PAPER_CEILING = 0.75
        config.DYNAMIC_VTI_FLOOR_MIN = 0.40
        config.DYNAMIC_VTI_ALLOW_ZERO = False
        config.DYNAMIC_VTI_OPTIONAL_ENABLED = False
        config.PAPER_POSITION_MAX_HOLD_BARS = int(leg.max_hold_bars)
        config.PAPER_TRAILING_STOP_ARM_PCT = float(leg.trail_arm)
        config.PAPER_TRAILING_STOP_TRAIL_PCT = float(leg.trail_pull)
        if leg.spy_cap is not None:
            config.SPY_SLEEVE_CAP_PCT = float(leg.spy_cap)
            config.PAPER_SPY_MAX_EXPOSURE_PCT = float(leg.spy_cap)
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


def _extract(result: dict | None, leg: Leg, *, strict_banner: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": leg.name,
        "group": leg.group,
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
    return f"{int(a) - int(b):+d}" if isinstance(a, int) else f"{float(a) - float(b):+.2f}"


def _promote_ok(row: dict, base: dict) -> bool:
    """Return AND Sharpe beat baseline; MaxDD not worse by more than MAX_DD_WORSE_PP."""
    if not base.get("ok") or not row.get("ok") or row.get("name") == "baseline":
        return False
    ret_ok = float(row["return_pct"]) > float(base["return_pct"])
    sh_ok = float(row.get("sharpe") or -999) > float(base.get("sharpe") or -999)
    # MaxDD is negative; "worse" means more negative (lower).
    dd_row = float(row.get("max_dd_pct") or 0)
    dd_base = float(base.get("max_dd_pct") or 0)
    dd_ok = dd_row >= (dd_base - MAX_DD_WORSE_PP)
    return ret_ok and sh_ok and dd_ok


def _verdict(rows: list[dict], base: dict | None, *, days: int) -> str:
    ok = [r for r in rows if r.get("ok")]
    if not ok:
        return f"HOLD — no successful legs. {DISCLAIMER}"
    best_b = max(
        (r for r in ok if r.get("group") == "B"),
        key=lambda r: (float(r["return_pct"]), float(r["sharpe"] or -999)),
        default=None,
    )
    spy = next((r for r in ok if r["name"] == "spy_off"), None)
    parts: list[str] = []
    if best_b and base and base.get("ok"):
        d = float(best_b["return_pct"]) - float(base["return_pct"])
        parts.append(
            f"Best exit leg {best_b['name']} ({best_b['config']}): "
            f"{_fmt_pct(best_b.get('return_pct'))} Sharpe {_fmt_num(best_b.get('sharpe'))} "
            f"vs baseline {d:+.2f}pp."
        )
    if spy and base and base.get("ok"):
        d = float(spy["return_pct"]) - float(base["return_pct"])
        parts.append(
            f"SPY-off: {_fmt_pct(spy.get('return_pct'))} Sharpe {_fmt_num(spy.get('sharpe'))} "
            f"SPY fills {spy.get('spy_fills')} vs baseline {d:+.2f}pp."
        )
    if base and base.get("ok"):
        promote = [r for r in ok if _promote_ok(r, base)]
    else:
        promote = []
    if days >= 300:
        if promote:
            parts.append(
                f"PAPER DEFAULT CANDIDATE (365d): {', '.join(r['name'] for r in promote)} "
                f"— beat baseline return+Sharpe with MaxDD within {MAX_DD_WORSE_PP:.1f}pp. "
                "Discuss paper default change only; no live Profile A change yet. No combo until singles clear."
            )
        else:
            parts.append(
                "No leg beats baseline on return AND Sharpe with MaxDD intact on 365d — "
                "do not change paper defaults."
            )
    else:
        if promote:
            parts.append(
                f"Queue 365d STRICT confirm: {', '.join(r['name'] for r in promote)}."
            )
        else:
            parts.append(f"No leg clearly beats baseline on {days}d — do not promote.")
    parts.append(DISCLAIMER)
    return " ".join(parts)


def _write(rows, *, days, window, bench, verdict, strict_banner, out_md: Path, out_json: Path) -> None:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    base = next((r for r in rows if r["name"] == "baseline"), None)
    payload = {
        "generated_at": generated,
        "days": days,
        "window": window,
        "benchmark_return_pct": bench,
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
    title = f"# STRICT exit ladder + SPY-off A/B ({days}d)"
    if days >= 300:
        title = f"# STRICT 365d confirm — exit + SPY-off"
    lines = [
        title,
        "",
        f"Generated: {generated}",
        f"Window: {window} ({days}d)",
        f"Benchmark VTI B&H: {_fmt_pct(bench)}",
        "",
        f"**{strict_banner}**",
        "",
        f"**{DISCLAIMER}**",
        "",
        "| Leg | Config | Return | Sharpe | MaxDD | Trades | SPY fills | NYSE fills | vs baseline ret |",
        "|-----|--------|--------|--------|-------|--------|-----------|------------|-----------------|",
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
    ap = argparse.ArgumentParser(description="STRICT exit + SPY-off A/B")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--legs", default="", help="Comma subset of leg names")
    ap.add_argument(
        "--resume",
        action="store_true",
        help="Merge prior exit_spy_ab_{days}_last.json rows for legs not re-run",
    )
    args = ap.parse_args()
    days = max(20, int(args.days))
    out_md, out_json = _out_paths(days)
    wanted = {x.strip() for x in args.legs.split(",") if x.strip()}
    legs = [L for L in LEGS if not wanted or L.name in wanted]
    if not legs:
        raise SystemExit(f"Choose from: {[L.name for L in LEGS]}")

    print(f"--- STRICT exit + SPY-off A/B ({days}d) ---")
    print(f"Legs ({len(legs)}):", ", ".join(L.name for L in legs))
    print(f"Output: {out_md.name}")
    data = _ensure_daily_data(days, refresh=args.refresh, use_max=False)
    warmup = min(MIN_HISTORY, max(0, len(data) - 5))
    window = f"{data.index[warmup].date()} -> {data.index[-1].date()}"
    bench = _benchmark_return(data, warmup)
    print(f"Window: {window}")
    if bench is not None:
        print(f"VTI B&H: {bench:+.2f}%")

    prior_by_name: dict[str, dict] = {}
    if args.resume and out_json.is_file():
        try:
            prior = json.loads(out_json.read_text(encoding="utf-8"))
            for r in prior.get("legs") or []:
                if isinstance(r, dict) and r.get("name"):
                    prior_by_name[str(r["name"])] = r
            print(f"Resume: loaded {len(prior_by_name)} prior leg(s)")
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Resume: ignored prior JSON ({exc})")

    rows: list[dict] = []
    strict_banner = ""
    for leg in legs:
        print(f"\n>>> [{leg.group}] {leg.name}: {leg.config_label}", flush=True)
        with _apply_leg(leg):
            strict_banner = config.format_strict_pit_banner()
            print(f"    {strict_banner}", flush=True)
            raw = run_backtest(
                data,
                track_active_exposure=True,
                track_metrics=True,
                paper_aggressive=True,
                paper_sleeve_features=True,
                paper_dynamic_vti=True,
                vti_core_pct=0.0,
                paper_thinking=False,
                strict_pit=True,
                paper_nyse_entry_hygiene=True,
                paper_crypto_enabled=False,
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
        # Checkpoint after each leg so a mid-run crash keeps completed results.
        ck = list(rows)
        if args.resume and prior_by_name:
            seen = {r["name"] for r in ck}
            for name, prior_row in prior_by_name.items():
                if name not in seen and (not wanted or name in wanted):
                    ck.append(prior_row)
        _write(
            ck,
            days=days,
            window=window,
            bench=bench,
            verdict=_verdict(
                ck,
                next((r for r in ck if r["name"] == "baseline"), None),
                days=days,
            ),
            strict_banner=strict_banner,
            out_md=out_md,
            out_json=out_json,
        )

    if args.resume and prior_by_name:
        by_name = {r["name"]: r for r in rows}
        order = [L.name for L in legs]
        if "baseline" not in order:
            order = ["baseline"] + order
        merged: list[dict] = []
        for name in order:
            if name in by_name:
                merged.append(by_name[name])
            elif name in prior_by_name:
                merged.append(prior_by_name[name])
        for r in rows:
            if r["name"] not in {m["name"] for m in merged}:
                merged.append(r)
        rows = merged
        if not strict_banner:
            strict_banner = str(
                (rows[0].get("strict_pit_banner") if rows else "")
                or "STRICT PIT: ON | insider/RVOL/catalyst/news/LLM/dyn_univ/buffett-fallback off"
            )

    base = next((r for r in rows if r["name"] == "baseline"), None)
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
