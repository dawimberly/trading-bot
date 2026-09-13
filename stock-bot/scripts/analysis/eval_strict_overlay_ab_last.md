# STRICT per-overlay A/B

Generated: 2026-07-26 23:50 UTC
Window: 2026-05-11 -> 2026-07-26 (90 sim days requested)
Benchmark VTI B&H: +0.61%

**Per-overlay deltas vs STRICT are diagnostic only; do not promote live from overlays alone**

| Mode | Return | Sharpe | MaxDD | Trades | NYSE | vs STRICT ret | Notes |
|------|--------|--------|-------|--------|------|---------------|-------|
| STRICT (OK) | +15.62% | 2.44 | -3.09% | 777 | 82 | -- | pure STRICT (hygiene ON) |
| +insider (OK) | +15.87% | 2.48 | -2.97% | 778 | 83 | +0.25% | STRICT + insider only |
| +rvol (OK) | +15.62% | 2.44 | -3.09% | 777 | 82 | +0.00% | STRICT + rvol only |
| +catalyst (OK) | +15.45% | 2.45 | -3.72% | 831 | 107 | -0.17% | STRICT + catalyst only |
| +hist_news (OK) | +15.61% | 2.44 | -3.09% | 777 | 82 | -0.01% | STRICT + hist_news only |

## Verdict

+insider: ret +0.25pp Sharpe +0.04, NYSE +1 (negligible) | +rvol: ret +0.00pp Sharpe +0.00, NYSE +0 (negligible) | +catalyst: ret -0.17pp Sharpe +0.01, NYSE +25 (negligible) | +hist_news: ret -0.01pp Sharpe +0.00, NYSE +0 (negligible) No single overlay moves return much vs STRICT on this window. Per-overlay deltas vs STRICT are diagnostic only; do not promote live from overlays alone
