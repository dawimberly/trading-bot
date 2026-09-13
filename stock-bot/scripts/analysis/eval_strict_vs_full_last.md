# STRICT PIT vs FULL overlays

Generated: 2026-08-19 05:36 UTC
Window: 2025-10-13 -> 2026-08-18 (365 sim days requested)
Benchmark VTI B&H: +18.45%

**FULL is not point-in-time; do not promote live from FULL alone**

| Mode | Return | Sharpe | MaxDD | Trades | NYSE fills | Notes |
|------|--------|--------|-------|--------|------------|-------|
| STRICT PIT (OK) | +30.13% | 1.56 | -8.40% | 2680 | 333 | point-in-time kill switches; hygiene ON |
| FULL overlays (OK) | +28.71% | 1.53 | -8.13% | 3011 | 485 | current overlays (lookahead risk on insider/RVOL/catalyst/news) |

## Verdict

FULL - STRICT: return -1.42pp | Sharpe -0.03 | MaxDD +0.27pp | NYSE fills +152. STRICT outperforms FULL -> overlays may be noise/drag; price-path edge is in STRICT. FULL is not point-in-time; do not promote live from FULL alone
