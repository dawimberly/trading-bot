# Full Strategy Experiment Report

Generated: 2026-06-29T11:40:27.572631+00:00

## Run settings

- Days: 365
- Paper aggressive: True
- Monte Carlo runs (top configs): 50
- Walk-forward folds: 4
- Total elapsed: 23233.3s

## Ranked configurations

| Rank | Phase | Config | Return % | Sharpe | MaxDD % | Composite | p5 ret |
|-----:|-------|--------|---------:|-------:|--------:|----------:|-------:|
| 1 | monte_carlo | minimal_features | +34.78 | 1.31 | -21.73 | 11.094 | -56.20 |
| 2 | monte_carlo | core_stack_no_deep | +33.19 | 1.31 | -18.61 | 10.620 | -36.57 |
| 3 | monte_carlo | deep_history_fixed_core | +32.02 | 1.33 | -20.32 | 10.275 | -53.71 |
| 4 | combos | minimal_features | +28.51 | 1.54 | -6.62 | 9.330 | — |
| 5 | combos | core_stack_no_deep | +27.75 | 1.51 | -7.05 | 9.087 | — |
| 6 | monte_carlo | all_on_no_stat_arb | +22.36 | 1.05 | -19.27 | 7.237 | -37.49 |
| 7 | combos | all_on_no_regime_sizing | +20.67 | 1.32 | -6.55 | 6.870 | — |
| 8 | combos | all_on_no_thinking | +20.51 | 1.30 | -6.86 | 6.812 | — |
| 9 | combos | all_on_basic_risk | +20.43 | 1.30 | -6.91 | 6.788 | — |
| 10 | combos | all_on_no_stat_arb | +20.29 | 1.29 | -6.92 | 6.741 | — |
| 11 | combos | all_on_no_positioning | +20.21 | 1.29 | -6.92 | 6.717 | — |
| 12 | combos | deep_history_fixed_core | +20.15 | 1.28 | -7.02 | 6.694 | — |
| 13 | combos | deep_history_no_thinking | +20.12 | 1.28 | -7.02 | 6.685 | — |
| 14 | combos | all_on_no_hybrid_rebalance | +18.94 | 1.30 | -7.09 | 6.342 | — |
| 15 | walk_forward | minimal_features | +16.30 | 1.39 | -6.38 | 5.593 | — |
| 16 | walk_forward | core_stack_no_deep | +16.15 | 1.40 | -6.50 | 5.555 | — |
| 17 | monte_carlo | all_on_no_positioning | +16.02 | 0.75 | -21.59 | 5.185 | -50.51 |
| 18 | walk_forward | all_on_no_thinking | +9.52 | 0.93 | -6.35 | 3.338 | — |
| 19 | walk_forward | all_on_no_regime_sizing | +9.52 | 0.92 | -6.25 | 3.337 | — |
| 20 | walk_forward | all_on_basic_risk | +9.44 | 0.92 | -6.39 | 3.309 | — |
| 21 | walk_forward | all_on_no_stat_arb | +9.33 | 0.90 | -6.39 | 3.269 | — |
| 22 | walk_forward | all_on_no_positioning | +9.31 | 0.90 | -6.40 | 3.262 | — |
| 23 | walk_forward | deep_history_fixed_core | +9.24 | 0.90 | -6.47 | 3.241 | — |
| 24 | monte_carlo | all_on_no_regime_sizing | +8.89 | 0.47 | -22.48 | 2.904 | -44.26 |
| 25 | ablation | ablate_no_deep_history_indicators | +6.37 | 1.40 | -4.85 | 2.638 | — |
| 26 | monte_carlo | all_on_basic_risk | +6.02 | 0.36 | -23.47 | 1.990 | -50.98 |
| 27 | ablation | ablate_no_hybrid_rebalance | +2.42 | 1.16 | -3.79 | 1.364 | — |
| 28 | ablation | ablate_no_positioning_overlay | +2.47 | 0.93 | -3.94 | 1.264 | — |
| 29 | ablation | ablate_no_dynamic_core | +2.20 | 0.83 | -4.23 | 1.137 | — |
| 30 | baseline | baseline_strong | +2.09 | 0.79 | -3.95 | 1.087 | — |
| 31 | ablation | ablate_no_stat_arb | +2.09 | 0.79 | -3.95 | 1.087 | — |
| 32 | ablation | ablate_no_wisdom_thinking | +1.93 | 0.73 | -4.04 | 1.012 | — |
| 33 | ablation | ablate_no_risk_tightened | +1.80 | 0.68 | -4.04 | 0.951 | — |
| 34 | ablation | ablate_no_regime_dynamic_sizing | +1.78 | 0.67 | -4.04 | 0.941 | — |
| 35 | monte_carlo | all_on_no_thinking | -0.87 | 0.04 | -22.81 | -0.234 | -44.69 |

## Feature importance

| Feature | ON mean | OFF mean | ON-OFF | Ablation delta |
|---------|--------:|---------:|-------:|---------------:|
| hybrid_rebalance | 4.157 | 5.679 | -1.522 | +0.278 |
| risk_tightened | 4.155 | 5.690 | -1.535 | -0.135 |
| regime_dynamic_sizing | 4.151 | 5.714 | -1.563 | -0.146 |
| stat_arb | 4.149 | 5.719 | -1.570 | +0.000 |
| positioning_overlay | 4.140 | 5.770 | -1.630 | +0.177 |
| wisdom_thinking | 3.981 | 5.960 | -1.979 | -0.074 |
| dynamic_core | 3.980 | 5.962 | -1.982 | +0.051 |
| deep_history_indicators | 3.906 | 7.018 | -3.112 | +1.551 |

## Notes

- Composite = Sharpe×0.5 + Return×0.3 + (1/(1+|p5_return|))×0.2 (full-window runs use return as p5 proxy).
- Ablation delta: score change vs baseline when that single feature is flipped OFF.
