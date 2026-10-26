# NYSE MA70 walk on honest universes (research only)

No orders, no `.env`, no restart. 10 bps round trip, sleeve 0.95, drawdown marked to market.

`current` = runtime list incl. today's screener JSON (look-ahead). `frozen` = static lists only. `sp500` = S&P 500 membership by date (`data/sp500_ticker_start_end.csv`).

## Window 365d_recent: 2025-04-11 -> 2026-10-09

`current`: 312 names
`frozen`: 231 names
`sp500`: 530 names with data; PIT members in window 544, with price data 530 (97.4%). Missing names are the remaining survivorship gap: ANSS, CTLT, CTRA, DAY, DFS, FI, HES, HOLX, IPG, JNPR, K, MMC...

| policy | current book% / DD% / Sharpe / trades | frozen book% / DD% / Sharpe / trades | sp500 book% / DD% / Sharpe / trades | gate (sp500) |
|---|---|---|---|---|
| pick_8 | +431.6 / 20.1 / 2.52 / 184 | +86.5 / 23.1 / 1.32 / 187 | +167.3 / 26.7 / 1.97 / 184 | FAIL |
| pick_15 | +265.9 / 14.4 / 2.51 / 349 | +124.5 / 13.7 / 2.06 / 320 | +100.2 / 18.7 / 1.66 / 338 | base |
| pick_25 | +264.5 / 12.8 / 2.98 / 544 | +86.1 / 12.0 / 1.96 / 563 | +68.2 / 17.4 / 1.47 / 573 | FAIL |
| a2b1_skip_mon | +243.9 / 11.7 / 2.68 / 577 | +90.1 / 10.2 / 1.84 / 446 | +108.1 / 16.0 / 1.9 / 442 | PASS |
| fat_loser_drip | +33.4 / 6.0 / 1.45 / 840 | +37.9 / 5.2 / 2.12 / 699 | +24.2 / 6.6 / 1.39 / 807 | FAIL |
| friday_flatten | +95.3 / 26.4 / 1.44 / 1428 | +32.3 / 26.6 / 0.82 / 1430 | +28.1 / 33.7 / 0.66 / 1413 | FAIL |
| take_1r | +322.2 / 16.4 / 2.81 / 1160 | +104.1 / 18.2 / 1.85 / 1120 | +100.7 / 19.6 / 1.7 / 1061 | PASS |
| lab_8_room | +523.5 / 16.2 / 3.0 / 280 | +111.5 / 14.1 / 1.71 / 177 | +96.2 / 16.0 / 1.5 / 184 | FAIL |

## Window 365d_prior: 2023-10-02 -> 2025-04-10

`current`: 312 names
`frozen`: 231 names
`sp500`: 515 names with data; PIT members in window 540, with price data 515 (95.4%). Missing names are the remaining survivorship gap: ABC, ANSS, ATVI, CDAY, CMA, CTLT, CTRA, DAY, DFS, DISH, FI, FLT...

| policy | current book% / DD% / Sharpe / trades | frozen book% / DD% / Sharpe / trades | sp500 book% / DD% / Sharpe / trades | gate (sp500) |
|---|---|---|---|---|
| pick_8 | -22.3 / 38.9 / -0.59 / 58 | -5.3 / 27.0 / -0.13 / 58 | +1.6 / 29.0 / 0.14 / 200 | FAIL |
| pick_15 | -14.2 / 31.0 / -0.47 / 104 | -9.7 / 22.8 / -0.45 / 113 | +6.2 / 22.8 / 0.27 / 369 | base |
| pick_25 | -11.2 / 24.1 / -0.44 / 182 | -8.2 / 16.6 / -0.46 / 187 | +10.1 / 17.9 / 0.44 / 631 | PASS |
| a2b1_skip_mon | -8.1 / 24.6 / -0.31 / 140 | -4.7 / 20.9 / -0.25 / 135 | +6.7 / 19.0 / 0.29 / 396 | PASS |
| fat_loser_drip | -0.1 / 7.0 / -0.06 / 242 | -2.0 / 7.6 / -0.27 / 237 | +3.6 / 9.3 / 0.25 / 714 | FAIL |
| friday_flatten | -3.1 / 24.8 / -0.09 / 359 | -5.9 / 19.8 / -0.33 / 359 | +2.4 / 17.4 / 0.15 / 1455 | FAIL |
| take_1r | -7.4 / 27.8 / -0.24 / 297 | -7.8 / 21.7 / -0.38 / 274 | +2.4 / 20.9 / 0.14 / 1043 | FAIL |
| lab_8_room | -18.5 / 38.6 / -0.5 / 61 | +1.1 / 21.7 / 0.13 / 51 | +23.9 / 24.8 / 0.71 / 127 | PASS |

## Deflated Sharpe, pick_15 (vs all trials in family ledger)

| window | universe | Sharpe (ann) | E[max SR] (ann) | trials | DSR | PSR>0 |
|---|---|---:|---:|---:|---:|---:|
| 365d_recent | current | 2.513 | 2.283 | 106 | 0.6107 | 0.999 |
| 365d_recent | frozen | 2.06 | 2.283 | 106 | 0.3923 | 0.9942 |
| 365d_recent | sp500 | 1.657 | 2.283 | 106 | 0.2216 | 0.9788 |
| 365d_prior | current | -0.467 | 2.283 | 106 | 0.0002 | 0.2757 |
| 365d_prior | frozen | -0.449 | 2.283 | 106 | 0.0003 | 0.2853 |
| 365d_prior | sp500 | 0.269 | 2.283 | 106 | 0.0068 | 0.6291 |

## Read

- Promote rule unchanged: book% > pick_15 **and** maxDD not worse by > 2 pp, on the `sp500` column, both windows.
- `current` numbers are not evidence. They are what the screener looked like on the day the JSON was written.
- DSR < 0.95 means the Sharpe is not distinguishable from the best of N lucky trials.
