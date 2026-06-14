# NYSE Overlap Filter — Active-Only A/B

Generated: 2026-06-07 14:21

## Setup

- **VTI core:** 0% (active-only, full caps: SPY 45% / NYSE 20% / crypto 20%)
- **Stack:** yield-gate-only game plan + dynamic wisdom (same as live)
- **Variants:** baseline (overlap off) vs overlap on (`NYSE_SPY_CORR_MAX=0.80`)
- **Social sleeve:** off for this test

Data: 945 daily bars | full range 2023-11-06 → 2026-06-07

## 365d (2025-06-08 → 2026-06-07)

| Variant | Return | Sharpe | Sortino | Max DD | Calmar | Avg SPY exp | Avg NYSE exp | Co-fire % |
|---------|-------:|-------:|--------:|-------:|-------:|------------:|-------------:|----------:|
| Active-only (overlap off) | +25.25% | 1.10 | 1.39 | -12.43% | 2.03 | 42.1% | 22.9% | 0.0% |
| Active-only + NYSE overlap (corr ≤ 0.80) | +14.17% | 0.73 | 0.91 | -12.92% | 1.10 | 42.4% | 22.4% | 0.0% |

### Active-only (overlap off) — overlap diagnostics

- Overlap filter changed top pick: **0** days
- Days with ≥1 symbol filtered (when SPY active): **0**
- SPY sleeve active: **61.9%** of sim days
- Top NYSE pick (unfiltered rank): AMD (111), KTOS (73), PPLT (37), LNG (36), GOOGL (29), URA (18), GDX (16), LMT (14)
- Top NYSE pick (after filter): AMD (111), KTOS (73), PPLT (37), LNG (36), GOOGL (29), URA (18), GDX (16), LMT (14)

### Active-only + NYSE overlap (corr ≤ 0.80) — overlap diagnostics

- Overlap filter changed top pick: **111** days
- Days with ≥1 symbol filtered (when SPY active): **221**
- SPY sleeve active: **61.9%** of sim days
- Top NYSE pick (unfiltered rank): AMD (111), KTOS (73), PPLT (37), LNG (36), GOOGL (29), URA (18), GDX (16), LMT (14)
- Top NYSE pick (after filter): KTOS (60), GDX (41), GOOGL (41), PPLT (38), LNG (36), UNH (32), AMD (30), URA (24)

## 1000d (2024-05-24 → 2026-06-07)

| Variant | Return | Sharpe | Sortino | Max DD | Calmar | Avg SPY exp | Avg NYSE exp | Co-fire % |
|---------|-------:|-------:|--------:|-------:|-------:|------------:|-------------:|----------:|
| Active-only (overlap off) | +36.74% | 0.82 | 0.92 | -16.89% | 2.18 | 46.1% | 25.1% | 0.0% |
| Active-only + NYSE overlap (corr ≤ 0.80) | +48.36% | 0.83 | 1.00 | -25.32% | 1.91 | 44.2% | 28.1% | 0.0% |

### Active-only (overlap off) — overlap diagnostics

- Overlap filter changed top pick: **0** days
- Days with ≥1 symbol filtered (when SPY active): **0**
- SPY sleeve active: **75.8%** of sim days
- Top NYSE pick (unfiltered rank): TSLA (130), AMD (113), KTOS (113), NVDA (55), URA (51), GDX (48), LMT (44), GOOGL (37)
- Top NYSE pick (after filter): TSLA (130), AMD (113), KTOS (113), NVDA (55), URA (51), GDX (48), LMT (44), GOOGL (37)

### Active-only + NYSE overlap (corr ≤ 0.80) — overlap diagnostics

- Overlap filter changed top pick: **346** days
- Days with ≥1 symbol filtered (when SPY active): **549**
- SPY sleeve active: **75.8%** of sim days
- Top NYSE pick (unfiltered rank): TSLA (130), AMD (113), KTOS (113), NVDA (55), URA (51), GDX (48), LMT (44), GOOGL (37)
- Top NYSE pick (after filter): GDX (96), GOOGL (84), KTOS (78), LNG (76), LMT (61), UNH (56), URA (49), AAPL (46)

