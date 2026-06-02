# NYSE anti-overlap A/B

Benchmark (VTI) on full window: 184.62%

Co-fire = SPY and NYSE both fired on same bar (cooldown-aware). Pick swaps = filtered top ≠ raw top when SPY sleeve active.

## full (2019-03-27 → 2026-05-22)

| Variant | Return | Sharpe | Max DD | Co-fire days | Pick swaps | Avg NYSE-SPY corr | Tech % (SPY on) |
|---|---|---|---|---|---|---|---|
| baseline | +741.32% | 1.16 | -42.10% | 230 | 0 | 0.281 | 63.5% |
| corr_0.75 | +155.11% | 0.95 | -22.22% | 232 | 62 | 0.205 | 52.6% |
| corr_0.80 | +155.11% | 0.95 | -22.22% | 232 | 61 | 0.214 | 51.3% |
| corr_0.85 | +162.75% | 0.93 | -21.65% | 232 | 53 | 0.238 | 56.5% |
| sector_tech_cap | +741.32% | 1.16 | -42.10% | 230 | 0 | 0.281 | 63.5% |

## recent_750d (2023-05-26 → 2026-05-22)

| Variant | Return | Sharpe | Max DD | Co-fire days | Pick swaps | Avg NYSE-SPY corr | Tech % (SPY on) |
|---|---|---|---|---|---|---|---|
| baseline | +133.96% | 1.04 | -40.68% | 158 | 0 | 0.47 | 53.8% |
| corr_0.75 | +108.54% | 0.91 | -41.80% | 158 | 81 | 0.417 | 27.2% |
| corr_0.80 | +108.54% | 0.91 | -41.80% | 158 | 81 | 0.417 | 27.2% |
| corr_0.85 | +108.54% | 0.91 | -41.80% | 158 | 81 | 0.417 | 27.2% |
| sector_tech_cap | +133.96% | 1.04 | -40.68% | 158 | 0 | 0.47 | 53.8% |

## Recommendation

Window: **recent_750d**. Best corr-threshold variant: **corr_0.75** (Sharpe 0.91 vs baseline 1.04).
Sector tech-cap: Sharpe 1.04, tech picks 53.8% (baseline 53.8%).

**Recommend baseline (no corr filter).** Corr filter costs ~25 pp return and 0.13 Sharpe on recent_750d; co-fire days unchanged (158). Lower avg NYSE-SPY corr (0.417 vs 0.47) but worse portfolio metrics. NYSE_SECTOR_TECH_CAP=1 has no effect with max_trades=1 — test via rebalance top-3.