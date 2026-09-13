"""Quick Monte Carlo: locked Realistic Research v1.5.4 + Dynamic VTI (50 runs, 90d)."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _configure_mc_isolation() -> Path:
    mc = ROOT / "data" / "cache" / "mc_v154_dyn_vti_locked_90"
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
    return mc


_configure_mc_isolation()
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PYTHONUNBUFFERED", "1")
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PAPER_DEPLOY_DEBUG", "false")

logging.disable(logging.INFO)


def _silence_backtest_side_effects() -> None:
    import modules.market_context as market_context
    import modules.exit_management as exit_management
    import modules.risk_management as risk_management

    market_context.announce_regime_change = lambda regime: regime  # type: ignore[method-assign]
    exit_management.record_exit_event = lambda *args, **kwargs: None  # type: ignore[method-assign]
    risk_management.record_conviction_sample = lambda *args, **kwargs: None  # type: ignore[method-assign]
    risk_management._save_correlation_snapshot = lambda *args, **kwargs: None  # type: ignore[method-assign]


def main() -> int:
    _silence_backtest_side_effects()
    from scripts.analysis.monte_carlo_backtest import build_parser, run_monte_carlo

    out = ROOT / "scripts" / "analysis" / "monte_carlo_v154_dyn_vti_locked_90.json"
    status = ROOT / "scripts" / "analysis" / "monte_carlo_v154_dyn_vti_locked_90.status.txt"
    os.environ["MC_STATUS_PATH"] = str(status)
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
        "--export-json",
        str(out),
    ]
    status.write_text("running\n", encoding="utf-8")
    args = build_parser().parse_args(argv)
    print(
        "=== Locked Dynamic VTI MC (quick) ===\n"
        "paper-aggressive | Dynamic VTI LOCKED 40-75% | 90d | 50 runs | no-thinking | seed=42\n"
        f"export -> {out}",
        flush=True,
    )
    rc = int(run_monte_carlo(args))
    status.write_text(f"done exit={rc}\n", encoding="utf-8")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
