"""Fresh 365d MC with trough sleeve attribution (faithful paths).

Seed-replay of prior wipeout IDs diverges when the universe changes
(column-count alters RNG consumption). This runner re-samples under the
current universe with --track-sleeve-path, then writes worst-5 analysis.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]


def _configure_mc_isolation() -> None:
    mc = ROOT / "data" / "cache" / "mc_v154_sleeve_attr_365"
    mc.mkdir(parents=True, exist_ok=True)
    files = {
        "EXIT_EVENTS_FILE": "exit_events.json",
        "CONVICTION_METRICS_FILE": "conviction_metrics.json",
        "CORRELATION_METRICS_FILE": "correlation_guard.json",
        "SECTOR_ROTATION_STATE_FILE": "sector_rotation_state.json",
        "SECTOR_SCREENER_STATE_FILE": "sector_screener_state.json",
        "INSIDER_MONITOR_STATE_FILE": "insider_monitor_state.json",
        "ORB_MOMENTUM_STATE_FILE": "orb_momentum_state.json",
        "VOL_BREAKOUT_STATE_FILE": "vol_breakout_state.json",
        "POLITICIAN_COPY_STATE_FILE": "politician_copy_state.json",
        "WHEEL_SLEEVE_STATE_FILE": "wheel_sleeve_state.json",
    }
    for key, name in files.items():
        os.environ[key] = str(mc / name)


_configure_mc_isolation()
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
# Force off — config defaults PAPER_DEPLOY_DEBUG=true and setdefault won't override .env.
os.environ["PAPER_DEPLOY_DEBUG"] = "false"
os.environ.setdefault("PYTHONUNBUFFERED", "1")
logging.disable(logging.INFO)

OUT_JSON = ROOT / "scripts" / "analysis" / "monte_carlo_v154_sleeve_attribution_365.json"
OUT_MD = ROOT / "scripts" / "analysis" / "mc_left_tail_sleeve_attribution_v154.md"
PRIOR_MC = ROOT / "scripts" / "analysis" / "monte_carlo_v154_dyn_vti_locked_365.json"
MC_RUNS = int(os.environ.get("MC_SLEEVE_ATTR_RUNS", "50"))
WORST_N = 5


def _silence_side_effects() -> None:
    import config

    config.PAPER_DEPLOY_DEBUG = False
    import modules.exit_management as exit_management
    import modules.market_context as market_context
    import modules.risk_management as risk_management

    market_context.announce_regime_change = lambda regime: regime  # type: ignore[method-assign]
    exit_management.record_exit_event = lambda *a, **k: None  # type: ignore[method-assign]
    risk_management.record_conviction_sample = lambda *a, **k: None  # type: ignore[method-assign]
    risk_management._save_correlation_snapshot = lambda *a, **k: None  # type: ignore[method-assign]


def _notes(att: dict) -> str:
    parts: list[str] = []
    vti = float(att.get("vti_pct") or 0)
    active = float(att.get("active_pct") or 0)
    if vti >= 55 and active >= 15:
        parts.append("high VTI + active open")
    elif vti >= 55:
        parts.append("high VTI at trough")
    nyse = float((att.get("sleeve_pct") or {}).get("nyse_momentum") or 0)
    if nyse >= 5:
        parts.append(f"NYSE {nyse:.1f}%")
    spy = float((att.get("sleeve_pct") or {}).get("spy") or 0)
    if spy >= 10:
        parts.append(f"SPY {spy:.1f}%")
    return "; ".join(parts) if parts else "—"


def write_report(mc_payload: dict) -> dict:
    """Build analysis JSON + markdown; worst 5 by max DD (most negative)."""
    runs_all = list(mc_payload.get("runs") or [])
    ranked = sorted(
        runs_all, key=lambda r: float(r.get("max_drawdown_pct") or 0.0)
    )
    worst = ranked[:WORST_N]
    rows = []
    for r in worst:
        att = dict(r.get("trough_sleeve") or {})
        # Official max DD from performance metrics (not sparse peak/trough equity ratio).
        att["trough_dd_pct"] = r.get("max_drawdown_pct")
        if att.get("vti_pct") is None and r.get("vti_at_max_dd") is not None:
            att["vti_pct"] = round(100.0 * float(r["vti_at_max_dd"]), 2)
        dominant = (
            att.get("dominant_sleeve_at_trough")
            or att.get("main_bleeding_sleeve")
            or "—"
        )
        rows.append(
            {
                "run": int(r["run"]),
                "total_return_pct": r.get("total_return_pct"),
                "sharpe": r.get("sharpe"),
                "max_drawdown_pct": r.get("max_drawdown_pct"),
                "vol_mult": r.get("vol_mult"),
                "regime_at_max_dd": r.get("regime_at_max_dd"),
                "max_dd_bar": r.get("max_dd_bar"),
                "trough": att,
                "dominant_sleeve": dominant,
                "peak_vs_trough_shift": att.get("peak_vs_trough_shift") or "—",
                "notes": _notes(att),
            }
        )

    bleed = Counter(
        (r.get("trough") or {}).get("main_bleeding_sleeve") or "unknown" for r in rows
    )
    dom = Counter(r.get("dominant_sleeve") or "unknown" for r in rows)
    avg_vti = float(
        np.mean([float((r.get("trough") or {}).get("vti_pct") or 0) for r in rows] or [0])
    )
    avg_active = float(
        np.mean(
            [float((r.get("trough") or {}).get("active_pct") or 0) for r in rows] or [0]
        )
    )
    high_vti_n = sum(
        1 for r in rows if float((r.get("trough") or {}).get("vti_pct") or 0) >= 55
    )

    narrative = [
        "### Findings",
        "",
        f"- Sample: **{len(runs_all)}** MC paths (seed={mc_payload.get('seed')}, "
        f"days={mc_payload.get('days')}), peak/trough sleeve snapshots only.",
        f"- Worst {WORST_N} ranked by **max drawdown** (not prior wipeout IDs).",
        f"- High VTI at trough (≥55%): **{high_vti_n}/{len(rows)}** "
        f"(mean trough VTI **{avg_vti:.1f}%**, mean active **{avg_active:.1f}%**).",
        f"- Dominant marked sleeve at trough: {dict(dom)}.",
        f"- Largest peak→trough marked-USD bleed: {dict(bleed)}.",
    ]
    if high_vti_n >= 3:
        narrative.append(
            "- **Yes — worst paths again show high VTI at the trough**, so the left "
            "tail is not explained by Dynamic VTI collapsing to the 40% floor."
        )
    else:
        narrative.append(
            "- Worst-path trough VTI is mixed; check per-run table before blaming the floor."
        )

    analysis = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "source_mc": OUT_JSON.name,
        "limitation": (
            "Seed-replay of older wipeout IDs diverges when universe width changes; "
            "this report uses worst-5 max-DD from the fresh MC only."
        ),
        "mc_runs": len(runs_all),
        "seed": mc_payload.get("seed"),
        "days": mc_payload.get("days"),
        "thinking": False,
        "profile": "paper-aggressive + Dynamic VTI 40-75%",
        "worst_runs": [r["run"] for r in rows],
        "high_vti_at_trough_count": high_vti_n,
        "summary_narrative": "\n".join(narrative),
        "runs": rows,
    }

    lines: list[str] = []
    lines.append("# Left-Tail Sleeve Attribution — Dynamic VTI 40–75% (365d)")
    lines.append("")
    lines.append(f"**Date:** {analysis['saved_at'][:10]}")
    lines.append(
        f"**MC export:** `{OUT_JSON.name}` · {len(runs_all)} runs · seed "
        f"{mc_payload.get('seed')} · no-thinking · peak/trough sleeve snapshots"
    )
    lines.append("")
    lines.append("## Method")
    lines.append("")
    lines.append(
        "- Fresh MC only (no dependency on prior wipeout run IDs — seed replay "
        "does not reproduce −40% paths when the universe changes)."
    )
    lines.append(
        f"- Profile: paper-aggressive, Dynamic VTI locked 40–75%, 365d, "
        f"{len(runs_all)} runs, seed 42, noise 0.01, regime_noise 0.1, thinking OFF."
    )
    lines.append(
        "- `track_sleeve_path`: snapshot **peak equity** and **max-DD trough** only "
        "(not every bar). `PAPER_DEPLOY_DEBUG` forced off for the run."
    )
    lines.append(
        "- Worst 5 by **max_drawdown_pct** (performance metric). Sleeve mix is from "
        "the peak/trough snapshot nearest that DD; `dominant_sleeve` = largest active "
        "marked % at trough; bleed = most negative peak→trough marked-USD delta."
    )
    n_done = len(runs_all)
    planned = int(mc_payload.get("mc_runs") or n_done)
    if mc_payload.get("partial") or n_done < planned:
        lines.append(
            f"- **Sample status:** {n_done}/{planned} runs completed "
            f"(partial export; worst-5 still valid within this sample)."
        )
    lines.append("")
    if analysis.get("limitation"):
        lines.append(f"**Limitation:** {analysis['limitation']}")
        lines.append("")

    lines.append("## Short table")
    lines.append("")
    lines.append(
        "| run_id | trough_dd | vti_pct | dominant_sleeve | peak vs trough shift |"
    )
    lines.append(
        "|-------:|----------:|--------:|-----------------|----------------------|"
    )
    for row in rows:
        att = row.get("trough") or {}
        lines.append(
            f"| {row['run']} | {att.get('trough_dd_pct')}% | {att.get('vti_pct')} | "
            f"{row.get('dominant_sleeve')} | {row.get('peak_vs_trough_shift')} |"
        )
    lines.append("")

    lines.append("## Trough sleeve mix")
    lines.append("")
    lines.append(
        "| Run | Ret% | Trough DD | VTI% | Active% | Cash% | "
        "SPY% | NYSE% | StatArb% | Shorts% | Bleed Δ |"
    )
    lines.append(
        "|----:|-----:|----------:|-----:|--------:|------:|"
        "-----:|------:|---------:|--------:|---------|"
    )
    for row in rows:
        att = row.get("trough") or {}
        sp = att.get("sleeve_pct") or {}
        lines.append(
            f"| {row['run']} | {row.get('total_return_pct')} | "
            f"{att.get('trough_dd_pct')}% | {att.get('vti_pct')} | "
            f"{att.get('active_pct')} | {att.get('cash_pct')} | "
            f"{sp.get('spy', 0)} | {sp.get('nyse_momentum', 0)} | "
            f"{sp.get('stat_arb', 0)} | {sp.get('opportunistic_short', 0)} | "
            f"{att.get('main_bleeding_sleeve') or '—'} |"
        )
    lines.append("")

    lines.append("## Peak→trough marked USD deltas")
    lines.append("")
    lines.append(
        "| Run | Δ equity | Δ VTI | Δ SPY | Δ NYSE | Δ StatArb | Δ Shorts | Δ Cash |"
    )
    lines.append(
        "|----:|---------:|------:|------:|-------:|----------:|---------:|-------:|"
    )
    for row in rows:
        att = row.get("trough") or {}
        d = att.get("sleeve_delta_peak_to_trough_usd") or {}
        deq = round(
            float(att.get("trough_equity") or 0) - float(att.get("peak_equity") or 0), 0
        )
        lines.append(
            f"| {row['run']} | {deq:,.0f} | "
            f"{float(d.get('vti_notional') or 0):,.0f} | {float(d.get('spy') or 0):,.0f} | "
            f"{float(d.get('nyse_momentum') or 0):,.0f} | {float(d.get('stat_arb') or 0):,.0f} | "
            f"{float(d.get('opportunistic_short') or 0):,.0f} | "
            f"{float(d.get('cash') or 0):,.0f} |"
        )
    lines.append("")
    lines.append("## Cross-run summary")
    lines.append("")
    lines.append(analysis["summary_narrative"])
    lines.append("")
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    analysis_path = (
        ROOT / "scripts" / "analysis" / "monte_carlo_v154_sleeve_attribution_analysis.json"
    )
    analysis_path.write_text(json.dumps(analysis, indent=2), encoding="utf-8")
    return analysis


def main() -> int:
    _silence_side_effects()
    from scripts.analysis.monte_carlo_backtest import build_parser, run_monte_carlo

    status = (
        ROOT / "scripts" / "analysis" / "monte_carlo_v154_sleeve_attribution_365.status.txt"
    )
    os.environ["MC_STATUS_PATH"] = str(status)
    argv = [
        "--paper-aggressive",
        "--days",
        "365",
        "--mc-runs",
        str(MC_RUNS),
        "--no-thinking",
        "--seed",
        "42",
        "--noise-level",
        "0.01",
        "--regime-noise",
        "0.1",
        "--track-sleeve-path",
        "--export-json",
        str(OUT_JSON),
    ]
    status.write_text("running\n", encoding="utf-8")
    args = build_parser().parse_args(argv)
    print(
        "=== Sleeve-attribution MC (left-tail step 1) ===\n"
        f"paper-aggressive | Dyn VTI 40-75% | 365d | {MC_RUNS} runs | "
        "no-thinking | seed=42 | peak/trough sleeve only | deploy_debug=OFF\n"
        f"export -> {OUT_JSON}",
        flush=True,
    )
    rc = int(run_monte_carlo(args))
    if rc == 0 and OUT_JSON.exists():
        payload = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        analysis = write_report(payload)
        print(f"Wrote {OUT_MD}", flush=True)
        print(
            "run_id | trough_dd | vti_pct | dominant_sleeve | peak vs trough shift",
            flush=True,
        )
        for row in analysis["runs"]:
            att = row["trough"]
            print(
                f"{row['run']} | {att.get('trough_dd_pct')}% | {att.get('vti_pct')} | "
                f"{row.get('dominant_sleeve')} | {row.get('peak_vs_trough_shift')}",
                flush=True,
            )
    status.write_text(f"done exit={rc}\n", encoding="utf-8")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
