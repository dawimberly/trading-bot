"""Replay specific MC run indices with VTI/regime diagnostics (same seed/noise as 90d)."""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]


def _configure_mc_isolation() -> None:
    mc = ROOT / "data" / "cache" / "mc_v154_wipeout_replay"
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
logging.disable(logging.INFO)

# Worst-10% wipeout cluster from monte_carlo_v154_dyn_vti_locked_90.json
WIPEOUT_RUNS = [19, 39, 42, 44, 45]
# A couple of strong runs for contrast (top Sharpe from same export — filled at runtime)
CONTRAST_TOP_N = 3


def main() -> int:
    import modules.market_context as market_context
    import modules.exit_management as exit_management
    import modules.risk_management as risk_management

    market_context.announce_regime_change = lambda regime: regime  # type: ignore[method-assign]
    exit_management.record_exit_event = lambda *a, **k: None  # type: ignore[method-assign]
    risk_management.record_conviction_sample = lambda *a, **k: None  # type: ignore[method-assign]
    risk_management._save_correlation_snapshot = lambda *a, **k: None  # type: ignore[method-assign]

    from backtester import MIN_HISTORY, _ensure_daily_data, run_backtest
    from modules.backtester_core import release_backtest_memory
    from scripts.analysis.monte_carlo_backtest import (
        apply_run_options_from_args,
        build_backtest_kwargs,
        build_parser,
        extract_run_diagnostics,
        perturb_market_data,
    )

    src = ROOT / "scripts" / "analysis" / "monte_carlo_v154_dyn_vti_locked_90.json"
    prior = json.loads(src.read_text(encoding="utf-8"))
    ranked = sorted(prior["runs"], key=lambda r: float(r["sharpe"]), reverse=True)
    contrast_runs = [int(r["run"]) for r in ranked[:CONTRAST_TOP_N]]
    targets = sorted(set(WIPEOUT_RUNS + contrast_runs))

    argv = [
        "--paper-aggressive",
        "--days",
        "90",
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
    days = 90
    data = _ensure_daily_data(days)
    target_sim_bars = max(5, int(days * 0.80))
    base = data.iloc[-target_sim_bars:].copy() if len(data) > target_sim_bars else data.copy()
    bt_kwargs = build_backtest_kwargs(args)

    from backtester import _build_indicator_context

    ctx = _build_indicator_context(base, max_years=20, refresh=False)
    if ctx is not None and not getattr(ctx, "empty", True):
        bt_kwargs["indicator_context"] = ctx
        bt_kwargs["deep_history_indicators_only"] = True

    out_rows = []
    print(
        f"Replaying runs {targets} (wipeouts={WIPEOUT_RUNS}, contrast={contrast_runs})",
        flush=True,
    )
    for run_id in targets:
        rng = np.random.default_rng(42)
        # Advance RNG exactly like the original MC loop up to run_id.
        for _ in range(run_id - 1):
            perturb_market_data(base, noise_level=0.01, regime_noise=0.1, rng=rng)
        perturbed, vol_mult, regime_drift = perturb_market_data(
            base, noise_level=0.01, regime_noise=0.1, rng=rng
        )
        release_backtest_memory(collect=False, indicators=False)
        result = run_backtest(perturbed, **bt_kwargs)
        diag = extract_run_diagnostics(result if isinstance(result, dict) else {})
        prior_row = next(r for r in prior["runs"] if int(r["run"]) == run_id)
        row = {
            "run": run_id,
            "cluster": "wipeout" if run_id in WIPEOUT_RUNS else "contrast",
            "prior_return_pct": prior_row["total_return_pct"],
            "prior_sharpe": prior_row["sharpe"],
            "prior_max_drawdown_pct": prior_row["max_drawdown_pct"],
            "total_return_pct": float(result.get("total_return_pct") or 0),
            "sharpe": float(result.get("sharpe") or 0),
            "max_drawdown_pct": float(result.get("max_drawdown_pct") or 0),
            "vol_mult": round(float(vol_mult), 4),
            "regime_drift": round(float(regime_drift), 4),
            **diag,
            "regime_series_head": [
                str(x).split(":")[0] for x in (result.get("regime_series") or [])[:8]
            ],
            "regime_series_tail": [
                str(x).split(":")[0] for x in (result.get("regime_series") or [])[-8:]
            ],
        }
        out_rows.append(row)
        print(
            f"  run {run_id} [{row['cluster']}] ret={row['total_return_pct']:.2f}% "
            f"sharpe={row['sharpe']:.2f} dd={row['max_drawdown_pct']:.2f}% "
            f"vti(avg/min/max/atDD)={row['vti_avg']}/{row['vti_min']}/{row['vti_max']}/{row['vti_at_max_dd']} "
            f"regime@DD={row['regime_at_max_dd']}",
            flush=True,
        )

    out = ROOT / "scripts" / "analysis" / "monte_carlo_v154_wipeout_replay_90.json"
    payload = {
        "source_mc": str(src.name),
        "seed": 42,
        "days": 90,
        "noise_level": 0.01,
        "regime_noise": 0.1,
        "thinking": False,
        "profile": "paper-aggressive + Dynamic VTI 40-75%",
        "wipeout_runs": WIPEOUT_RUNS,
        "contrast_runs": contrast_runs,
        "runs": out_rows,
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Exported {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
