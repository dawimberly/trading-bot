# STRICT Live Conservative SPY on/off A/B (365d)

Generated: 2026-07-29 17:28 UTC
Window: 2025-09-23 -> 2026-07-29 (365d)
Benchmark VTI B&H: +12.60%
Research sizing: $10,000 start / $500 max order (live 85/5 ratios)

**STRICT PIT: ON | insider/RVOL/catalyst/news/LLM/dyn_univ/buffett-fallback off**

**STRICT live-shaped research only; do not change live Profile A until 365d confirms**

| Leg | Config | Return | Sharpe | MaxDD | Trades | SPY fills | NYSE fills | vs spy_on ret |
|-----|--------|--------|--------|-------|--------|-----------|------------|---------------|
| spy_on | Live Conservative: 85% VTI + 5% SPY trend | +15.45% | 1.24 | -7.97% | 14 | 2 | 1 | +0.00pp |
| spy_off | Live Conservative: 85% VTI + 5% cash (SPY cap=0) | +15.45% | 1.25 | -7.97% | 15 | 0 | 1 | +0.00pp |

## Verdict

spy_off +15.45% Sharpe 1.25 MaxDD -7.97% SPY fills 0 vs spy_on +15.45% Sharpe 1.24 (+0.00pp ret). spy_off did not clear return+Sharpe+MaxDD rule — keep live SPY trend ON. STRICT live-shaped research only; do not change live Profile A until 365d confirms
