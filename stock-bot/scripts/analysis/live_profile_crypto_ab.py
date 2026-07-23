"""365d Live Profile A ($300) — crypto OFF vs ON (final live-profile test)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import os

import config
from backtester import (
    MIN_HISTORY,
    _benchmark_return,
    _ensure_daily_data,
    release_backtest_memory,
    run_backtest,
)
from modules.backtester_core import apply_default_execution_costs


def _row(label: str, result: dict, bench: float) -> dict:
    vs = round(result["total_return_pct"] - bench, 2)
    return {
        "label": label,
        "return_pct": result["total_return_pct"],
        "sharpe": result["sharpe"],
        "max_dd_pct": result["max_drawdown_pct"],
        "vs_vti": vs,
        "total_orders": int(result.get("total_orders") or 0),
        "crypto_trades": int(result.get("crypto_signals") or 0),
        "spy_trades": int(result.get("spy_signals") or 0),
        "nyse_trades": int(result.get("nyse_signals") or 0),
        "pairs_traded": int(result.get("pairs_traded") or 0),
        "halt_events": result.get("halt_events", 0),
        "avg_active_pct": result.get("avg_active_exposure_pct"),
    }


def main() -> None:
    apply_default_execution_costs()
    days = 365
    data = _ensure_daily_data(days, refresh=False, use_max=False)
    if len(data) < MIN_HISTORY:
        raise SystemExit(f"Need {MIN_HISTORY} bars; got {len(data)}")

    bench = _benchmark_return(data, MIN_HISTORY)
    start = data.index[MIN_HISTORY].date()
    end = data.index[-1].date()
    sim_bars = len(data) - MIN_HISTORY

    base_kwargs = dict(
        small_account=True,
        vti_core_pct=config.SMALL_ACCOUNT_VTI_CORE_PCT,
        live_thinking_start_equity=300.0,
        simulate_live_thinking=True,
        paper_thinking=False,
        track_metrics=True,
        track_active_exposure=True,
    )

    print("--- LIVE PROFILE A FINAL TEST (365d) ---")
    print(
        f"Window: {start} -> {end} ({sim_bars} bars) | start $300 | "
        f"{config.SMALL_ACCOUNT_VTI_CORE_PCT:.0%} VTI | "
        f"{config.SMALL_ACCOUNT_RISK_PER_TRADE:.0%} risk | "
        f"${config.SMALL_ACCOUNT_MAX_NOTIONAL:.0f} max order | "
        f"yield-gate-only | overlap/chunk/co-fire OFF | thinking OFF | "
        f"live safety sim ON"
    )
    print(f"VTI buy & hold: {bench:+.2f}%")
    print(
        f"{'Config':<28} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} "
        f"{'vsVTI':>7} {'Orders':>7} {'Crypto':>7} {'SPY':>5} {'NYSE':>5}"
    )
    print("-" * 92)

    saved_crypto = config.CRYPTO_SLEEVE_ENABLED
    saved_paper_crypto = config.PAPER_CRYPTO_ENABLED
    saved_paper_aggr = config.PAPER_AGGRESSIVE_ENABLED
    saved_chase_env = os.environ.get("PAPER_CHASE_MODE")
    # Live Profile A — not paper chase (avoid PAPER_CRYPTO path from .env)
    config.PAPER_AGGRESSIVE_ENABLED = False
    config.PAPER_CRYPTO_ENABLED = False
    os.environ["PAPER_CHASE_MODE"] = "0"
    rows: list[dict] = []
    try:
        for label, crypto_on in (
            ("Live A (crypto OFF)", False),
            ("Live A (crypto ON)", True),
        ):
            config.CRYPTO_SLEEVE_ENABLED = crypto_on
            result = run_backtest(data, **base_kwargs)
            rows.append(_row(label, result, bench or 0.0))
            release_backtest_memory()
            r = rows[-1]
            print(
                f"{r['label']:<28} "
                f"{r['return_pct']:>+7.2f}% "
                f"{r['sharpe']:>7.2f} "
                f"{r['max_dd_pct']:>7.2f}% "
                f"{r['vs_vti']:>+6.2f}p "
                f"{r['total_orders']:>7d} "
                f"{r['crypto_trades']:>7d} "
                f"{r['spy_trades']:>5d} "
                f"{r['nyse_trades']:>5d}"
            )
    finally:
        config.CRYPTO_SLEEVE_ENABLED = saved_crypto
        config.PAPER_CRYPTO_ENABLED = saved_paper_crypto
        config.PAPER_AGGRESSIVE_ENABLED = saved_paper_aggr
        if saved_chase_env is None:
            os.environ.pop("PAPER_CHASE_MODE", None)
        else:
            os.environ["PAPER_CHASE_MODE"] = saved_chase_env

    print("-" * 92)
    if len(rows) == 2:
        off, on = rows
        print(
            f"Crypto ON vs OFF: return {on['return_pct'] - off['return_pct']:+.2f}pp | "
            f"Sharpe {on['sharpe'] - off['sharpe']:+.2f} | "
            f"MaxDD {on['max_dd_pct'] - off['max_dd_pct']:+.2f}pp | "
            f"orders {on['total_orders'] - off['total_orders']:+d} | "
            f"crypto trades {on['crypto_trades'] - off['crypto_trades']:+d}"
        )


if __name__ == "__main__":
    main()
