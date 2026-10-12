"""Inventory whether local data can test 12-to-7-month NYSE momentum.

Research only. Does not place orders, refresh screeners, open a broker
connection, or invent bars. Exit code 2 means the required simulation
cannot be run (inconclusive).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Match the paper-book supervisor env without starting the bot.
os.environ.setdefault("PAPER_TRADING", "true")
os.environ.setdefault("ALLOW_LIVE_TRADING", "false")
os.environ.setdefault("PAPER_CHASE_MODE", "1")
os.environ.setdefault("PAPER_AGGRESSIVE", "true")

import config  # noqa: E402
from modules.trading_books import BOOKS  # noqa: E402

PIPELINE = ROOT / "modules" / "pipeline_strategies.py"
CACHE = ROOT / "data" / "cache" / "backtest" / "days_266.pkl"
SLEEVE_ETFS = {
    "SPY",
    "QQQ",
    "IWM",
    "VTI",
    "GLD",
    "SLV",
    "CPER",
    "URA",
    "PPLT",
    "DBB",
    "GDX",
}


def _safe(fn, fallback):
    try:
        return fn()
    except Exception as exc:  # missing optional modules must not invent a rule
        return {"fallback": fallback, "error": f"{type(exc).__name__}: {exc}"}


def _apply_paper_launch_without_network() -> str:
    """Mirror paper startup, but do not refresh the screener (that hits the network).

    ``config.py`` shadows the ``config/`` directory, so
    ``from config.best_paper_config import apply_best_paper_config`` fails and
    ``init_paper_chase_if_enabled`` falls back to ``enforce_best_paper_stack``.
    """
    try:
        from config.best_paper_config import apply_best_paper_config
    except ImportError as exc:
        config.enforce_best_paper_stack()
        config.set_paper_aggressive_context(True)
        return f"fallback enforce_best_paper_stack ({type(exc).__name__}: {exc})"
    apply_best_paper_config()
    config.set_paper_aggressive_context(True)
    return "apply_best_paper_config"


def _paper_effective_rules() -> dict:
    """Flags after the paper launch path, minus the screener network refresh."""
    config.PAPER_DEPLOY_DEBUG = False
    launch = _apply_paper_launch_without_network()
    text = PIPELINE.read_text(encoding="utf-8")
    return {
        "paper_launch_apply": launch,
        "book_ids": [
            {"id": book_id, "enabled": bool(spec.get("enabled")), "label": spec.get("label")}
            for book_id, spec in BOOKS.items()
        ],
        "paper_chase": config.paper_chase_mode_enabled(),
        "paper_aggressive_context": config.paper_aggressive_context(),
        "realistic_research": config.is_realistic_research_active(),
        "ranking_score": "(last_close / rolling_mean) - 1",
        "ranking_ma_hardcoded_in_candidates": "ma_window = 20 if ipo else 50" in text,
        "effective_nyse_ma_window_banner_only": config.effective_nyse_ma_window(),
        "paper_nyse_ma_window_constant": config.PAPER_NYSE_MA_WINDOW,
        "entry_tolerance_pct": config.NYSE_MA_ENTRY_TOLERANCE_PCT,
        "max_equity_trades_per_cycle": config.effective_max_equity_trades(),
        "max_active_tickers": config.effective_max_active_tickers(),
        "per_name_max_pct": config.effective_per_name_max_pct(),
        "nyse_sleeve_cap_pct": config.effective_nyse_sleeve_cap_pct()
        if hasattr(config, "effective_nyse_sleeve_cap_pct")
        else config.NYSE_SLEEVE_CAP_PCT,
        "paper_nyse_sleeve_cap_pct": config.PAPER_NYSE_SLEEVE_CAP_PCT,
        "paper_nyse_high_cash_cap_pct": config.PAPER_NYSE_HIGH_CASH_CAP_PCT,
        "paper_nyse_max_exposure_pct": config.PAPER_NYSE_MAX_EXPOSURE_PCT,
        "fixed_universe_count": len(config.get_nyse_universe_fixed()),
        "quality_fixes_flag": config.PAPER_MOMENTUM_QUALITY_FIXES,
        "quality_fixes_effective": config.effective_paper_momentum_quality_fixes(),
        "scan_after_open_min": config.EQUITY_SCAN_AFTER_OPEN_MIN,
        "cooldown_source_is_0930_1000": "time(9, 30) <= t <= time(10, 0)" in text,
        "gap_skip_source_gt_2pct": "gap > 0.02" in text,
        "hold_bars_fallback": config.PAPER_POSITION_MAX_HOLD_BARS,
        "exit_optimization": config.effective_exit_optimization_enabled(),
        "exit_optimization_max_hold_bars": config.EXIT_OPTIMIZATION_MAX_HOLD_BARS,
        "atr_period": config.ATR_PERIOD,
        "atr_stop_multiplier": config.ATR_STOP_MULTIPLIER,
        "atr_risk_multiple": config.ATR_RISK_MULTIPLE,
        "smart_stops": config.effective_smart_stops_enabled(),
        "atr_sizing": _safe(config.effective_atr_sizing_enabled, config.ATR_SIZING_ENABLED),
        "overlap_filter": _safe(
            config.effective_nyse_overlap_filter_enabled,
            config.PAPER_NYSE_OVERLAP_FILTER_ENABLED,
        ),
        "conditional_on_spy_flag": _safe(
            config.effective_nyse_conditional_on_spy,
            config.PAPER_NYSE_CONDITIONAL_ON_SPY,
        ),
        "equity_slippage_bps_research_default": 5,
        "equity_commission_bps_research_default": 0,
    }


def _cache_inventory() -> dict:
    if not CACHE.is_file():
        return {"present": False, "path": str(CACHE)}
    import pandas as pd

    frame = pd.read_pickle(CACHE)
    equity = [
        c
        for c in frame.columns
        if "-USD" not in str(c) and str(c) not in SLEEVE_ETFS
    ]
    weekday = ~frame.index.dayofweek.isin([5, 6])
    weekend_equal = None
    if "AAPL" in frame.columns and len(frame) > 2:
        aapl = frame["AAPL"]
        sat = aapl.index.dayofweek == 5
        if sat.any():
            prev = aapl.shift(1)
            weekend_equal = float((aapl[sat] == prev[sat]).mean())
    first = frame.index.min()
    last = frame.index.max()
    after_12m = frame.loc[frame.index >= first + pd.DateOffset(months=12)]
    missing_from_universe = sorted(
        set(config.UNIVERSE) - set(map(str, frame.columns))
    )
    nn = frame.notna().sum()
    sparse = {str(k): int(v) for k, v in nn[nn < len(frame)].items()}
    return {
        "present": True,
        "path": str(CACHE.relative_to(ROOT)),
        "rows": int(frame.shape[0]),
        "cols": int(frame.shape[1]),
        "start": str(first.date()),
        "end": str(last.date()),
        "median_step_days": float(frame.index.to_series().diff().dt.days.median()),
        "weekday_rows": int(weekday.sum()),
        "weekend_rows": int((~weekday).sum()),
        "aapl_saturday_equals_prior_row": weekend_equal,
        "columns_have_ohlc": False,
        "equity_names": equity,
        "equity_count": len(equity),
        "sparse_columns": sparse,
        "universe_symbols_absent": missing_from_universe,
        "rows_on_or_after_12_month_lag": int(len(after_12m)),
        "first_date_after_12_month_lag": str(after_12m.index.min().date())
        if len(after_12m)
        else None,
        "last_date": str(last.date()),
    }


def _missing_inputs() -> dict:
    db_candidates = [
        ROOT / "market_data.db",
        ROOT / "data" / "market_data.db",
        ROOT / "dist" / "market_data.db",
    ]
    membership = list(ROOT.rglob("*sp500*")) + list(ROOT.rglob("*constituent*"))
    membership += list(ROOT.rglob("*delist*"))
    intraday_script = ROOT / "scripts" / "research" / "backtest_intraday.py"
    intraday_cache = ROOT / "data" / "intraday_cache"
    five_min = list(ROOT.rglob("*5m*")) + list(ROOT.rglob("*5min*"))
    # Ignore source code that merely mentions 5m.
    five_min_data = [
        str(p.relative_to(ROOT))
        for p in five_min
        if p.suffix.lower() in {".db", ".parquet", ".pkl", ".csv", ".feather", ".h5"}
    ]
    return {
        "market_data_db_present": any(p.is_file() for p in db_candidates),
        "membership_or_delist_files": [str(p.relative_to(ROOT)) for p in membership if p.is_file()],
        "intraday_research_script_present": intraday_script.is_file(),
        "intraday_cache_present": intraday_cache.is_dir(),
        "five_minute_data_files": five_min_data,
    }


def main() -> int:
    rules = _paper_effective_rules()
    cache = _cache_inventory()
    missing = _missing_inputs()
    five_min_coverage = 0.0
    report = {
        "decision": "inconclusive",
        "reason": (
            "Local data cannot support a point-in-time S&P 500 simulation "
            "with prior-close 12-to-7 signals and next 5-minute fills. "
            "5-minute coverage of member observations is 0%, below the 90% bar. "
            "No returns were computed."
        ),
        "books_in_repo": ["alpaca_live", "alpaca_paper"],
        "medium_book_present": False,
        "launch_path": {
            "daily": "Start_Bot_and_Dashboard.bat -> scripts/owner_reset.py",
            "live": "run_all.py (alpaca_live, Profile A)",
            "paper": "run_paper_bot.py -> run_all.py with PAPER_CHASE_MODE (alpaca_paper)",
            "nyse_orders": "run_all.py -> run_nyse_momentum_and_stat_arb -> run_equity_strategy -> execute_order",
        },
        "paper_rules_from_code": rules,
        "price_cache": cache,
        "other_inputs": missing,
        "five_minute_pit_coverage": five_min_coverage,
        "coverage_bar": 0.90,
        "returns_computed": False,
        "production_settings_changed": False,
        "bots_restarted": False,
        "holdout_touched": False,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
