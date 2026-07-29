# STRICT conviction book (0% VTI + top-N)

Generated: 2026-07-27 20:51 UTC
Window: 2026-05-13 -> 2026-07-27 (90d)
Benchmark VTI B&H: +0.31%

**STRICT research only; conviction book is not live Profile A**

| Leg | Return | Sharpe | MaxDD | Avg VTI | Active% | Unique NYSE | Notes |
|-----|--------|--------|-------|---------|---------|-------------|-------|
| baseline_strict ** | +15.16% | 2.36 | -4.44% | 0.65 | 35.0 | 307 | STRICT defaults (dynamic VTI ballast) |
| zero_vti | +11.91% | 1.92 | -4.76% | 0.40 | 59.7 | 307 | fixed 0% VTI; still multi-name sleeve |
| conviction_top5 | +11.68% | 1.86 | -5.02% | 0.40 | 59.7 | 46 | 0% VTI + top 5 ranks, max 5 names, fat size |
| conviction_top3 | +12.21% | 2.00 | -4.40% | 0.40 | 59.7 | 6 | 0% VTI + top 3 ranks, max 3 names, fatter size |

## Verdict

Best: baseline_strict return +15.16% Sharpe 2.36 MaxDD -4.44% avg VTI 0.65 unique NYSE 307. vs baseline_strict: +0.00pp. Conviction did not clearly beat baseline — top-N ranks may lack predictive edge; do not promote yet. STRICT research only; conviction book is not live Profile A
