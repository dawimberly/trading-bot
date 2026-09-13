# Exit policy A/B (NYSE MA70 daily, research only)

No orders, no `.env`, no restart.

**Window:** 2025-08-12 → 2026-09-06 (390 bars, 343 names)
**Baseline hold:** 30 bars · stop default ~2×ATR / 1% floor

| policy | trades | 1R hit% | win% | avg% | book% | maxDD% | exits |
|---|---:|---:|---:|---:|---:|---:|---|
| hold | 357 | 52.1% | 38.7 | 3.57 | 66.14 | 6.63 | 1r=0 stop=208 time=134 |
| take_1r | 1203 | 58.1% | 58.4 | 1.17 | 81.28 | 10.95 | 1r=699 stop=488 time=1 |
| wide_stop_3x | 292 | 57.5% | 47.6 | 4.65 | 72.39 | 8.34 | 1r=0 stop=136 time=141 |
| long_hold_45 | 267 | 54.7% | 33.7 | 4.98 | 66.90 | 5.86 | 1r=0 stop=169 time=83 |

Median/avg 1R target (hold): 4.10% of entry

## Verdict rule (human)

- Promote candidate only if book% improves vs `hold` **and** maxDD does not worsen by >2pp.
- One knob at a time if wiring to paper later.
