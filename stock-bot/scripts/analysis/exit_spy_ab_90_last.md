# STRICT exit ladder + SPY-off A/B (90d)

Generated: 2026-07-28 21:51 UTC
Window: 2026-05-14 -> 2026-07-28 (90d)
Benchmark VTI B&H: -0.32%

**STRICT PIT: ON | insider/RVOL/catalyst/news/LLM/dyn_univ/buffett-fallback off**

**STRICT research only; no live Profile A changes**

| Leg | Config | Return | Sharpe | MaxDD | Trades | SPY fills | NYSE fills | vs baseline ret |
|-----|--------|--------|--------|-------|--------|-----------|------------|-----------------|
| baseline | defaults (hold=30 arm=10% trail=5% SPY cap ON) | +17.08% | 2.55 | -4.08% | 751 | 16 | 61 | +0.00pp |
| exit_h20_base | hold=20 arm=10% trail=5% | +17.19% | 2.57 | -4.08% | 753 | 16 | 60 | +0.11pp |
| exit_h20_tight | hold=20 arm=8% trail=4% | +18.15% | 2.85 | -4.40% | 755 | 17 | 64 | +1.07pp |
| exit_h30_tight | hold=30 arm=8% trail=4% | +17.74% | 2.79 | -4.20% | 756 | 16 | 65 | +0.66pp |
| exit_h45_base | hold=45 arm=10% trail=5% | +17.95% | 2.83 | -2.79% | 740 | 15 | 59 | +0.87pp |
| exit_h45_tight | hold=45 arm=8% trail=4% | +18.46% | 2.95 | -3.09% | 749 | 16 | 63 | +1.38pp |
| spy_off | SPY cap=0% (Dyn VTI ON) | +19.61% | 3.06 | -3.94% | 823 | 0 | 112 | +2.53pp |

## Verdict

Best exit leg exit_h45_tight (hold=45 arm=8% trail=4%): +18.46% Sharpe 2.95 vs baseline +1.38pp. SPY-off: +19.61% Sharpe 3.06 SPY fills 0 vs baseline +2.53pp. Queue 365d STRICT confirm: exit_h20_tight, exit_h30_tight, exit_h45_base, exit_h45_tight, spy_off. STRICT research only; no live Profile A changes
