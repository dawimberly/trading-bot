# Open-price predict backtest (research only)

No orders, no `.env`, no restart.

**Days requested:** 252  
**Symbols:** GOLD, FCX, SMCI, ELF, HALO, SPY  
**Train min (walk-forward):** 40 bars  
**Gap-skip threshold:** 2% (matches paper quality skip)

## Models

| Name | Rule |
|---|---|
| baseline | `pred_open = prior_close` |
| gap_persist | `prior_close * (1 + a * prior_gap)` — `a` expanding OLS |
| ridge_lag | ridge on ret_1d, prior_gap, range_pct, spy_gap_lag1 → gap → open |

## Results (lower MAE wins; must beat baseline)

### GOLD  (OOS n=210, 2025-11-04 → 2026-09-04)

| Model | MAE $ | MedAE $ | MAPE | Gap MAE (pp) | Gap corr | Beats baseline? |
|---|---:|---:|---:|---:|---:|:---:|
| `baseline_prior_close` | 0.5557 | 0.3983 | 1.291% | 1.2977 | nan | — |
| `gap_persist` | 0.5571 | 0.3978 | 1.295% | 1.3017 | -0.2084 | no |
| `ridge_lag` | 0.5709 | 0.4267 | 1.327% | 1.3332 | 0.049 | no |

Last completed session **2026-09-04**: prior_close=41.48, actual_open=41.0 (gap -1.16%). Preds — baseline=41.48, gap_persist=41.5119, ridge=41.6277.

Gap-skip decision (ridge, `|pred|>2%`): signals=1, precision=0.0, recall=0.0.

### FCX  (OOS n=210, 2025-11-04 → 2026-09-04)

| Model | MAE $ | MedAE $ | MAPE | Gap MAE (pp) | Gap corr | Beats baseline? |
|---|---:|---:|---:|---:|---:|:---:|
| `baseline_prior_close` | 1.0774 | 0.8539 | 1.801% | 1.8047 | nan | — |
| `gap_persist` | 1.0785 | 0.8367 | 1.803% | 1.8059 | -0.0993 | no |
| `ridge_lag` | 1.1095 | 0.8293 | 1.860% | 1.8626 | -0.0695 | no |

Last completed session **2026-09-04**: prior_close=72.56, actual_open=71.72 (gap -1.16%). Preds — baseline=72.56, gap_persist=72.549, ridge=72.5573.

Gap-skip decision (ridge, `|pred|>2%`): signals=2, precision=1.0, recall=0.029.

### SMCI  (OOS n=210, 2025-11-04 → 2026-09-04)

| Model | MAE $ | MedAE $ | MAPE | Gap MAE (pp) | Gap corr | Beats baseline? |
|---|---:|---:|---:|---:|---:|:---:|
| `baseline_prior_close` | 0.6847 | 0.4400 | 2.150% | 2.1201 | nan | — |
| `gap_persist` | 0.6896 | 0.4461 | 2.162% | 2.1323 | -0.0421 | no |
| `ridge_lag` | 0.7012 | 0.4514 | 2.211% | 2.184 | 0.0469 | no |

Last completed session **2026-09-04**: prior_close=37.87, actual_open=38.16 (gap +0.77%). Preds — baseline=37.87, gap_persist=37.8737, ridge=37.8412.

Gap-skip decision (ridge, `|pred|>2%`): signals=7, precision=0.2857, recall=0.0278.

### ELF  (OOS n=210, 2025-11-04 → 2026-09-04)

| Model | MAE $ | MedAE $ | MAPE | Gap MAE (pp) | Gap corr | Beats baseline? |
|---|---:|---:|---:|---:|---:|:---:|
| `baseline_prior_close` | 0.9749 | 0.6300 | 1.242% | 1.2119 | nan | — |
| `gap_persist` | 1.0279 | 0.6339 | 1.312% | 1.2821 | 0.0431 | no |
| `ridge_lag` | 1.0737 | 0.6501 | 1.370% | 1.3404 | 0.0499 | no |

Last completed session **2026-09-04**: prior_close=107.41, actual_open=107.07 (gap -0.32%). Preds — baseline=107.41, gap_persist=107.5168, ridge=107.622.

Gap-skip decision (ridge, `|pred|>2%`): signals=2, precision=0.0, recall=0.0.

### HALO  (OOS n=210, 2025-11-04 → 2026-09-04)

| Model | MAE $ | MedAE $ | MAPE | Gap MAE (pp) | Gap corr | Beats baseline? |
|---|---:|---:|---:|---:|---:|:---:|
| `baseline_prior_close` | 0.4580 | 0.3200 | 0.617% | 0.6191 | nan | — |
| `gap_persist` | 0.4605 | 0.3125 | 0.619% | 0.6214 | -0.0756 | no |
| `ridge_lag` | 0.4660 | 0.2947 | 0.625% | 0.6269 | 0.0224 | no |

Last completed session **2026-09-04**: prior_close=110.76, actual_open=109.91 (gap -0.77%). Preds — baseline=110.76, gap_persist=110.7614, ridge=110.7661.

Gap-skip decision (ridge, `|pred|>2%`): signals=1, precision=0.0, recall=0.0.

### SPY  (OOS n=210, 2025-11-04 → 2026-09-04)

| Model | MAE $ | MedAE $ | MAPE | Gap MAE (pp) | Gap corr | Beats baseline? |
|---|---:|---:|---:|---:|---:|:---:|
| `baseline_prior_close` | 2.8560 | 2.2530 | 0.407% | 0.4073 | nan | — |
| `gap_persist` | 2.8780 | 2.3162 | 0.410% | 0.4105 | -0.0566 | no |
| `ridge_lag` | 2.8687 | 2.1629 | 0.409% | 0.4093 | 0.0929 | no |

Last completed session **2026-09-04**: prior_close=773.17, actual_open=772.01 (gap -0.15%). Preds — baseline=773.17, gap_persist=773.1986, ridge=772.7146.

Gap-skip decision (ridge, `|pred|>2%`): signals=0, precision=None, recall=None.

## Roll-up

Model variants that beat baseline MAE: **0 / 12** (across gap_persist + ridge × names).

## Verdict rule

Promote nothing. If models rarely beat `prior_close`, leave paper alone.

## Non-goals

- No after-hours feed (yfinance daily only).
- No live Telegram / no paper gate change.
- Does not predict the *close* — only the next open.
