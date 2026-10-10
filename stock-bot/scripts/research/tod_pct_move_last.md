# Time-of-day % move report (research only)

No orders, no `.env`, no restart.

**Window:** ~120 calendar days of hourly RTH bars (yfinance).  
**Metric:** mean **forward 1-hour % return** from bars that start in each bucket/hour.  
**Symbols:** GOLD, SPY, VTI, FCX, SMCI, ELF, HALO, EL, DINO

Same vocabulary as `modules/time_of_day.py`: `open` 9:30–9:45 · `first_30m` 9:30–10:00 · `mid_morning` 10:00–11:30 · `midday` 11:30–14:00 · `last_hour` 15:00–16:00 · `close` 15:45–16:00.

## Mean forward-1h % by session bucket

| Symbol | open | first_30m | mid_morning | midday | last_hour | close |
|---|---:|---:|---:|---:|---:|---:|
| GOLD | +0.012% | +0.012% | -0.027% | +0.026% | +0.183% | +0.183% |
| SPY | -0.001% | -0.001% | +0.002% | -0.005% | +0.091% | +0.091% |
| VTI | -0.001% | -0.001% | +0.001% | -0.005% | +0.094% | +0.094% |
| FCX | +0.049% | +0.049% | -0.014% | +0.021% | +0.119% | +0.119% |
| SMCI | +0.054% | +0.054% | -0.005% | -0.030% | +0.217% | +0.217% |
| ELF | -0.019% | -0.019% | -0.051% | -0.030% | +0.207% | +0.207% |
| HALO | -0.027% | -0.027% | -0.050% | +0.034% | +0.133% | +0.133% |
| EL | -0.015% | -0.015% | -0.018% | -0.038% | +0.300% | +0.300% |
| DINO | +0.095% | +0.095% | +0.061% | -0.002% | +0.173% | +0.173% |

## Mean forward-1h % by ET clock hour

| Symbol | 9:00 | 10:00 | 11:00 | 12:00 | 13:00 | 14:00 | 15:00 |
|---|---:|---:|---:|---:|---:|---:|---:|
| GOLD | +0.012% | -0.027% | -0.040% | +0.018% | +0.022% | +0.104% | +0.183% |
| SPY | -0.001% | +0.002% | -0.006% | +0.011% | -0.014% | -0.010% | +0.091% |
| VTI | -0.001% | +0.001% | -0.005% | +0.010% | -0.015% | -0.009% | +0.094% |
| FCX | +0.049% | -0.014% | +0.040% | -0.005% | +0.054% | -0.007% | +0.119% |
| SMCI | +0.054% | -0.005% | -0.034% | +0.020% | -0.092% | -0.014% | +0.217% |
| ELF | -0.019% | -0.051% | -0.042% | +0.023% | -0.044% | -0.056% | +0.207% |
| HALO | -0.027% | -0.050% | +0.030% | +0.088% | +0.014% | +0.004% | +0.133% |
| EL | -0.015% | -0.018% | -0.035% | -0.018% | -0.015% | -0.082% | +0.300% |
| DINO | +0.095% | +0.061% | -0.008% | +0.008% | -0.026% | +0.020% | +0.173% |

## GOLD focus

- Best bucket: **last_hour** (+0.183%, n=187)
- Worst bucket: **mid_morning** (-0.027%, n=190)

| Bucket | n | mean 1h % | median | win% |
|---|---:|---:|---:|---:|
| open | 190 | +0.012% | -0.092% | 46.8% |
| first_30m | 190 | +0.012% | -0.092% | 46.8% |
| mid_morning | 190 | -0.027% | +0.000% | 48.9% |
| midday | 757 | +0.026% | +0.000% | 49.9% |
| last_hour | 187 | +0.183% | -0.063% | 49.2% |
| close | 187 | +0.183% | -0.063% | 49.2% |

## How to use (research)

- Track which hours/buckets carry drift vs chop — not a Monday-open predictor.
- Paper already soft-uses TOD via `TIME_OF_DAY_ANALYSIS` / Markov blend; this report is the plain % view.
- Re-run anytime: `python scripts/research/tod_pct_move_report.py --days 120`

## Full stack TOD

Broader 365d study (Sharpe + recommendations): `python scripts/analysis/run_tod_analysis.py --days 365`
