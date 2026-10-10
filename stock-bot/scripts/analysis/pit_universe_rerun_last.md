# NYSE MA70 walk on honest universes (research only)

No orders, no `.env`, no restart. 10 bps round trip, sleeve 0.95, drawdown marked to market.

`current` = runtime list incl. today's screener JSON (look-ahead). `frozen` = static lists only. `sp500` = S&P 500 membership by date (`data/sp500_ticker_start_end.csv`).

## Window 365d: 2025-09-16 -> 2026-10-10

`current`: 312 names
`frozen`: 231 names
`sp500`: 299 names with data; PIT members in window 299, with price data 299 (100.0%). Missing names are the remaining survivorship gap: ...

| policy | current book% / DD% / Sharpe / trades | frozen book% / DD% / Sharpe / trades | sp500 book% / DD% / Sharpe / trades | gate (sp500) |
|---|---|---|---|---|
| pick_8 | +310.1 / 16.8 / 2.49 / 180 | +80.2 / 17.0 / 1.53 / 176 | +103.5 / 20.4 / 1.67 / 187 | FAIL |
| pick_15 | +246.3 / 13.8 / 2.72 / 341 | +70.8 / 14.5 / 1.63 / 337 | +95.5 / 13.8 / 1.91 / 325 | base |
| pick_25 | +170.3 / 13.3 / 2.57 / 572 | +54.8 / 13.8 / 1.57 / 574 | +71.8 / 12.6 / 1.85 / 549 | FAIL |
| friday_flatten | +89.1 / 22.1 / 1.57 / 1125 | +38.9 / 22.6 / 1.08 / 1113 | +37.8 / 24.9 / 0.99 / 1112 | FAIL |
| take_1r | +229.2 / 15.0 / 2.71 / 1206 | +74.4 / 15.3 / 1.65 / 1150 | +82.3 / 14.3 / 1.72 / 1114 | FAIL |
| lab_8_room | +368.4 / 13.5 / 2.76 / 235 | +102.9 / 15.4 / 1.81 / 161 | +121.5 / 20.8 / 1.9 / 160 | FAIL |

## Window full_db_1568bars: 2022-06-11 -> 2026-10-10

`current`: 312 names
`frozen`: 231 names
`sp500`: 307 names with data; PIT members in window 307, with price data 307 (100.0%). Missing names are the remaining survivorship gap: ...

| policy | current book% / DD% / Sharpe / trades | frozen book% / DD% / Sharpe / trades | sp500 book% / DD% / Sharpe / trades | gate (sp500) |
|---|---|---|---|---|
| pick_8 | +366.3 / 40.9 / 1.19 / 306 | +133.9 / 28.8 / 0.88 / 299 | +165.8 / 23.2 / 0.99 / 371 | FAIL |
| pick_15 | +297.9 / 30.6 / 1.28 / 565 | +103.2 / 22.1 / 0.9 / 572 | +134.4 / 19.6 / 1.06 / 655 | base |
| pick_25 | +244.9 / 24.2 / 1.34 / 945 | +85.9 / 16.0 / 0.92 / 971 | +109.0 / 14.2 / 1.09 / 1030 | FAIL |
| friday_flatten | +135.5 / 24.4 / 0.9 / 1889 | +57.0 / 22.6 / 0.63 / 1893 | +53.0 / 24.9 / 0.58 / 2225 | FAIL |
| take_1r | +298.4 / 27.9 / 1.32 / 1914 | +100.0 / 21.7 / 0.87 / 1829 | +110.9 / 19.9 / 0.94 / 2093 | FAIL |
| lab_8_room | +341.0 / 43.9 / 1.17 / 353 | +144.9 / 23.2 / 0.92 / 258 | +188.7 / 22.7 / 1.1 / 296 | FAIL |

## Deflated Sharpe, pick_15 (vs all trials in family ledger)

| window | universe | Sharpe (ann) | E[max SR] (ann) | trials | DSR | PSR>0 |
|---|---|---:|---:|---:|---:|---:|
| 365d | current | 2.718 | 1.471 | 90 | 0.9421 | 0.9997 |
| 365d | frozen | 1.629 | 1.471 | 90 | 0.5769 | 0.9773 |
| 365d | sp500 | 1.913 | 1.471 | 90 | 0.7086 | 0.9913 |
| full_db_1568bars | current | 1.284 | 1.471 | 90 | 0.3251 | 0.9991 |
| full_db_1568bars | frozen | 0.896 | 1.471 | 90 | 0.0835 | 0.9843 |
| full_db_1568bars | sp500 | 1.057 | 1.471 | 90 | 0.1571 | 0.9949 |

## Read

- Promote rule unchanged: book% > pick_15 **and** maxDD not worse by > 2 pp, on the `sp500` column, both windows.
- `current` numbers are not evidence. They are what the screener looked like on the day the JSON was written.
- DSR < 0.95 means the Sharpe is not distinguishable from the best of N lucky trials.
