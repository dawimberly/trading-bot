# Paper Weekly Research — 2026-09-05

_Paper book only. Never auto-applies `.env`. Live Profile A is read-only here._

## Executive Decision
**HOLD** — Mixed 90d A/B for `PAPER_NYSE_SLEEVE_CAP_PCT`; no paper change this week.

Treatment under review: `PAPER_NYSE_SLEEVE_CAP_PCT=0.675`

## Mandate
**Score: FAILING** · lookback 7d · regime `RHYME_D: Range_Bound_Neutral`
_Mandate unused — use `docs/DAILY_ANALYZE_LAST.md` (fills/ATR/cash/NYSE MV), not 7d Sharpe/DD n/a._

| Target | Threshold | Actual |
|--------|-----------|--------|
| Ann. Sharpe | ≥ 1.0 | -11.07 |
| Max DD | ≤ 15.0% | 1.45% |
| 7d return | ≥ 0.5% | -1.45% |
| Closed trades | ≥ 5 | 123 |

Pass: max DD 1.45% ≤ 15.0%; closed trades 123 ≥ 5
Miss: ann. Sharpe -11.07 < 1.0; 7d return -1.45% < 0.5%

## Data Quality
**Grade A** · source `paper_journal.csv+read_journal_csv`
| Grade | Criteria | Mandate usable? |
|-------|----------|-----------------|
| **A** | ≥4 clean days, 0 equity jumps, ≥5 closed trades | Yes |
| **B** | ≥4 days, ≤2 jumps after filter, trades may be sparse | Yes (cautious) |
| **C** | Missing equity, <2 days, or noisy/unfiltered | **No** |

Observations: 4 clean / 0 raw · jumps removed: 0 · method: EOD equity, √252 Sharpe, single-factor rule
- Clean curve, no discontinuities, adequate closed-trade sample.

## Performance (7d, cleaned)
| | |
|--|--|
| Equity | 96453 → 95053 |
| Return | -1.45% |
| Sharpe / Sortino | -11.07 / -11.07 |
| Max DD | 1.45% |
| Closed trades | 123 (WR 16%, exp -8.92) |
| Daily hit rate | 0% |

**Key Observations**
- SoT: `docs/DAILY_ANALYZE_LAST.md` (not this pipeline's 7d Sharpe/DD or mandate grade).
- vti_core 0; no VTI fills on last ANALYZE day.
- Last ANALYZE day: NYSE fills present; realized red on ATR-stop sessions.
- ANALYZE paper fills: 2026-09-02 paper Fills: 0; 2026-09-03 paper Fills: 0; 2026-09-04 paper Fills: 0.
- Nyse realized -1097 (123 closes, WR 16%).
- Core unrealized +9 (MTM).
- Wisdom/regime tone defensive (`RHYME_D: Range_Bound_Neutral`).
- 7d return -1.45% below 0.5% target.

## Markov HMM regime

- Markov HMM: OFF

- Informational only. HMM OFF is not a reason to enable it this weekend.

## Sleeve Attribution
Best **core** (+9) · Worst **nyse** (-341)

| Sleeve | Cls | Win% | Contrib | Src |
|--------|-----|------|---------|-----|
| core | 0 | n/a | +9 | unrealized |
| spy | 0 | n/a | +0 | unrealized |
| crypto | 0 | n/a | +0 | unrealized |
| metal | 0 | n/a | +0 | unrealized |
| nyse | 123 | 16% | -341 | mixed |

## NYSE Entry Quality
`PAPER_MOMENTUM_QUALITY_FIXES`=off · 7d NYSE exits: 123 (0 with `entry_hour`)

| Window (ET) | Trades | Win% | Avg PnL |
|-------------|--------|------|---------|
| 9:30–10:00 open-chase | 0 | n/a | n/a |
| 12:00–14:00 midday | 0 | n/a | n/a |
365d intraday sim (memo): Δreturn n/a pp · ΔSharpe 0.03
- No `entry_hour` on NYSE exits yet — hour buckets empty until post-deploy closes.

## Live vs Paper Delta
_Read-only. Does not modify live._

| Book | 7d return | Sharpe | Equity | Source |
|------|-----------|--------|--------|--------|
| Paper | -1.45% | -11.07 | 95053 | paper_journal.csv+read_journal_csv |
| Live | -0.10% | -0.10 | 302 | paper_journal.csv |
**Paper − Live return:** -1.35 pp
- Read-only comparison; live Profile A is never modified by this review.

## Hypothesis (single factor)
**Proposed change:** `PAPER_NYSE_SLEEVE_CAP_PCT=0.675` (current `0.90`)
**Rationale:** Single factor under investigation: nyse sleeve is the weakest 7d contributor (contrib=-341.22, -35.9 bps of book).
**Mechanism:** Tighten `PAPER_NYSE_SLEEVE_CAP_PCT` from 0.90 → 0.675 to change that sleeve's capital allocation while holding all other policy constants fixed.
**Pass (90d A/B):** On a 90d paper-aggressive backtest, proposed config should improve Sharpe and not worsen max-DD magnitude materially (ΔSharpe>+0.05, ΔReturn≥−0.5pp, Δ|DD|≤+0.5pp).
**Fail:** Reject if proposed Sharpe worsens by >0.05, or return drops >1pp with worse |DD|, or data grade remains C with no closed-trade corroboration.

## Controlled Experiment (90d paper-aggressive A/B)
| Metric | Baseline | Treatment | Δ |
|--------|----------|-----------|---|
| Return | 2.73% | 2.73% | 0.00 pp |
| Sharpe | 1.22 | 1.22 | 0.00 |
| Max DD | -3.53% | -3.53% | 0.00 pp |

## Recommendation
**HOLD** — Effect size mixed / within noise (ΔReturn=+0.00pp, ΔSharpe=+0.00, Δ|DD|=+0.00pp.) Gather another week of clean data.

## Implementation (paper only)
Apply **only** to the paper book `.env` if APPROVE:
```
PAPER_NYSE_SLEEVE_CAP_PCT=0.675
```
Do **not** copy to live. Restart paper bot. Re-check next Saturday.

## Appendix
**Wisdom (last 3):**
- 2026-08-30: ret=-0.007029337635255928 sharpe=None
- 2026-08-31: ret=-0.16526325786874185 sharpe=None
- 2026-09-01: ret=-1.2817654104568699 sharpe=None
**Notes:**
- Wisdom corroboration series: 4 point(s).

<!-- advisory only; never auto-apply -->
