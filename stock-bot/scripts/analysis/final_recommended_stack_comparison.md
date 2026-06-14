# Final Recommended Stack Comparison

Generated: 2026-06-07 18:12

## Versions

| # | Stack | Wisdom | VTI | Game plan | Halt |
|---|-------|--------|-----|-----------|------|
| 1 | Old baseline | governor | 0% | full (0.9 long scale live) | one-way |
| 2 | Intermediate | dynamic | 80% | yield-gate-only | one-way |
| 3 | Current recommended | dynamic | 80% | yield-gate-only | resume 8% + liquidate |

Social sleeve off for all runs. Metal/stress-cash sleeves not simulated in `backtester.py`.

Data: 945 bars | 2023-11-06 → 2026-06-07

## 365d (2025-06-08 → 2026-06-07)

VTI buy & hold benchmark: **+24.59%**

| Version | Return | Ann. | Sharpe | Sortino | Max DD | Calmar | Avg exp | Halts | Resumes |
|---------|-------:|-----:|-------:|--------:|-------:|-------:|--------:|------:|--------:|
| Old baseline (pre-optimization) | +8.61% | 8.63% | 0.47 | 0.59 | -12.69% | 0.68 | 65.8% | 1 | 0 |
| Intermediate (dynamic + yield-gate + 80/20 VTI) | +20.50% | 20.56% | 1.22 | 1.48 | -10.14% | 2.02 | 94.9% | 1 | 0 |
| Current recommended (README stack) | +19.95% | 20.01% | 1.25 | 1.48 | -10.14% | 1.97 | 94.6% | 1 | 1 |

**Old baseline (pre-optimization)** — + Simple discrete wisdom; full game-plan config (0.9 long scale in live). − Governor mode; no VTI ballast; one-way halt; metal/stress not in daily sim.

**Intermediate (dynamic + yield-gate + 80/20 VTI)** — + Dynamic sizing; yield-gate-only; passive VTI core — major Sharpe uplift vs old. − Legacy halt (no resume); small active sleeve limits overlap/sizing tweaks.

**Current recommended (README stack)** — + Same as intermediate + halt resume 8% and breach liquidation; live default. − Optional flags (overlap, adaptive, SPY exit) tested — not worth default on.

## 1000d (2024-05-24 → 2026-06-07)

VTI buy & hold benchmark: **+42.29%**

| Version | Return | Ann. | Sharpe | Sortino | Max DD | Calmar | Avg exp | Halts | Resumes |
|---------|-------:|-----:|-------:|--------:|-------:|-------:|--------:|------:|--------:|
| Old baseline (pre-optimization) | +33.10% | 15.06% | 0.82 | 0.92 | -15.35% | 2.16 | 60.9% | 1 | 0 |
| Intermediate (dynamic + yield-gate + 80/20 VTI) | +41.43% | 18.54% | 0.93 | 0.99 | -18.55% | 2.23 | 93.5% | 1 | 0 |
| Current recommended (README stack) | +45.95% | 20.38% | 1.04 | 1.15 | -17.03% | 2.70 | 94.1% | 3 | 3 |

**Old baseline (pre-optimization)** — + Simple discrete wisdom; full game-plan config (0.9 long scale in live). − Governor mode; no VTI ballast; one-way halt; metal/stress not in daily sim.

**Intermediate (dynamic + yield-gate + 80/20 VTI)** — + Dynamic sizing; yield-gate-only; passive VTI core — major Sharpe uplift vs old. − Legacy halt (no resume); small active sleeve limits overlap/sizing tweaks.

**Current recommended (README stack)** — + Same as intermediate + halt resume 8% and breach liquidation; live default. − Optional flags (overlap, adaptive, SPY exit) tested — not worth default on.

## max (2024-05-24 → 2026-06-07)

VTI buy & hold benchmark: **+42.29%**

| Version | Return | Ann. | Sharpe | Sortino | Max DD | Calmar | Avg exp | Halts | Resumes |
|---------|-------:|-----:|-------:|--------:|-------:|-------:|--------:|------:|--------:|
| Old baseline (pre-optimization) | +33.10% | 15.06% | 0.82 | 0.92 | -15.35% | 2.16 | 60.9% | 1 | 0 |
| Intermediate (dynamic + yield-gate + 80/20 VTI) | +41.43% | 18.54% | 0.93 | 0.99 | -18.55% | 2.23 | 93.5% | 1 | 0 |
| Current recommended (README stack) | +45.95% | 20.38% | 1.04 | 1.15 | -17.03% | 2.70 | 94.1% | 3 | 3 |

**Old baseline (pre-optimization)** — + Simple discrete wisdom; full game-plan config (0.9 long scale in live). − Governor mode; no VTI ballast; one-way halt; metal/stress not in daily sim.

**Intermediate (dynamic + yield-gate + 80/20 VTI)** — + Dynamic sizing; yield-gate-only; passive VTI core — major Sharpe uplift vs old. − Legacy halt (no resume); small active sleeve limits overlap/sizing tweaks.

**Current recommended (README stack)** — + Same as intermediate + halt resume 8% and breach liquidation; live default. − Optional flags (overlap, adaptive, SPY exit) tested — not worth default on.

## Final verdict

### Improvement (old → current recommended, 365d)

- Sharpe: **0.47 → 1.25** (+0.78)
- Return: **+8.61% → +19.95%** (+11.34 pp)
- Max DD: **-12.69% → -10.14%** (+2.55 pp)
- Annualized: **8.63% → 20.01%**

### Intermediate → current (365d)

Halt layer only: Sharpe 1.22 → 1.25, return +20.50% → +19.95%, halts 1/0 → 1/1.

### Ready as live default?

**Yes.** Current recommended stack is validated across recent A/B tests:
- VTI 80/20 beats active-only on Sharpe (`--compare-vti-core`)
- NYSE overlap, adaptive/cofire, SPY MA exit **not** worth enabling as defaults on this stack
- Paper aggressive profile stays separate for research

### Last small tweaks before lock-in

1. **Keep optional flags off** — overlap / adaptive / SPY exit opt-in only.
2. **NYSE overlap** — enable on paper book if you run active-heavy; not on live 80/20.
3. **Monitor** social/Felix sleeve on paper only until 60d aligned live-vs-sim.
4. **Document** that daily backtest omits metal sleeve deploy (yield-gate-only is what runs live).

Re-run: `python scripts/analysis/final_recommended_stack_comparison.py`