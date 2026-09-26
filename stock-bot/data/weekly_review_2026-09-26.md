# Paper Weekly Research — 2026-09-26

_Medium SoT (`alpaca_paper_v2`). Never auto-applies `.env`. Live Profile A is read-only here._

## Executive Decision
**HOLD** — HOLD — Only ranked sleeve is nyse at +2810.30. A single positive sleeve is HOLD. Keep `PAPER_NYSE_SLEEVE_CAP_PCT=0.90`.

Treatment under review: none — keep `PAPER_NYSE_SLEEVE_CAP_PCT=0.90`

## Mandate
**Score: FAILING** · lookback 7d · regime `RHYME_A: Euphoric_Volatility`

| Target | Threshold | Actual |
|--------|-----------|--------|
| Ann. Sharpe | ≥ 1.0 | -3.89 |
| Max DD | ≤ 15.0% | 3.08% |
| 7d return | ≥ 0.5% | -2.33% |
| Closed trades | ≥ 5 | 9 |

Pass: max DD 3.08% ≤ 15.0%; closed trades 9 ≥ 5
Miss: ann. Sharpe -3.89 < 1.0; 7d return -2.33% < 0.5%

## Data Quality
**Grade A** · source `alpaca_paper_v2/paper_journal.csv+wisdom_filter(ok)`
| Grade | Criteria | Mandate usable? |
|-------|----------|-----------------|
| **A** | ≥4 clean days, 0 equity jumps, ≥5 closed trades | Yes |
| **B** | ≥4 days, ≤2 jumps after filter, trades may be sparse | Yes (cautious) |
| **C** | Missing equity, <2 days, or noisy/unfiltered | **No** |

Observations: 8 clean / 0 raw · jumps removed: 0 · method: EOD equity, √252 Sharpe, single-factor rule
- Clean curve, no discontinuities, adequate closed-trade sample.

## Performance (7d, cleaned)
| | |
|--|--|
| Equity | 99741 → 97420 |
| Return | -2.33% |
| Sharpe / Sortino | -3.89 / -14.58 |
| Max DD | 3.08% |
| Closed trades | 9 (WR 0%, exp n/a) |
| Daily hit rate | 43% |

**Key Observations**
- Nyse realized +0 (9 closes, WR 0%).
- Wisdom/regime tone defensive (`RHYME_A: Euphoric_Volatility`).
- 7d return -2.33% below 0.5% target.

## Markov HMM regime

- Markov HMM: OFF


## Sleeve Attribution
Only ranked sleeve **nyse** (+2810)

| Sleeve | Cls | Win% | Contrib | Src |
|--------|-----|------|---------|-----|
| nyse | 9 | 0% | +2810 | mixed |
| crypto | 0 | n/a | +0 | unrealized |
| metal | 0 | n/a | +0 | unrealized |

## NYSE sale hour
`PAPER_MOMENTUM_QUALITY_FIXES`=off · 7d NYSE exits: 9 (0 with `entry_hour`)

| Window (ET) | Trades | Win% | Avg PnL |
|-------------|--------|------|---------|
| 9:30–10:00 | 5 | 0% | n/a |
| 12:00–14:00 midday | 1 | 0% | n/a |
365d intraday sim (memo): Δreturn n/a pp · ΔSharpe 0.03
- Buckets use the sell timestamp converted to ET, not the entry hour.

## Live vs Paper Delta
_Read-only. Does not modify live._

| Book | 7d return | Sharpe | Equity | Source |
|------|-----------|--------|--------|--------|
| Paper | -2.33% | -3.89 | 97420 | alpaca_paper_v2/paper_journal.csv+wisdom_filter(ok) |
| Live | 2.22% | 5.07 | 308 | alpaca_live/paper_journal.csv |
**Paper − Live return:** -4.55 pp
- Read-only comparison; live Profile A is never modified by this review.

## Hypothesis (single factor)
**Proposed change:** none (keep `PAPER_NYSE_SLEEVE_CAP_PCT=0.90`)
**Rationale:** Only ranked sleeve is nyse at +2810.30. A single positive sleeve is HOLD.
**Mechanism:** Keep `PAPER_NYSE_SLEEVE_CAP_PCT` at 0.90. No sleeve-cap change this week.
**Status:** No 90d A/B — a tighten is not tested while this sleeve's contribution is positive.

## Controlled Experiment (90d paper-aggressive A/B)
| Metric | Baseline | Treatment | Δ |
|--------|----------|-----------|---|
| Return | n/a | n/a | n/a pp |
| Sharpe | n/a | n/a | n/a |
| Max DD | n/a | n/a | n/a pp |
_Baseline: skipped — sleeve contribution positive_
_Treatment: skipped — sleeve contribution positive_

## Recommendation
**HOLD** — Only ranked sleeve is nyse at +2810.30. A single positive sleeve is HOLD.

## Implementation (paper only)
**No paper change.**
Keep `PAPER_NYSE_SLEEVE_CAP_PCT=0.90`.
Do **not** copy to live.

## Appendix
**Wisdom (last 3):**
- 2026-09-24: ret=2.05612119432681 sharpe=None
- 2026-09-25: ret=-1.4521891429902123 sharpe=None
- 2026-09-26: ret=0.0 sharpe=None
**Notes:**
- Sleeve inferred from symbol on 9 close(s) with a blank sleeve column.
- 9 close(s) have a return sign and no dollar PnL — win rate uses that sign; sleeve dollars stay mark-to-market.
- Wisdom corroboration series: 7 point(s).

<!-- advisory only; never auto-apply -->
