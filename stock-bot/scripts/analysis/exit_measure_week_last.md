## Exit measure week

**Book:** `alpaca_paper_v2` · **Window:** 2026-09-08 → 2026-09-12  
**Started:** 2026-09-06 19:30 UTC · measure only (no `.env`).

### Baseline (week start)

- Equity: 100421.52
- 1R hits: 0/21 (0.0%)
- cancel_ex_core: 0.7323
- Stop-bounce flags in follow-through report: 1
- NYSE realized / unrealized: -705.8100000000002 / 840.3147

### Research reference (365d daily A/B — not promote)

| policy | book% | maxDD% |
|---|---:|---:|
| hold | 66.14 | 6.63 |
| take_1r | 81.28 | 10.95 |
| wide_stop_3x | 72.39 | 8.34 |
| long_hold_45 | 66.9 | 5.86 |

Candidates under watch: **wide_stop_3x** (if stop-bounce ≥2), **same_day_churn** (if cancel_ex_core ≥0.70), **take_1r** stays DENY.

### Evaluation

_Pending Saturday 2026-09-12. Run `python scripts/analysis/exit_measure_week.py --evaluate` (also hooked from freeze weekly / weekly review)._

