"""Replay worst 365d MC runs with trough sleeve-path attribution.

Reuses monte_carlo_backtest RNG + Dynamic VTI locked settings; enables
track_sleeve_path so marked exposures are available at the equity trough.
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
os.environ.setdefault("PAPER_DEPLOY_DEBUG", "false")
os.environ.setdefault("PYTHONUNBUFFERED", "1")
logging.disable(logging.INFO)

SOURCE_MC = ROOT / "scripts" / "analysis" / "monte_carlo_v154_dyn_vti_locked_365.json"
OUT_JSON = ROOT / "scripts" / "analysis" / "monte_carlo_v154_sleeve_attribution_365.json"
OUT_MD = ROOT / "scripts" / "analysis" / "mc_left_tail_sleeve_attribution_v154.md"
WORST_N = 5


def _silence_side_effects() -> None:
    import modules.exit_management as exit_management
    import modules.market_context as market_context
    import modules.risk_management as risk_management

    market_context.announce_regime_change = lambda regime: regime  # type: ignore[method-assign]
    exit_management.record_exit_event = lambda *a, **k: None  # type: ignore[method-assign]
    risk_management.record_conviction_sample = lambda *a, **k: None  # type: ignore[method-assign]
    risk_management._save_correlation_snapshot = lambda *a, **k: None  # type: ignore[method-assign]


def _fidelity(prior: dict, replay: dict) -> dict:
    return {
        "prior_return_pct": prior.get("total_return_pct"),
        "replay_return_pct": replay.get("total_return_pct"),
        "prior_sharpe": prior.get("sharpe"),
        "replay_sharpe": replay.get("sharpe"),
        "prior_max_dd_pct": prior.get("max_drawdown_pct"),
        "replay_max_dd_pct": replay.get("max_drawdown_pct"),
        "return_delta_pp": round(
            float(replay.get("total_return_pct") or 0)
            - float(prior.get("total_return_pct") or 0),
            2,
        ),
        "dd_delta_pp": round(
            float(replay.get("max_drawdown_pct") or 0)
            - float(prior.get("max_drawdown_pct") or 0),
            2,
        ),
    }


def _write_markdown(payload: dict) -> None:
    runs = payload.get("runs") or []
    lines: list[str] = []
    lines.append("# Left-Tail Sleeve Attribution — Dynamic VTI 40–75% (365d)")
    lines.append("")
    lines.append(f"**Date:** {payload.get('saved_at', '')[:10]}")
    lines.append(
        f"**Source MC:** `{payload.get('source_mc')}` · "
        f"replay seed={payload.get('seed')} · days={payload.get('days')} · "
        f"thinking={payload.get('thinking')}"
    )
    lines.append("")
    lines.append("## Method")
    lines.append("")
    lines.append(
        "- Replay **worst 5 runs by Sharpe** from the 365d export (same RNG advance "
        "as original MC: seed 42, noise 0.01, regime_noise 0.1)."
    )
    lines.append(
        "- Backtester `track_sleeve_path=True` records marked sleeve notionals each bar; "
        "attribution is taken at the **max-DD equity trough**."
    )
    lines.append(
        "- `main_bleeding_sleeve` = most negative peak→trough marked-USD delta among "
        "active sleeves (proxy; rebalancing can distort)."
    )
    lines.append(
        "- Lifetime sleeve PnL (end-of-run attribution) is secondary context."
    )
    lines.append("")
    lim = payload.get("limitation") or ""
    if lim:
        lines.append(f"**Limitation:** {lim}")
        lines.append("")

    lines.append("## Per-run trough snapshot")
    lines.append("")
    lines.append(
        "| Run | Trough DD | VTI% | Active% | Cash% | "
        "SPY% | NYSE% | StatArb% | Shorts% | Metals/Other% | "
        "Bleed sleeve | Top holdings |"
    )
    lines.append("|----:|----------:|-----:|--------:|------:|-----:|------:|---------:|--------:|--------------:|--------------|-------------|")
    for row in runs:
        att = row.get("trough") or {}
        sp = att.get("sleeve_pct") or {}
        metals_other = float(sp.get("metals") or 0) + float(sp.get("other") or 0) + float(
            sp.get("crypto") or 0
        )
        tops = att.get("top_holdings") or []
        top_s = ", ".join(
            f"{t.get('symbol')}(${t.get('notional'):,.0f})" for t in tops[:3]
        ) or "—"
        fid = row.get("fidelity") or {}
        note = ""
        if abs(float(fid.get("return_delta_pp") or 0)) > 5:
            note = " ‡"
        lines.append(
            f"| {row.get('run')} | {att.get('trough_dd_pct')}%{note} | "
            f"{att.get('vti_pct')} | {att.get('active_pct')} | {att.get('cash_pct')} | "
            f"{sp.get('spy', 0)} | {sp.get('nyse_momentum', 0)} | "
            f"{sp.get('stat_arb', 0)} | {sp.get('opportunistic_short', 0)} | "
            f"{metals_other:.1f} | {att.get('main_bleeding_sleeve') or '—'} | {top_s} |"
        )
    lines.append("")
    lines.append("‡ = replay return diverged >5pp from export (universe/RNG drift).")
    lines.append("")

    lines.append("## Peak→trough marked USD deltas (active sleeves)")
    lines.append("")
    lines.append(
        "| Run | Δ equity | Δ VTI | Δ SPY | Δ NYSE | Δ StatArb | Δ Shorts | Δ Crypto | Δ Metals | Δ Other |"
    )
    lines.append(
        "|----:|---------:|------:|------:|-------:|----------:|---------:|---------:|---------:|--------:|"
    )
    for row in runs:
        att = row.get("trough") or {}
        d = att.get("sleeve_delta_peak_to_trough_usd") or {}
        deq = round(
            float(att.get("trough_equity") or 0) - float(att.get("peak_equity") or 0), 0
        )
        lines.append(
            f"| {row.get('run')} | {deq:,.0f} | "
            f"{d.get('vti_notional', 0):,.0f} | {d.get('spy', 0):,.0f} | "
            f"{d.get('nyse_momentum', 0):,.0f} | {d.get('stat_arb', 0):,.0f} | "
            f"{d.get('opportunistic_short', 0):,.0f} | {d.get('crypto', 0):,.0f} | "
            f"{d.get('metals', 0):,.0f} | {d.get('other', 0):,.0f} |"
        )
    lines.append("")

    lines.append("## Lifetime sleeve PnL (end of run)")
    lines.append("")
    lines.append("| Run | SPY | NYSE/MA50 | Stat arb | Shorts | Crypto |")
    lines.append("|----:|----:|----------:|---------:|-------:|-------:|")
    for row in runs:
        lp = (row.get("trough") or {}).get("lifetime_sleeve_pnl_usd") or {}
        lines.append(
            f"| {row.get('run')} | {lp.get('spy', 0):+,.0f} | "
            f"{lp.get('nyse_momentum', 0):+,.0f} | {lp.get('stat_arb', 0):+,.0f} | "
            f"{lp.get('opportunistic_short', 0):+,.0f} | {lp.get('crypto', 0):+,.0f} |"
        )
    lines.append("")

    # Cross-run summary
    bleed_counts = Counter(
        (r.get("trough") or {}).get("main_bleeding_sleeve") or "unknown" for r in runs
    )
    vti_high = sum(
        1
        for r in runs
        if float((r.get("trough") or {}).get("vti_pct") or 0) >= 55
    )
    active_large = sum(
        1
        for r in runs
        if float((r.get("trough") or {}).get("active_pct") or 0) >= 20
    )
    nyse_open = sum(
        1
        for r in runs
        if float(((r.get("trough") or {}).get("sleeve_pct") or {}).get("nyse_momentum") or 0)
        >= 5
    )
    lines.append("## Cross-run summary")
    lines.append("")
    lines.append(f"- **Dominant bleed sleeve counts:** {dict(bleed_counts)}")
    lines.append(
        f"- **VTI still high at trough (≥55%):** {vti_high}/{len(runs)}"
    )
    lines.append(
        f"- **Active book still large (≥20% of equity):** {active_large}/{len(runs)}"
    )
    lines.append(
        f"- **NYSE momentum ≥5% at trough:** {nyse_open}/{len(runs)}"
    )
    lines.append("")
    lines.append(payload.get("summary_narrative") or "")
    lines.append("")
    lines.append("## Short table")
    lines.append("")
    lines.append("| run_id | trough_dd | vti_pct | main_bleeding_sleeve | notes |")
    lines.append("|-------:|----------:|--------:|----------------------|-------|")
    for row in runs:
        att = row.get("trough") or {}
        notes = row.get("notes") or ""
        lines.append(
            f"| {row.get('run')} | {att.get('trough_dd_pct')}% | "
            f"{att.get('vti_pct')} | {att.get('main_bleeding_sleeve') or '—'} | {notes} |"
        )
    lines.append("")
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


def _build_narrative(runs: list[dict]) -> str:
    if not runs:
        return "No runs replayed."
    bleed = Counter(
        (r.get("trough") or {}).get("main_bleeding_sleeve") or "unknown" for r in runs
    )
    top_bleed = bleed.most_common(1)[0][0] if bleed else "unknown"
    avg_vti = float(
        np.mean([float((r.get("trough") or {}).get("vti_pct") or 0) for r in runs])
    )
    avg_active = float(
        np.mean([float((r.get("trough") or {}).get("active_pct") or 0) for r in runs])
    )
    avg_nyse = float(
        np.mean(
            [
                float(
                    ((r.get("trough") or {}).get("sleeve_pct") or {}).get(
                        "nyse_momentum"
                    )
                    or 0
                )
                for r in runs
            ]
        )
    )
    lines = [
        "### Findings",
        "",
        f"- Most common peak→trough bleed sleeve: **{top_bleed}** ({dict(bleed)}).",
        f"- Mean trough VTI marked %: **{avg_vti:.1f}%**; mean active %: **{avg_active:.1f}%** "
        f"(VTI high while residual book still non-trivial).",
        f"- Mean NYSE momentum exposure at trough: **{avg_nyse:.1f}%**.",
    ]
    # Holding pattern
    top_syms: Counter[str] = Counter()
    for r in runs:
        for h in (r.get("trough") or {}).get("top_holdings") or []:
            top_syms[str(h.get("symbol"))] += 1
    if top_syms:
        common = ", ".join(f"{s}×{c}" for s, c in top_syms.most_common(5))
        lines.append(f"- Recurring top holdings across wipeouts: {common}.")
    lines.append(
        "- Pattern check: if NYSE/stat-arb remain open with high VTI into a late grind, "
        "the left tail is **core beta + residual actives**, not VTI abandonment."
    )
    return "\n".join(lines)


def main() -> int:
    _silence_side_effects()
    from backtester import _ensure_daily_data, run_backtest
    from modules.backtester_core import release_backtest_memory
    from scripts.analysis.monte_carlo_backtest import (
        apply_run_options_from_args,
        build_backtest_kwargs,
        build_parser,
        extract_run_diagnostics,
        extract_trough_sleeve_attribution,
        perturb_market_data,
    )

    if not SOURCE_MC.exists():
        print(f"Missing source MC export: {SOURCE_MC}", flush=True)
        return 1

    prior = json.loads(SOURCE_MC.read_text(encoding="utf-8"))
    ranked = sorted(prior["runs"], key=lambda r: float(r["sharpe"]))
    worst = ranked[:WORST_N]
    targets = [int(r["run"]) for r in worst]
    prior_by_run = {int(r["run"]): r for r in prior["runs"]}

    argv = [
        "--paper-aggressive",
        "--days",
        "365",
        "--mc-runs",
        "50",
        "--no-thinking",
        "--seed",
        "42",
        "--noise-level",
        "0.01",
        "--regime-noise",
        "0.1",
    ]
    args = build_parser().parse_args(argv)
    apply_run_options_from_args(args)
    days = 365
    data = _ensure_daily_data(days)
    target_sim_bars = max(5, int(days * 0.80))
    base = data.iloc[-target_sim_bars:].copy() if len(data) > target_sim_bars else data.copy()
    bt_kwargs = build_backtest_kwargs(args)
    bt_kwargs["track_sleeve_path"] = True
    bt_kwargs["stat_arb_report"] = True

    from backtester import _build_indicator_context

    ctx = _build_indicator_context(base, max_years=20, refresh=False)
    if ctx is not None and not getattr(ctx, "empty", True):
        bt_kwargs["indicator_context"] = ctx
        bt_kwargs["deep_history_indicators_only"] = True

    print(
        f"Sleeve-attribution replay: worst runs {targets} "
        f"(universe cols={base.shape[1]}, sim_bars={len(base)})",
        flush=True,
    )

    out_rows: list[dict] = []
    for run_id in targets:
        rng = np.random.default_rng(42)
        for _ in range(run_id - 1):
            perturb_market_data(base, noise_level=0.01, regime_noise=0.1, rng=rng)
        perturbed, vol_mult, regime_drift = perturb_market_data(
            base, noise_level=0.01, regime_noise=0.1, rng=rng
        )
        release_backtest_memory(collect=False, indicators=False)
        result = run_backtest(perturbed, **bt_kwargs)
        diag = extract_run_diagnostics(result if isinstance(result, dict) else {})
        trough = extract_trough_sleeve_attribution(
            result if isinstance(result, dict) else {}
        )
        prior_row = prior_by_run[run_id]
        fid = _fidelity(prior_row, result if isinstance(result, dict) else {})
        notes_parts = []
        if abs(float(fid.get("return_delta_pp") or 0)) > 5:
            notes_parts.append(f"replayΔret {fid['return_delta_pp']:+.1f}pp")
        bleed = trough.get("main_bleeding_sleeve")
        vti = trough.get("vti_pct")
        active = trough.get("active_pct")
        if vti is not None and float(vti) >= 55 and active is not None and float(active) >= 15:
            notes_parts.append("high VTI + active open")
        nyse_pct = float((trough.get("sleeve_pct") or {}).get("nyse_momentum") or 0)
        if nyse_pct >= 5:
            notes_parts.append(f"NYSE {nyse_pct:.1f}%")
        row = {
            "run": run_id,
            "vol_mult": round(float(vol_mult), 4),
            "regime_drift": round(float(regime_drift), 4),
            "fidelity": fid,
            "diagnostics": diag,
            "trough": trough,
            "notes": "; ".join(notes_parts) if notes_parts else "—",
        }
        out_rows.append(row)
        print(
            f"  run {run_id}: dd={trough.get('trough_dd_pct')}% "
            f"vti={trough.get('vti_pct')}% active={trough.get('active_pct')}% "
            f"bleed={bleed} tops={trough.get('top_holdings')}",
            flush=True,
        )

    universe_note = (
        f"Replay universe has {base.shape[1]} symbols; original export may differ. "
        "If fidelity deltas are large, treat sleeve mix as diagnostic of wipeout *shape*, "
        "not exact reproduction of export equity."
    )
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "source_mc": SOURCE_MC.name,
        "seed": 42,
        "days": 365,
        "noise_level": 0.01,
        "regime_noise": 0.1,
        "thinking": False,
        "profile": "paper-aggressive + Dynamic VTI 40-75%",
        "worst_runs": targets,
        "limitation": universe_note,
        "summary_narrative": _build_narrative(out_rows),
        "runs": out_rows,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _write_markdown(payload)
    print(f"Exported {OUT_JSON}", flush=True)
    print(f"Wrote {OUT_MD}", flush=True)
    print("", flush=True)
    print("run_id | trough_dd | vti_pct | main_bleeding_sleeve | notes", flush=True)
    for row in out_rows:
        att = row["trough"]
        print(
            f"{row['run']} | {att.get('trough_dd_pct')}% | {att.get('vti_pct')} | "
            f"{att.get('main_bleeding_sleeve')} | {row.get('notes')}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
