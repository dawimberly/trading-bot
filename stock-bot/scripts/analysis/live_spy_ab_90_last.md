# STRICT Live Conservative SPY on/off A/B (90d)

Generated: 2026-07-29 03:46 UTC
Window: 2026-05-14 -> 2026-07-28 (90d)
Benchmark VTI B&H: -0.32%
Research sizing: $10,000 start / $500 max order (live 85/5 ratios)

**STRICT PIT: ON | insider/RVOL/catalyst/news/LLM/dyn_univ/buffett-fallback off**

**STRICT live-shaped research only; do not change live Profile A until 365d confirms**

| Leg | Config | Return | Sharpe | MaxDD | Trades | SPY fills | NYSE fills | vs spy_on ret |
|-----|--------|--------|--------|-------|--------|-----------|------------|---------------|
| spy_on | Live Conservative: 85% VTI + 5% SPY trend | +0.72% | 0.26 | -5.30% | 8 | 0 | 1 | +0.00pp |
| spy_off | Live Conservative: 85% VTI + 5% cash (SPY cap=0) | +0.72% | 0.25 | -5.38% | 8 | 0 | 1 | +0.00pp |

## Verdict

spy_off +0.72% Sharpe 0.25 MaxDD -5.38% SPY fills 0 vs spy_on +0.72% Sharpe 0.26 (+0.00pp ret). spy_off did not clear return+Sharpe+MaxDD rule — keep live SPY trend ON. STRICT live-shaped research only; do not change live Profile A until 365d confirms
