# Refinements Grid A/B Results

Generated: 2026-06-02 00:55

Tests SPY MA-break exit, SPY ladder sizing, NYSE beta scaling, and co-fire / adaptive-chunk tuning on the recommended live stack.

Variants: 9 | Windows: 2000d, 500d

## Window 2000d

| Variant | Return | Sharpe | Max DD | SPY | NYSE | Crypto | Orders |
|---------|-------:|-------:|-------:|----:|-----:|-------:|-------:|
| spy_ladder | +4310.50% | 1.38 | -58.06% | 243 | 110 | 141 | 494 |
| cofire_5 | +3993.67% | 1.37 | -52.89% | 81 | 76 | 135 | 292 |
| chunk_7 | +3967.46% | 1.36 | -52.95% | 77 | 70 | 134 | 281 |
| nyse_beta | +3844.42% | 1.36 | -52.71% | 78 | 85 | 135 | 298 |
| chunk_6 | +3646.24% | 1.36 | -52.94% | 76 | 70 | 134 | 280 |
| baseline | +3740.10% | 1.35 | -54.03% | 80 | 71 | 140 | 291 |
| spy_exit_off | +3740.10% | 1.35 | -54.03% | 80 | 71 | 140 | 291 |
| cofire_8 | +3548.96% | 1.34 | -53.52% | 71 | 73 | 140 | 284 |
| chunk_4 | +3108.60% | 1.33 | -53.91% | 77 | 72 | 140 | 289 |

## Window 500d

| Variant | Return | Sharpe | Max DD | SPY | NYSE | Crypto | Orders |
|---------|-------:|-------:|-------:|----:|-----:|-------:|-------:|
| nyse_beta | +1.33% | 0.14 | -24.17% | 25 | 28 | 10 | 63 |
| cofire_8 | -2.19% | 0.06 | -24.66% | 30 | 24 | 10 | 64 |
| chunk_4 | -2.33% | 0.06 | -24.41% | 33 | 26 | 12 | 71 |
| baseline | -2.60% | 0.05 | -24.80% | 32 | 26 | 10 | 68 |
| spy_exit_off | -2.60% | 0.05 | -24.80% | 32 | 26 | 10 | 68 |
| cofire_5 | -2.96% | 0.05 | -24.87% | 34 | 27 | 11 | 72 |
| spy_ladder | -3.65% | 0.03 | -24.68% | 100 | 30 | 12 | 142 |
| chunk_6 | -3.81% | 0.03 | -24.96% | 33 | 21 | 10 | 64 |
| chunk_7 | -4.17% | 0.03 | -25.61% | 31 | 17 | 10 | 58 |

## Recommendations

### Window 2000d
- Baseline: return +3740.10%, Sharpe 1.35, max DD -54.03%
- Best Sharpe: **spy_ladder** — return +4310.50%, Sharpe 1.38, max DD -58.06% (SPY/NYSE/crypto signals 243/110/141)
- vs baseline: Sharpe +0.03, return +570.40 pp, max DD -4.03 pp

### Window 500d
- Baseline: return -2.60%, Sharpe 0.05, max DD -24.80%
- Best Sharpe: **nyse_beta** — return +1.33%, Sharpe 0.14, max DD -24.17% (SPY/NYSE/crypto signals 25/28/10)
- vs baseline: Sharpe +0.09, return +3.93 pp, max DD +0.63 pp

### Suggested config overrides

Best combo on **2000d** (`spy_ladder`):
- `SPY_EXIT_ON_MA_BREAK=true`
- `SPY_LADDER_SIZING_ENABLED=true`

Baseline stack: yield-gate-only game plan, NYSE overlap 0.80, adaptive chunk + co-fire, halt resume 8% + liquidate, SPY_EXIT_ON_MA_BREAK=true.