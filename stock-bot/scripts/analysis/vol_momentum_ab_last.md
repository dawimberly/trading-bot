# Momentum crash knobs on S&P PIT (research only)

No orders, no `.env`. 10 bps RT, sleeve 0.95, pick_15, marked-to-market DD.
Universe is S&P 500 membership by date.

`risk_adj` = rank by distance-to-MA / 20d vol. `vol_12` = sleeve × min(1, 12% / 126-bar book vol).

## Window 365d: 2025-09-16 → 2026-10-10

Coverage 100.0% (299/299 members).

| policy | book% | maxDD% | Sharpe | trades | vs pick_15 |
|---|---:|---:|---:|---:|---|
| pick_15 | +95.53 | 13.79 | 1.91 | 325 | base |
| risk_adj | +34.12 | 9.68 | 1.25 | 385 | FAIL (-61.4pp ret, -4.1pp DD) |
| vol_12 | +73.02 | 9.12 | 1.96 | 325 | FAIL (-22.5pp ret, -4.7pp DD) |

## Window full_db_1568bars: 2022-06-11 → 2026-10-10

Coverage 100.0% (307/307 members).

| policy | book% | maxDD% | Sharpe | trades | vs pick_15 |
|---|---:|---:|---:|---:|---|
| pick_15 | +134.38 | 19.61 | 1.06 | 655 | base |
| risk_adj | +31.65 | 20.16 | 0.49 | 744 | FAIL (-102.7pp ret, +0.6pp DD) |
| vol_12 | +105.59 | 14.34 | 1.13 | 655 | FAIL (-28.8pp ret, -5.3pp DD) |

## Deflated Sharpe (family ledger)

| window | policy | Sharpe (ann) | E[max SR] | trials | DSR | PSR>0 |
|---|---|---:|---:|---:|---:|---:|
| 365d | pick_15 | 1.913 | 0.726 | 6 | 0.93 | 0.9913 |
| 365d | risk_adj | 1.251 | 0.726 | 6 | 0.7455 | 0.9424 |
| 365d | vol_12 | 1.964 | 0.726 | 6 | 0.9379 | 0.9927 |
| full_db_1568bars | pick_15 | 1.057 | 0.726 | 6 | 0.789 | 0.9949 |
| full_db_1568bars | risk_adj | 0.491 | 0.726 | 6 | 0.2849 | 0.8821 |
| full_db_1568bars | vol_12 | 1.134 | 0.726 | 6 | 0.8382 | 0.997 |

## Verdict rule

- Promote a knob only if book% > pick_15 **and** maxDD not worse by > 2.0 pp, both windows.
- One knob. Do not combine risk_adj + vol_12 from this file.
