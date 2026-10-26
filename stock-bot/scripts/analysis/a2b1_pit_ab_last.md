# A2B1 on S&P PIT pick_15 (research only)

No orders, no `.env`. 10 bps RT, sleeve 0.95, marked-to-market DD.
Knob: flatten after prior close ≥8%, next open, skip Monday. Open gap is open[t]/close[t-1] from one OHLC source (Alpaca, else yfinance).

**Verdict:** CANDIDATE

## Window 365d_recent: 2025-04-11 → 2026-10-09

Coverage 97.4% (530/544 members).

| policy | book% | maxDD% | Sharpe | trades | fades | vs pick_15 |
|---|---:|---:|---:|---:|---:|---|
| pick_15 | +100.16 | 18.69 | 1.66 | 338 | 0 | base |
| a2b1_skip_mon | +108.09 | 15.99 | 1.9 | 442 | 118 | PASS (+7.9pp ret, -2.7pp DD) |

Fades by weekday: Mon=0, Tue=23, Wed=26, Thu=41, Fri=28

## Window 365d_prior: 2023-10-02 → 2025-04-10

Coverage 95.4% (515/540 members).

| policy | book% | maxDD% | Sharpe | trades | fades | vs pick_15 |
|---|---:|---:|---:|---:|---:|---|
| pick_15 | +6.17 | 22.77 | 0.27 | 369 | 0 | base |
| a2b1_skip_mon | +6.65 | 19.02 | 0.29 | 396 | 37 | PASS (+0.5pp ret, -3.8pp DD) |

Fades by weekday: Mon=0, Tue=2, Wed=9, Thu=13, Fri=13

## Deflated Sharpe (family ledger)

| window | policy | Sharpe (ann) | E[max SR] | trials | DSR | PSR>0 |
|---|---|---:|---:|---:|---:|---:|
| 365d_recent | pick_15 | 1.657 | 1.511 | 58 | 0.5712 | 0.9788 |
| 365d_recent | a2b1_skip_mon | 1.896 | 1.511 | 58 | 0.6819 | 0.99 |
| 365d_prior | pick_15 | 0.269 | 1.511 | 58 | 0.064 | 0.6291 |
| 365d_prior | a2b1_skip_mon | 0.292 | 1.511 | 58 | 0.0671 | 0.6399 |

## Verdict rule

- Promote only if book% > pick_15 **and** maxDD not worse by > 2.0 pp, both windows.
- Already wired on Lab 8. A PASS here is the isolated-walk second window, not a Medium/live promote.
- A FAIL keeps Lab as-is (do not silently turn A2B1 off from this file).
