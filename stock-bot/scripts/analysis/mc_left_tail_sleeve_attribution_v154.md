# Left-Tail Sleeve Attribution — Dynamic VTI 40–75% (365d)

**Date:** 2026-07-25
**MC export:** `monte_carlo_v154_sleeve_attribution_365.json` · 50 runs · seed 42 · no-thinking · peak/trough sleeve snapshots

## Method

- Fresh MC only (no dependency on prior wipeout run IDs — seed replay does not reproduce −40% paths when the universe changes).
- Profile: paper-aggressive, Dynamic VTI locked 40–75%, 365d, 50 runs, seed 42, noise 0.01, regime_noise 0.1, thinking OFF.
- `track_sleeve_path`: snapshot **peak equity** and **max-DD trough** only (not every bar). `PAPER_DEPLOY_DEBUG` forced off for the run.
- Worst 5 by **max_drawdown_pct** (performance metric). Sleeve mix is from the peak/trough snapshot nearest that DD; `dominant_sleeve` = largest active marked % at trough; bleed = most negative peak→trough marked-USD delta.

**Limitation:** Seed-replay of older wipeout IDs diverges when universe width changes; this report uses worst-5 max-DD from the fresh MC only.

## Short table

| run_id | trough_dd | vti_pct | dominant_sleeve | peak vs trough shift |
|-------:|----------:|--------:|-----------------|----------------------|
| 30 | -52.33% | 64.95 | nyse_momentum | VTI 56→65%; active 40→35%; Δspy $-983 |
| 40 | -39.75% | 57.37 | spy | VTI 57→57%; active 43→43%; Δspy $-231 |
| 49 | -36.39% | 57.35 | nyse_momentum | VTI 68→57%; active 30→43%; Δstat_arb $+0 |
| 1 | -35.71% | 52.74 | nyse_momentum | VTI 53→53%; active 40→39%; Δspy $-873 |
| 32 | -31.34% | 57.67 | nyse_momentum | VTI 57→58%; active 40→41%; Δspy $-2,307 |

## Trough sleeve mix

| Run | Ret% | Trough DD | VTI% | Active% | Cash% | SPY% | NYSE% | StatArb% | Shorts% | Bleed Δ |
|----:|-----:|----------:|-----:|--------:|------:|-----:|------:|---------:|--------:|---------|
| 30 | -51.99 | -52.33% | 64.95 | 35.4 | -0.35 | 14.43 | 20.97 | 0.0 | 0.0 | spy |
| 40 | -38.84 | -39.75% | 57.37 | 42.63 | 0.0 | 21.49 | 21.14 | 0.0 | 0.0 | spy |
| 49 | -35.2 | -36.39% | 57.35 | 42.64 | 0.01 | 19.56 | 23.08 | 0.0 | 0.0 | stat_arb |
| 1 | -34.98 | -35.71% | 52.74 | 39.45 | 7.81 | 13.1 | 26.35 | 0.0 | 0.0 | spy |
| 32 | -28.31 | -31.34% | 57.67 | 41.22 | 1.11 | 0.0 | 41.22 | 0.0 | 0.0 | spy |

## Peak→trough marked USD deltas

| Run | Δ equity | Δ VTI | Δ SPY | Δ NYSE | Δ StatArb | Δ Shorts | Δ Cash |
|----:|---------:|------:|------:|-------:|----------:|---------:|-------:|
| 30 | -879 | 208 | -983 | 178 | 0 | 0 | -462 |
| 40 | -770 | -399 | -231 | -139 | 0 | 0 | -1 |
| 49 | -941 | -1,599 | 361 | 526 | 0 | 0 | -149 |
| 1 | -971 | -540 | -873 | 406 | 0 | 0 | 37 |
| 32 | -929 | -424 | -2,307 | 2,036 | 0 | 0 | -210 |

## Cross-run summary

### Findings

- Sample: **50** MC paths (seed=42, days=365), peak/trough sleeve snapshots only.
- Worst 5 ranked by **max drawdown** (not prior wipeout IDs).
- High VTI at trough (≥55%): **4/5** (mean trough VTI **58.0%**, mean active **40.3%**).
- Dominant marked sleeve at trough: {'nyse_momentum': 4, 'spy': 1}.
- Largest peak→trough marked-USD bleed: {'spy': 4, 'stat_arb': 1}.
- **Yes — worst paths again show high VTI at the trough**, so the left tail is not explained by Dynamic VTI collapsing to the 40% floor.
