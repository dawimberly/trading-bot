# A2B1 on S&P PIT pick_15 (research only)

No orders, no `.env`. 10 bps RT, sleeve 0.95, marked-to-market DD.
Knob: flatten after prior close ≥8%, next open, skip Monday. Open gap is open[t]/close[t-1] from one OHLC source (Alpaca, else yfinance).

**Verdict:** HOLD

## Window 365d: 2025-09-16 → 2026-10-10

Coverage 100.0% (299/299 members).

| policy | book% | maxDD% | Sharpe | trades | fades | vs pick_15 |
|---|---:|---:|---:|---:|---:|---|
| pick_15 | +95.53 | 13.79 | 1.91 | 325 | 0 | base |
| a2b1_skip_mon | +86.68 | 14.51 | 1.88 | 412 | 84 | FAIL (-8.8pp ret, +0.7pp DD) |

Fades by weekday: Mon=0, Tue=0, Wed=0, Thu=24, Fri=32

## Window full_db_1568bars: 2022-06-11 → 2026-10-10

Coverage 100.0% (307/307 members).

| policy | book% | maxDD% | Sharpe | trades | fades | vs pick_15 |
|---|---:|---:|---:|---:|---:|---|
| pick_15 | +134.38 | 19.61 | 1.06 | 655 | 0 | base |
| a2b1_skip_mon | +112.14 | 18.18 | 0.99 | 785 | 142 | FAIL (-22.2pp ret, -1.4pp DD) |

Fades by weekday: Mon=0, Tue=23, Wed=31, Thu=50, Fri=38

## Deflated Sharpe (family ledger)

| window | policy | Sharpe (ann) | E[max SR] | trials | DSR | PSR>0 |
|---|---|---:|---:|---:|---:|---:|
| 365d | pick_15 | 1.913 | 0.811 | 10 | 0.9147 | 0.9913 |
| 365d | a2b1_skip_mon | 1.879 | 0.811 | 10 | 0.9046 | 0.9893 |
| full_db_1568bars | pick_15 | 1.057 | 0.811 | 10 | 0.725 | 0.9949 |
| full_db_1568bars | a2b1_skip_mon | 0.987 | 0.811 | 10 | 0.6652 | 0.9916 |

## Verdict rule

- Promote only if book% > pick_15 **and** maxDD not worse by > 2.0 pp, both windows.
- Already wired on Lab 8. A PASS here is the isolated-walk second window, not a Medium/live promote.
- A FAIL keeps Lab as-is (do not silently turn A2B1 off from this file).
