"""Compare live paper journal vs aligned fund backtest for the current bot iteration.

Excludes the $100 live-account switch on the last journal day.

Run: python scripts/analysis/paper_iteration_compare.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config
from modules.data_loader import load_close_matrix
from modules.wisdom_evaluator import _daily_equity_from_journal, _metrics_from_equity, simulate_modes
from modules.wisdom_journal import load_journal

PAPER_PERIOD_END = date(2026, 6, 6)


def _paper_window(df: pd.DataFrame) -> tuple[date, date, pd.DataFrame]:
    df = df.dropna(subset=["equity"])
    df = df[df["equity"] > 500]
    start = df["timestamp"].dt.date.min()
    end = min(df["timestamp"].dt.date.max(), PAPER_PERIOD_END)
    window = df[(df["timestamp"].dt.date >= start) & (df["timestamp"].dt.date <= end)]
    return start, end, window


def main() -> None:
    raw = load_journal()
    if raw.empty:
        print("No wisdom journal data.")
        return

    period_start, period_end, df = _paper_window(raw)
    daily = _daily_equity_from_journal(df)
    live = _metrics_from_equity(daily)
    pause_cycles = int(
        df["wisdom_paused"].astype(str).str.lower().isin(("true", "1", "yes")).sum()
    )
    live.update(
        {
            "from_date": str(daily.index.min()),
            "to_date": str(daily.index.max()),
            "start_equity": round(float(daily.iloc[0]), 2),
            "end_equity": round(float(daily.iloc[-1]), 2),
            "daily_samples": len(daily),
            "cycles": len(df),
            "pause_cycles": pause_cycles,
            "mode_end": df["active_mode"].iloc[-1],
            "trades": {
                "crypto": int(df["crypto_trades"].sum()),
                "spy": int(df["spy_trades"].sum()),
                "nyse": int(df["nyse_trades"].sum()),
            },
        }
    )

    simulated = simulate_modes(30, period_start=period_start, period_end=period_end)
    dynamic = simulated.get("dynamic", {})
    baseline = simulated.get("baseline", {})

    data = load_close_matrix(interval="1d")
    vti = data["VTI"].dropna().loc[str(period_start) : str(period_end)]
    vti_ret = round((vti.iloc[-1] / vti.iloc[0] - 1) * 100, 2) if len(vti) >= 2 else None

    cal = pd.date_range(period_start, period_end, freq="D")
    active = set(daily.index)
    offline = [d.date() for d in cal if d.date() not in active]

    offline_drift = None
    offline_pct = None
    if date(2026, 5, 30) in active and date(2026, 6, 3) in active:
        wj = df.copy()
        wj["d"] = wj["timestamp"].dt.date
        may30 = float(wj[wj["d"] == date(2026, 5, 30)]["equity"].iloc[-1])
        jun3 = float(wj[wj["d"] == date(2026, 6, 3)]["equity"].iloc[0])
        offline_drift = round(jun3 - may30, 2)
        offline_pct = round((jun3 / may30 - 1) * 100, 2)

    mode_mix = df.groupby("active_mode").size().sort_values(ascending=False)

    print("=" * 60)
    print("PAPER ITERATION: LIVE vs BACKTEST")
    print("=" * 60)
    print(f"Window:          {period_start} -> {period_end}")
    print(f"Calendar days:   {len(cal)} | Bot online: {len(active)} | Offline: {len(offline)}")
    if offline:
        print(f"Offline days:    {', '.join(str(d) for d in offline)}")
    print(f"Stack in sim:    dynamic + game_plan={config.GAME_PLAN_ENABLED}")
    print()

    print("--- LIVE (wisdom journal, daily last equity) ---")
    print(f"  Start / End:     ${live['start_equity']:,.2f} -> ${live['end_equity']:,.2f}")
    print(f"  Return:          {live['return_pct']:+.2f}%")
    print(f"  Sharpe:          {live['sharpe']:.2f}")
    print(f"  Max drawdown:    {live['max_drawdown_pct']:+.2f}%")
    print(
        f"  Bot cycles:      {live['cycles']:,} "
        f"({pause_cycles:,} paused = {pause_cycles / live['cycles'] * 100:.1f}%)"
    )
    print(f"  Mode at end:     {live['mode_end']}")
    print(f"  Trades (journal): crypto={live['trades']['crypto']} spy={live['trades']['spy']} nyse={live['trades']['nyse']}")
    print(f"  Mode mix (cycles): {dict(mode_mix)}")
    print()

    print("--- BACKTEST (dynamic, same calendar window, daily bars) ---")
    if dynamic:
        print(f"  Return:          {dynamic.get('return_pct', 0):+.2f}%")
        print(f"  Sharpe:          {dynamic.get('sharpe', 0):.2f}")
        print(f"  Max drawdown:    {dynamic.get('max_drawdown_pct', 0):+.2f}%")
        print(f"  Sim orders:      {dynamic.get('orders', 0)}")
        print(f"  Paused days:     {dynamic.get('paused_days', 0)}")
        gap = round(live["return_pct"] - dynamic.get("return_pct", 0), 2)
        print(f"  Live minus sim:  {gap:+.2f} pp")
    print()

    print("--- BACKTEST (baseline / price-only) ---")
    if baseline:
        print(f"  Return:          {baseline.get('return_pct', 0):+.2f}% | orders={baseline.get('orders', 0)}")
    print()

    if vti_ret is not None:
        print(f"--- BENCHMARK (VTI buy-and-hold) ---")
        print(f"  Return:          {vti_ret:+.2f}%")
        print()

    if offline_drift is not None:
        print("--- OFFLINE DRIFT (May 30 -> Jun 3 reopen) ---")
        print(f"  Equity change:   ${offline_drift:+,.2f} ({offline_pct:+.2f}%) with bot off 3 days")
        print()

    print("--- INTERPRETATION ---")
    print(
        "  Live underperformance vs sim is expected when the bot was offline, paused "
        "(governor mode), or signals did not fill on Alpaca paper."
    )
    print(
        "  Trade reconciliation against the current $100 live API will not show "
        "historical paper fills — use paper account keys for fill-level audit."
    )


if __name__ == "__main__":
    main()