## max (2024-05-24 → 2026-06-07)

| Variant | Return | Sharpe | Sortino | Max DD | Calmar | Avg SPY exp | Avg NYSE exp | Co-fire % |
|---------|-------:|-------:|--------:|-------:|-------:|------------:|-------------:|----------:|
| Active-only (overlap off) | +36.74% | 0.82 | 0.92 | -16.89% | 2.18 | 46.1% | 25.1% | 0.0% |
| Active-only + NYSE overlap (corr ≤ 0.80) | +48.36% | 0.83 | 1.00 | -25.32% | 1.91 | 44.2% | 28.1% | 0.0% |

### Active-only (overlap off) — overlap diagnostics

- Overlap filter changed top pick: **0** days
- Days with ≥1 symbol filtered (when SPY active): **0**
- SPY sleeve active: **75.8%** of sim days
- Top NYSE pick (unfiltered rank): TSLA (130), AMD (113), KTOS (113), NVDA (55), URA (51), GDX (48), LMT (44), GOOGL (37)
- Top NYSE pick (after filter): TSLA (130), AMD (113), KTOS (113), NVDA (55), URA (51), GDX (48), LMT (44), GOOGL (37)

### Active-only + NYSE overlap (corr ≤ 0.80) — overlap diagnostics

- Overlap filter changed top pick: **346** days
- Days with ≥1 symbol filtered (when SPY active): **549**
- SPY sleeve active: **75.8%** of sim days
- Top NYSE pick (unfiltered rank): TSLA (130), AMD (113), KTOS (113), NVDA (55), URA (51), GDX (48), LMT (44), GOOGL (37)
- Top NYSE pick (after filter): GDX (96), GOOGL (84), KTOS (78), LNG (76), LMT (61), UNH (56), URA (49), AAPL (46)

## Conclusion

### Does overlap reduce concentration?

**Yes, meaningfully** when SPY is active (~62–76% of sim days on full active caps):

| Window | Top-pick changes | Filter events |
|--------|------------------|---------------|
| 365d | 111 days | 221 days with ≥1 symbol dropped |
| 1000d / max | 346 days | 549 days |

Without filter, NYSE momentum favors **AMD, TSLA, NVDA** (high SPY correlation). With filter, leadership shifts to **GDX, GOOGL, KTOS, UNH, AAPL** — lower beta / less SPY overlap.

Average sleeve exposure is similar (SPY ~42–46%, NYSE ~22–28%); the filter changes **which** names fill the NYSE sleeve, not total deployment.

### Does overlap improve risk-adjusted returns?

**Mixed — not a clear win.**

| Window | Baseline Sharpe | Overlap Sharpe | Return Δ | Max DD Δ |
|--------|----------------:|---------------:|---------:|---------:|
| 365d | **1.10** | 0.73 | **−11.1 pp** | −0.5 pp |
| 1000d / max | 0.82 | **0.83** | **+11.6 pp** | **−8.4 pp** (worse) |

Recent year (365d): overlap **hurts** Sharpe and return. Longer window: tiny Sharpe gain (+0.01) with **much deeper drawdown** (−25% vs −17%). Sortino follows the same pattern.

### Should `NYSE_OVERLAP_FILTER_ENABLED=true` be the new default?

**No — keep it opt-in.**

1. **Live stack is 80/20 VTI** — overlap barely fires there (see `sharpe_flag_grid_results.md`).
2. **Active-only 365d** — overlap is clearly worse on the metrics that matter for near-term risk control.
3. **Long window** — higher headline return comes with **worse tail risk** (Max DD −25%), not a trade worth baking in as default.

**Recommended use:** Enable overlap **opt-in** on the paper research book or if you run active-only and observe repeated NYSE picks in high-corr tech while SPY is deployed. It is a **diversification guard**, not a Sharpe optimizer in this backtest.

Re-run: `python scripts/analysis/nyse_overlap_active_only_test.py`