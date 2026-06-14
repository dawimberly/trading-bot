# Risk layer A/B results

## 500 daily bars (2025-03-14 → 2026-05-22)

| Variant | Return % | Sharpe | Max DD % | Halt | Resume | Pause days | Liq trims | Orders |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline | 38.96 | 1.24 | -19.28 | 1 | 0 | 0 | 0 | 88 |
| halt_resume | 38.96 | 1.24 | -19.28 | 5 | 4 | 0 | 0 | 88 |
| halt_resume_liquidate | **54.65** | **1.77** | **-13.48** | 2 | 2 | 0 | 2 | 88 |
| derived_bear_pause | 9.07 | 0.53 | -15.45 | 3 | 3 | 38 | 0 | 88 |
| regime_thresh_0.10 | 38.96 | 1.24 | -19.28 | 5 | 4 | 0 | 0 | 88 |
| combined_best | 7.19 | 0.45 | -14.80 | 4 | 4 | 38 | 2 | 88 |

## Max history (~5033 daily bars)

| Variant | Return % | Sharpe | Max DD % | Halt | Resume | Pause days | Liq trims | Orders |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline | 124.88 | 0.47 | **-25.46** | 1 | 0 | 0 | 0 | — |
| halt_resume | 517.48 | 0.47 | -78.33 | 17 | 16 | 0 | 0 | — |
| halt_resume_liquidate | 800.99 | 0.64 | -59.68 | 14 | 13 | 0 | — | — |
| derived_bear_pause | 2436.60 | 0.78 | -43.11 | 40 | 40 | 1597 | 0 | — |
| regime_thresh_0.10 | 517.48 | 0.47 | -78.33 | 17 | 16 | 0 | 0 | — |
| combined_best | 2996.71 | **0.85** | -60.05 | 26 | 26 | 1594 | — | — |

`backtester.py --days 500` (default config): return 38.96%, Sharpe 1.24, max DD -19.28%, with 5 halt / 4 resume events logged.

## Recommendation

**Deploy for live + backtest (recent window):**

1. `HALT_RESUME_DRAWDOWN_PCT=0.08` — avoids permanent lockout after a 10% breach; resume events fire in 2025–2026 stress.
2. `HALT_LIQUIDATE_ON_BREACH=true` and `HALT_TARGET_CASH_PCT=0.25` — on the 500-day window this cut max DD by ~6 pp and raised Sharpe to 1.77 with only two trim cycles.

**Do not enable together on production without more review:**

- `DERIVED_BEAR_PAUSE_ENABLED=true` — blocks entries on 38 days (500d) and ~1,597 days (max history); too coarse for daily bars.
- `REGIME_SENTIMENT_THRESHOLD=0.10` alone did not add pause days in either run (RHYME_B/E still rare); it only matters when combined with derived bear or lower thresholds (e.g. 0.08) in a follow-up test.

**Legacy baseline:** set `HALT_RESUME_DRAWDOWN_PCT=0` to restore never-resume halt behavior.

## Config env vars

- `MAX_DRAWDOWN_PCT` (default 0.10)
- `HALT_RESUME_DRAWDOWN_PCT` (default 0.08; set 0 for legacy never-resume)
- `HALT_LIQUIDATE_ON_BREACH` (default false)
- `HALT_TARGET_CASH_PCT` (default 0.25)
- `REGIME_SENTIMENT_THRESHOLD` (default 0.5 legacy; try 0.10)
- `DERIVED_BEAR_PAUSE_ENABLED` (default false)
- `DERIVED_BEAR_SENTIMENT_THRESHOLD` (default 0.10)
