# Paper Weekly Research — 2026-08-15

_Paper book only. Never auto-applies `.env`. Live Profile A is read-only here._

## Executive Decision
**HOLD** — Monitor only — data too sparse (grade C, 0 closes); track `PAPER_RISK_PER_TRADE=0.02` until grade B+ and 90d A/B.

Treatment under review: `PAPER_RISK_PER_TRADE=0.02` · _monitor only_

## Mandate
**Score: UNRELIABLE (data quality C)** · lookback 7d · regime `RHYME_A: Euphoric_Volatility`
_Insufficient closed trades for strong mandate (0/5 closes, grade C) — use mark-to-market attribution below._

| Target | Threshold | Actual |
|--------|-----------|--------|
| Ann. Sharpe | ≥ 1.0 | n/a |
| Max DD | ≤ 15.0% | n/a |
| 7d return | ≥ 0.5% | n/a |
| Closed trades | ≥ 5 | 0 |
Miss: Data grade C — do not trust mandate score this week

## Data Quality
**Grade C** · source `none`
| Grade | Criteria | Mandate usable? |
|-------|----------|-----------------|
| **A** | ≥4 clean days, 0 equity jumps, ≥5 closed trades | Yes |
| **B** | ≥4 days, ≤2 jumps after filter, trades may be sparse | Yes (cautious) |
| **C** | Missing equity, <2 days, or noisy/unfiltered | **No** |

Observations: 0 clean / 0 raw · jumps removed: 0 · method: EOD equity, √252 Sharpe, single-factor rule
- Insufficient clean equity observations.

## Performance (7d, cleaned)
| | |
|--|--|
| Equity | n/a → n/a |
| Return | n/a |
| Sharpe / Sortino | n/a / n/a |
| Max DD | n/a |
| Closed trades | 0 (WR n/a, exp n/a) |
| Daily hit rate | n/a |

**Key Observations**
- Insufficient closed trades for strong mandate (0 closes, grade C).
- No closed trades in 7d — sleeve ranks use mark-to-market only.
- No NYSE closed trades in 7d window.
- Nyse unrealized +552 (MTM).
- Core unrealized +332 (MTM).

## Markov HMM regime

- Markov HMM: OFF


## Sleeve Attribution (mark-to-market)
_Realized closes sparse — contrib = realized + unrealized from heartbeat._
Best **nyse** (+552) · Worst **core** (+332)

| Sleeve | Realized | Unrealized | Contrib | Cls | Src |
|--------|----------|------------|---------|-----|-----|
| nyse | +0 | +552 | +552 | 0 | unrealized |
| core | +0 | +332 | +332 | 0 | unrealized |
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
- No journal — NYSE hour stats unavailable; see intraday research memo.

## Live vs Paper Delta
_Read-only. Does not modify live._

| Book | 7d return | Sharpe | Equity | Source |
|------|-----------|--------|--------|--------|
| Paper | n/a | n/a | n/a | none |
| Live | n/a | n/a | n/a | none |
- Live book journal missing — delta is paper-only.

## Hypothesis (single factor)
**Proposed change:** `PAPER_RISK_PER_TRADE=0.02` (current `0.015`)
**Rationale:** Single factor under investigation: core sleeve is the weakest 7d contributor (contrib=+331.92, 34.0 bps of book).
**Mechanism:** Tighten `PAPER_RISK_PER_TRADE` from 0.015 → 0.02 to change that sleeve's capital allocation while holding all other policy constants fixed.
**Status: Data too sparse — monitor only.** Do not apply without grade B+ data and a passing 90d A/B.

## Controlled Experiment (90d paper-aggressive A/B)
| Metric | Baseline | Treatment | Δ |
|--------|----------|-----------|---|
| Return | 10.68% | 10.64% | -0.04 pp |
| Sharpe | 2.70 | 2.70 | 0.00 |
| Max DD | -3.13% | -3.16% | 0.03 pp |

## Recommendation
**HOLD** — Effect size mixed / within noise (ΔReturn=-0.04pp, ΔSharpe=+0.00, Δ|DD|=+0.03pp.) Gather another week of clean data. Caveats: data grade C; thin closed-trade sample; mandate unreliable.

## Implementation (paper only)
**Monitor only** — no `.env` change this week.
- Watch: `PAPER_RISK_PER_TRADE=0.02`
- Promote only after grade B+, ≥5 closed trades, and 90d A/B APPROVE.
- Do **not** copy to live.

## Appendix
**Wisdom (last 3):**
- 2026-08-13: ret=0.43214024350359814 sharpe=None
- 2026-08-14: ret=-0.29203971282761865 sharpe=None
- 2026-08-15: ret=-7.166756079701742e-05 sharpe=None
**Notes:**
- No closed trades in window — sleeve ranking uses mark-to-market (unrealized) only.
- Wisdom corroboration series: 7 point(s).

<!-- advisory only; never auto-apply -->
