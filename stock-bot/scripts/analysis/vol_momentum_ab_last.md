# Momentum crash knobs on S&P PIT (research only)

No orders, no `.env`. 10 bps RT, sleeve 0.95, pick_15, marked-to-market DD.
Universe is S&P 500 membership by date.

`risk_adj` = rank by distance-to-MA / 20d vol. `vol_12` = sleeve × min(1, 12% / 126-bar book vol).

## Window 365d_recent: 2025-04-11 → 2026-10-09

Coverage 97.4% (530/544 members).

| policy | book% | maxDD% | Sharpe | trades | vs pick_15 |
|---|---:|---:|---:|---:|---|
| pick_15 | +100.16 | 18.69 | 1.66 | 338 | base |
| risk_adj | +39.10 | 12.97 | 1.19 | 375 | FAIL (-61.1pp ret, -5.7pp DD) |
| vol_12 | +69.50 | 10.63 | 1.8 | 338 | FAIL (-30.7pp ret, -8.1pp DD) |

## Window 365d_prior: 2023-10-02 → 2025-04-10

Coverage 95.4% (515/540 members).

| policy | book% | maxDD% | Sharpe | trades | vs pick_15 |
|---|---:|---:|---:|---:|---|
| pick_15 | +6.17 | 22.77 | 0.27 | 369 | base |
| risk_adj | +23.09 | 12.81 | 0.96 | 389 | PASS (+16.9pp ret, -10.0pp DD) |
| vol_12 | +11.11 | 16.33 | 0.49 | 369 | PASS (+4.9pp ret, -6.4pp DD) |

## Deflated Sharpe (family ledger)

| window | policy | Sharpe (ann) | E[max SR] | trials | DSR | PSR>0 |
|---|---|---:|---:|---:|---:|---:|
| 365d_recent | pick_15 | 1.657 | 1.535 | 64 | 0.5592 | 0.9788 |
| 365d_recent | risk_adj | 1.189 | 1.535 | 64 | 0.3339 | 0.9298 |
| 365d_recent | vol_12 | 1.801 | 1.535 | 64 | 0.6272 | 0.9861 |
| 365d_prior | pick_15 | 0.269 | 1.535 | 64 | 0.0603 | 0.6291 |
| 365d_prior | risk_adj | 0.959 | 1.535 | 64 | 0.244 | 0.8758 |
| 365d_prior | vol_12 | 0.487 | 1.535 | 64 | 0.1 | 0.7242 |

## Verdict rule

- Promote a knob only if book% > pick_15 **and** maxDD not worse by > 2.0 pp, both windows.
- One knob. Do not combine risk_adj + vol_12 from this file.
