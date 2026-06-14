# Sharpe Phase Comparison

Generated: 2026-06-04 22:47 (verified 1000d window 2026-06-05)

## Variants

| Key | Old | Current dynamic | New optimized |
|-----|-----|-----------------|---------------|
| Wisdom | governor | dynamic | dynamic |
| Game plan | full (0.9 scale) | yield-gate-only | yield-gate-only |
| NYSE overlap | off | off | on (corr 0.80) |
| Adaptive + co-fire | off | off | on |
| SPY MA exit | off | off | on |
| NYSE beta scaling | off | off | on |
| Halt resume / liquidate | off | 8% / on | 8% / on |
| Gap tiers | — | 0.25 / 0.35 / 0.40 | 0.25 / 0.35 / 0.40 |

**Assumption:** integrated `backtester.py` does not deploy metal basket or stress-cash trims; full game plan effect is mainly the 0.9 long-cap scale.

## Window 500d

| Variant | Return | Sharpe | Sortino | Max DD | Calmar | Avg Exp% | Co-fire% | Crypto% |
|---------|-------:|-------:|--------:|-------:|-------:|---------:|---------:|--------:|
| old | +25.89% | 0.70 | 0.88 | -24.59% | 1.05 | 50.8 | 0.0 | +0.00 |
| current_dynamic | +38.38% | 0.91 | 1.23 | -25.14% | 1.53 | 75.5 | 0.0 | +2.35 |
| new_optimized | +22.91% | 0.74 | 1.03 | -18.02% | 1.27 | 77.4 | 3.4 | +12.51 |

## Window 1000d

| Variant | Return | Sharpe | Sortino | Max DD | Calmar | Avg Exp% | Co-fire% | Crypto% |
|---------|-------:|-------:|--------:|-------:|-------:|---------:|---------:|--------:|
| old | +52.42% | 0.98 | 1.17 | -16.32% | 3.21 | 60.7 | 0.0 | +0.00 |
| current_dynamic | +56.16% | 0.97 | 1.17 | -16.97% | 3.31 | 70.1 | 0.0 | +0.00 |
| new_optimized | +57.82% | **1.00** | 1.21 | -16.73% | 3.46 | 71.1 | 0.3 | +0.00 |

## Window 2000d

| Variant | Return | Sharpe | Sortino | Max DD | Calmar | Avg Exp% | Co-fire% | Crypto% |
|---------|-------:|-------:|--------:|-------:|-------:|---------:|---------:|--------:|
| old | +83.17% | 0.56 | 0.72 | -34.53% | 2.41 | 57.2 | 0.0 | +18.52 |
| current_dynamic | +105.47% | 0.63 | 0.80 | -37.01% | 2.85 | 84.8 | 0.0 | +2.54 |
| new_optimized | +83.93% | 0.56 | 0.72 | -37.65% | 2.23 | 85.9 | 3.6 | +2.55 |

## Target Sharpe 1.2–1.5

**No variant hit 1.2–1.5** on any window. Closest: **new_optimized @ 1000d** Sharpe **1.00** (+57.82%, max DD -16.73%).

Prior grids without the wisdom layer reached higher Sharpe on long windows (`refinements_grid_ab.py` baseline **1.35** on 2000d; `risk_layer_ab.py` halt-resume+liquidate **1.77** on 500d). Dynamic wisdom adds pause/sizing that trades raw return for drawdown control on recent windows but caps long-window Sharpe vs price-only stack.

## Top 3 Sharpe improvements (prioritized, with evidence)

1. **Halt resume 8% + liquidate on breach** — largest single-layer gain in prior A/B (`risk_layer_ab.py`): 500d Sharpe **1.24 → 1.77** (+0.53), max DD ~6 pp shallower. Enabled in current_dynamic and new_optimized; this phase shows **500d current_dynamic +0.21** vs old governor stack (0.70 → 0.91).

2. **Dynamic wisdom + yield-gate-only** (vs full game plan governor) — **500d +0.21 Sharpe**, **2000d +0.07 Sharpe** with +22.3 pp return (`old → current_dynamic` deltas). Yield-gate-only keeps full 45/20/20/15% caps without metal drag (`game_plan_ab_test.py`: recent 750d within -0.05 pp of baseline).

3. **NYSE overlap filter + adaptive chunk + co-fire + SPY MA exit + beta** — mixed in this phase with wisdom on: **1000d +0.03** Sharpe (0.97 → 1.00) and shallower 500d max DD (-25.1% → -18.0%), but **500d -0.17** and **2000d -0.07** Sharpe vs current_dynamic. Prior price-only grids (`refinements_grid_ab.py`, `deployment_efficiency_ab.py`) showed +0.04 Sharpe (2000d) and +145 pp return from adaptive sizing when wisdom pauses were absent.

## Dynamic mode status

- `WISDOM_MODE=dynamic` default in `config.py` and `.env.example`
- Gap tiers: aggressive **<0.25**, normal **0.25–0.35** (extends to 0.40 with tapering web weight), defensive **>0.40**
- `AUTO_DYNAMIC_ENABLED=true`; `SENTIMENT_GAP_THRESHOLD_*` = 0.25 / 0.35 / 0.40
- `backtester.run_backtest()` now accepts `wisdom_mode` + `monthly_web` for Wayback-backed dynamic tests

## Reproduce

```bash
python scripts/analysis/sharpe_phase_compare.py --days 500 1000 2000
```

Outputs: `scripts/analysis/sharpe_phase_results.md`, `scripts/analysis/sharpe_phase_compare.csv`

```bash
python -m py_compile backtester.py scripts/analysis/sharpe_phase_compare.py
```
