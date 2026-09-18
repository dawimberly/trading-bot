# Paper Weekly Research — 2026-07-15

_Paper book only. Never auto-applies `.env`. Live Profile A is read-only here._

## Executive Decision
**HOLD** — Monitor only — data too sparse (grade C, 0 closes); track `METAL_SLEEVE_CAP_PCT=0.075` until grade B+ and 90d A/B.

Treatment under review: `METAL_SLEEVE_CAP_PCT=0.075` · _monitor only_

## Mandate
**Score: UNRELIABLE (data quality C)** · lookback 7d · regime `RHYME_C: Steady_Bullish_Growth`
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
- Nyse unrealized +274 (MTM).
- Crypto unrealized +157 (MTM).
- Metal unrealized +7 (MTM).

## Sleeve Attribution (mark-to-market)
_Realized closes sparse — contrib = realized + unrealized from heartbeat._
Best **nyse** (+274) · Worst **metal** (+7)

| Sleeve | Realized | Unrealized | Contrib | Cls | Src |
|--------|----------|------------|---------|-----|-----|
| nyse | +0 | +274 | +274 | 0 | unrealized |
| crypto | +0 | +157 | +157 | 0 | unrealized |
| metal | +0 | +7 | +7 | 0 | unrealized |
| spy | +0 | +0 | +0 | 0 | unrealized |

## NYSE Entry Quality
`PAPER_MOMENTUM_QUALITY_FIXES`=**on** · 7d NYSE exits: 0 (0 with `entry_hour`)

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
**Proposed change:** `METAL_SLEEVE_CAP_PCT=0.075` (current `0.10`)
**Rationale:** Single factor under investigation: metal sleeve is the weakest 7d contributor (contrib=+7.36, 0.7 bps of book).
**Mechanism:** Tighten `METAL_SLEEVE_CAP_PCT` from 0.10 → 0.075 to change that sleeve's capital allocation while holding all other policy constants fixed.
**Status: Data too sparse — monitor only.** Do not apply without grade B+ data and a passing 90d A/B.

## Controlled Experiment (90d paper-aggressive A/B)
| Metric | Baseline | Treatment | Δ |
|--------|----------|-----------|---|
| Return | n/a | n/a | n/a pp |
| Sharpe | n/a | n/a | n/a |
| Max DD | n/a | n/a | n/a pp |
_Baseline: skipped_
_Treatment: skipped_

## Recommendation
**HOLD** — Backtest skipped; sparse week — monitor proposed lever, no promotion.

## Implementation (paper only)
**Monitor only** — no `.env` change this week.
- Watch: `METAL_SLEEVE_CAP_PCT=0.075`
- Promote only after grade B+, ≥5 closed trades, and 90d A/B APPROVE.
- Do **not** copy to live.

## Appendix
**Wisdom (last 3):**
- 2026-07-13: ret=-0.6682342336947089 sharpe=None
- 2026-07-14: ret=0.5455882064412165 sharpe=None
- 2026-07-15: ret=-0.18306100315843565 sharpe=None
**Notes:**
- Lookback had few rows; extended history used then jump-filtered.
- No closed trades in window — sleeve ranking uses mark-to-market (unrealized) only.
- Wisdom corroboration series: 6 point(s).

<!-- advisory only; never auto-apply -->
