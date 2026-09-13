import os
import time
import sys
import logging

os.environ["PAPER_DEPLOY_DEBUG"] = "false"
os.chdir(r"C:\Users\Owner\PythonTrading\stock-bot")
sys.path.insert(0, ".")
logging.disable(logging.INFO)

import config

config.PAPER_DEPLOY_DEBUG = False

import modules.market_context as mc
import modules.exit_management as em
import modules.risk_management as rm

mc.announce_regime_change = lambda r: r
em.record_exit_event = lambda *a, **k: None
rm.record_conviction_sample = lambda *a, **k: None
rm._save_correlation_snapshot = lambda *a, **k: None

from scripts.analysis.monte_carlo_backtest import (
    build_parser,
    apply_run_options_from_args,
    build_backtest_kwargs,
    perturb_market_data,
)
from backtester import _ensure_daily_data, run_backtest, _build_indicator_context
from modules.backtester_core import release_backtest_memory
import numpy as np

args = build_parser().parse_args(
    [
        "--paper-aggressive",
        "--days",
        "365",
        "--mc-runs",
        "1",
        "--no-thinking",
        "--seed",
        "42",
        "--noise-level",
        "0.01",
        "--regime-noise",
        "0.1",
    ]
)
apply_run_options_from_args(args)
data = _ensure_daily_data(365)
base = data.iloc[-max(5, int(365 * 0.8)) :].copy()
ctx = _build_indicator_context(base, max_years=20, refresh=False)
kw = build_backtest_kwargs(args)
kw["indicator_context"] = ctx
kw["deep_history_indicators_only"] = True
rng = np.random.default_rng(42)
pert, _, _ = perturb_market_data(base, noise_level=0.01, regime_noise=0.1, rng=rng)
for label, track in [("no_sleeve", False), ("sleeve", True)]:
    k = dict(kw)
    k["track_sleeve_path"] = track
    release_backtest_memory(collect=False, indicators=False)
    t0 = time.perf_counter()
    r = run_backtest(pert, **k)
    dt = time.perf_counter() - t0
    print(
        f"{label}: {dt:.1f}s ret={r.get('total_return_pct')} "
        f"dd={r.get('max_drawdown_pct')} "
        f"trough={r.get('sleeve_path_trough') is not None}",
        flush=True,
    )
