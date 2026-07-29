# STRICT 365d confirm — exit + SPY-off

Generated: 2026-07-29 01:08 UTC
Window: 2025-09-22 -> 2026-07-28 (365d)
Benchmark VTI B&H: +12.00%

**STRICT PIT: ON | insider/RVOL/catalyst/news/LLM/dyn_univ/buffett-fallback off**

**STRICT research only; no live Profile A changes**

| Leg | Config | Return | Sharpe | MaxDD | Trades | SPY fills | NYSE fills | vs baseline ret |
|-----|--------|--------|--------|-------|--------|-----------|------------|-----------------|
| baseline | defaults (hold=30 arm=10% trail=5% SPY cap ON) | +20.64% | 1.12 | -7.58% | 2501 | 43 | 174 | +0.00pp |
| exit_h45_tight | hold=45 arm=8% trail=4% | +19.11% | 1.05 | -7.60% | 2503 | 43 | 166 | -1.53pp |
| spy_off | SPY cap=0% (Dyn VTI ON) | +27.67% | 1.47 | -7.05% | 2873 | 0 | 408 | +7.03pp |

## Verdict

Best exit leg exit_h45_tight (hold=45 arm=8% trail=4%): +19.11% Sharpe 1.05 vs baseline -1.53pp. SPY-off: +27.67% Sharpe 1.47 SPY fills 0 vs baseline +7.03pp. PAPER DEFAULT CANDIDATE (365d): spy_off — beat baseline return+Sharpe with MaxDD within 1.0pp. Discuss paper default change only; no live Profile A change yet. No combo until singles clear. STRICT research only; no live Profile A changes
