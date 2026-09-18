# Paper Weekly Research — 2026-08-29

_Overlay. Blind pipeline (grade C, 0 closes, equity source none) is not the week. SoT: `docs/DAILY_ANALYZE_LAST.md`. Paper book only. Never auto-applies `.env`._

## Executive Decision
**HOLD** — No treatment. Mandate and metal hypothesis are not usable. No `.env`, no metal sleeve, no weekend flag flip.

Treatment under review: `(no treatment)`

## Mandate
**Not used this week.** Pipeline 7d Sharpe/DD n/a and “0 NYSE closes” are a journal-parse miss (`pd.read_csv` on ragged portal CSV), not a quiet book.

Use ANALYZE snapshots instead of the mandate table.

## Data Quality
Pipeline said **Grade C** / source `none`. That is **false** relative to portal ANALYZE (same `paper_journal.csv`).

## Performance (from ANALYZE, not 7d Sharpe)

| Day | Paper fills | Realized | ATR stops (sample) | Cash % | NYSE MV | vti_core |
|-----|-------------|----------|---------------------|--------|---------|----------|
| 2026-08-26 | 30 (10 buy / 20 sell) | −263.45 | 5 names | 75.1% | ~$21.8k | 0 |
| 2026-08-27 | 109 (23 / 86) | −257.21 | many (IQV…SCCO) | 67.6% | ~$29.0k | 0 |
| 2026-08-28 | 21 (8 / 13) | −189.08 | AG, EGO, CDE, DINO, HALO, RBRK | 66.4% | ~$30.0k | 0 |

Last snapshot (`DAILY_ANALYZE_LAST` = 2026-08-28 15:15 CDT): equity **96445.7**, cash **64028** (~**66%**), NYSE **~$30k**, **vti_core 0**, **no VTI fills** that day. Realized red on stop days.

8/26 also logged empty-reason VTI buys (then core off again). Not a metal-sleeve story.

## Markov HMM regime

- Markov HMM: OFF
- Informational only. **Not** a reason to turn HMM on this weekend.

## Hypothesis
**None.** Do not tighten `METAL_SLEEVE_CAP_PCT` 10% → 7.5% (90d A/B already worse). Do not add “watch metal” as a to-do.

## Recommendation
**HOLD** — freeze continues. Paper is NYSE-active with ATR stops and cash-heavy / VTI core off.

## Implementation (paper only)
**No `.env` / metal / weekend flag flip.**
- **Monday:** run ANALYZE; if you restarted paper, confirm **`PAPER_MAX_EQUITY_TRADES=12`** is loaded for the cycle.
- Do **not** copy to live.

<!-- advisory only; never auto-apply -->
