# Paper Weekly Research — 2026-08-01

_Paper book only. Never auto-applies `.env`. Live Profile A is read-only here._

## Executive Decision
**HOLD** — Monitor only — data too sparse (grade C, 0 closes); track `PAPER_NYSE_SLEEVE_CAP_PCT=0.15` until grade B+ and 90d A/B.

Treatment under review: `PAPER_NYSE_SLEEVE_CAP_PCT=0.15` · _monitor only_

## Mandate
**Score: UNRELIABLE (data quality C)** · lookback 7d · regime `RHYME_D: Range_Bound_Neutral`
_Insufficient closed trades for strong mandate (0/5 closes, grade C) — use mark-to-market attribution below._

| Target | Threshold | Actual |
|--------|-----------|--------|
| Ann. Sharpe | ≥ 1.0 | n/a |
| Max DD | ≤ 15.0% | 0.00% |
| 7d return | ≥ 0.5% | 0.06% |
| Closed trades | ≥ 5 | 0 |
Miss: Data grade C — do not trust mandate score this week

## Data Quality
**Grade C** · source `paper_chase_journal.csv+wisdom_filter(pct_drop_54.9%)`
| Grade | Criteria | Mandate usable? |
|-------|----------|-----------------|
| **A** | ≥4 clean days, 0 equity jumps, ≥5 closed trades | Yes |
| **B** | ≥4 days, ≤2 jumps after filter, trades may be sparse | Yes (cautious) |
| **C** | Missing equity, <2 days, or noisy/unfiltered | **No** |

Observations: 2 clean / 0 raw · jumps removed: 0 · method: EOD equity, √252 Sharpe, single-factor rule
- Sparse/noisy sample — mandate scoring and sleeve trade stats unreliable.

## Performance (7d, cleaned)
| | |
|--|--|
| Equity | 98235 → 98292 |
| Return | 0.06% |
| Sharpe / Sortino | n/a / n/a |
| Max DD | 0.00% |
| Closed trades | 0 (WR n/a, exp n/a) |
| Daily hit rate | 100% |

**Key Observations**
- Insufficient closed trades for strong mandate (0 closes, grade C).
- No closed trades in 7d — sleeve ranks use mark-to-market only.
- No NYSE closed trades in 7d window.
- Nyse unrealized +465 (MTM).
- Wisdom/regime tone defensive (`RHYME_D: Range_Bound_Neutral`).
- 7d return 0.06% below 0.5% target.

## Markov HMM regime

- Markov HMM: OFF


## Sleeve Attribution (mark-to-market)
_Realized closes sparse — contrib = realized + unrealized from heartbeat._
Best **nyse** (+465) · Worst **nyse** (+465)

| Sleeve | Realized | Unrealized | Contrib | Cls | Src |
|--------|----------|------------|---------|-----|-----|
| nyse | +0 | +465 | +465 | 0 | unrealized |
| spy | +0 | +0 | +0 | 0 | unrealized |
| crypto | +0 | +0 | +0 | 0 | unrealized |
| metal | +0 | +0 | +0 | 0 | unrealized |

## NYSE Entry Quality
`PAPER_MOMENTUM_QUALITY_FIXES`=off · 7d NYSE exits: 0 (0 with `entry_hour`)

| Window (ET) | Trades | Win% | Avg PnL |
|-------------|--------|------|---------|
| 9:30–10:00 open-chase | 0 | n/a | n/a |
| 12:00–14:00 midday | 0 | n/a | n/a |
365d intraday sim (memo): Δreturn n/a pp · ΔSharpe 0.03
- No NYSE closed trades in 7d window.
- `entry_hour` populates on exits after quality-fixes deploy — journal may be sparse.

## Live vs Paper Delta
_Read-only. Does not modify live._

| Book | 7d return | Sharpe | Equity | Source |
|------|-----------|--------|--------|--------|
| Paper | 0.06% | n/a | 98292 | paper_chase_journal.csv+wisdom_filter(pct_drop_54.9%) |
| Live | n/a | n/a | n/a | none |
- Live book journal missing — delta is paper-only.

## Hypothesis (single factor)
**Proposed change:** `PAPER_NYSE_SLEEVE_CAP_PCT=0.15` (current `0.20`)
**Rationale:** Single factor under investigation: nyse sleeve is the weakest 7d contributor (contrib=+464.54, 48.9 bps of book).
**Mechanism:** Tighten `PAPER_NYSE_SLEEVE_CAP_PCT` from 0.20 → 0.15 to change that sleeve's capital allocation while holding all other policy constants fixed.
**Status: Data too sparse — monitor only.** Do not apply without grade B+ data and a passing 90d A/B.

## Controlled Experiment (90d paper-aggressive A/B)
| Metric | Baseline | Treatment | Δ |
|--------|----------|-----------|---|
| Return | 7.21% | 7.18% | -0.03 pp |
| Sharpe | 1.92 | 1.93 | 0.01 |
| Max DD | -3.50% | -3.50% | 0.00 pp |

## Recommendation
**HOLD** — Effect size mixed / within noise (ΔReturn=-0.03pp, ΔSharpe=+0.01, Δ|DD|=+0.00pp.) Gather another week of clean data. Caveats: data grade C; thin closed-trade sample; mandate unreliable.

## Implementation (paper only)
**Monitor only** — no `.env` change this week.
- Watch: `PAPER_NYSE_SLEEVE_CAP_PCT=0.15`
- Promote only after grade B+, ≥5 closed trades, and 90d A/B APPROVE.
- Do **not** copy to live.

## Appendix
**Wisdom (last 3):**
- 2026-07-30: ret=0.561260711718492 sharpe=None
- 2026-07-31: ret=-0.16720308318577404 sharpe=None
- 2026-08-01: ret=-0.00014734893997170317 sharpe=None
**Notes:**
- Lookback had few rows; extended history used then jump-filtered.
- No closed trades in window — sleeve ranking uses mark-to-market (unrealized) only.
- Wisdom corroboration series: 7 point(s).

<!-- advisory only; never auto-apply -->
