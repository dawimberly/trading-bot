"""Today's paper bot performance snapshot.

Run: python scripts/account/today_performance.py
"""

import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modules.alpaca_executor import get_trading_client

JOURNAL = Path(__file__).resolve().parents[2] / "paper_journal.csv"


def main() -> None:
    today = date.today()
    client = get_trading_client()
    account = client.get_account()
    equity_now = float(account.equity)
    cash_now = float(account.cash)

    print(f"=== Paper stock bot — {today.isoformat()} ===\n")
    print(f"Account:  ACTIVE (paper)")
    print(f"Equity:   ${equity_now:,.2f}")
    print(f"Cash:     ${cash_now:,.2f}")
    print(f"Deployed: ${equity_now - cash_now:,.2f} ({(equity_now - cash_now) / equity_now * 100:.1f}%)")

    if JOURNAL.exists():
        j = pd.read_csv(JOURNAL)
        j["ts"] = pd.to_datetime(j["timestamp"])
        day = j[j["ts"].dt.date == today]
        cycles = day[day["event"] == "cycle"]
        if not cycles.empty:
            eq = cycles["equity"].astype(float)
            day_pnl = eq.iloc[-1] - eq.iloc[0]
            day_pct = (eq.iloc[-1] / eq.iloc[0] - 1) * 100 if eq.iloc[0] else 0
            print(f"\nToday (journal):")
            print(f"  Bot cycles:     {len(cycles)}")
            print(f"  Equity at start: ${eq.iloc[0]:,.2f}")
            print(f"  Day P/L:         ${day_pnl:+,.2f} ({day_pct:+.3f}%)")
            print(f"  Regime (last):   {cycles.iloc[-1]['regime']}")
        signals = day[day["event"] == "signal"]
        print(f"  New trades:      {len(signals)} signals today")
        if len(signals):
            for _, r in signals.iterrows():
                print(
                    f"    {r['timestamp']} {r['side']} {r['symbol']} "
                    f"${r.get('notional', '')} ({r.get('regime', '')[:30]})"
                )

    pos = client.get_all_positions()
    print(f"\nOpen positions: {len(pos)}")
    for p in sorted(pos, key=lambda x: -abs(float(x.market_value))):
        print(f"  {p.symbol}: qty {p.qty}  ~${abs(float(p.market_value)):,.2f}")

    # vs 100k paper start
    start = 100_000.0
    total_pnl = equity_now - start
    print(f"\nSince ~$100k paper start: ${total_pnl:+,.2f} ({total_pnl / start * 100:+.2f}%)")


if __name__ == "__main__":
    main()
